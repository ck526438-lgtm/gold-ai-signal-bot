"""Decalage horloge broker -> UTC veritable.

Les timestamps MT5 sont en heure serveur (ici UTC+2). Sans correction, les
filtres de session et les ancrages journaliers jouent a cotre de la reel London
open. Le decalage se mesure sur le dernier tick, mais un marche ferme le week-end
rend la mesure fausse: on la valide, sinon on rejoue la derniere valeur connue.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_PLAUSIBLE_HOURS = 14


def store_file(cache_dir: Path) -> Path:
    return Path(cache_dir) / "broker_offset.json"


def measure_hours(tick_epoch: float) -> float | None:
    """Heures de decalage broker-UTC, ou None si la mesure n'est pas exploitable."""
    if not tick_epoch:
        return None
    raw = (datetime.fromtimestamp(tick_epoch, timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 3600
    rounded = round(raw)
    if abs(raw - rounded) > 0.05 or abs(rounded) > MAX_PLAUSIBLE_HOURS:
        return None
    return float(rounded)


def load_stored(cache_dir: Path) -> float | None:
    path = store_file(cache_dir)
    if not path.exists():
        return None
    try:
        return float(json.loads(path.read_text())["hours"])
    except Exception:
        return None


def save_stored(cache_dir: Path, hours: float, source: str) -> None:
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    store_file(cache_dir).write_text(json.dumps({
        "hours": hours,
        "source": source,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }))


def resolve_offset(tick_epoch: float | None, cache_dir: Path, override: float | None) -> dict:
    if override is not None:
        hours, source = float(override), "config"
        save_stored(cache_dir, hours, "config")
    else:
        hours = measure_hours(tick_epoch or 0)
        if hours is None:
            hours = load_stored(cache_dir)
            source = "market_closed_fallback" if hours is not None else "none"
            if hours is None:
                hours = 0.0
        else:
            source = "measured"
            save_stored(cache_dir, hours, "measured")
    return {
        "hours": hours,
        "source": source,
        "delta": timedelta(hours=hours),
        "warning": "Horodatages MT5 non corriges: repere la valeur dans config mt5.broker_utc_offset_hours"
        if source in ("none", "market_closed_fallback") else None,
    }
