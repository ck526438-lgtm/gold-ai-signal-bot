"""Service d'analyse : recharge les données à chaque clôture M5, détecte les signaux,
suit leurs résultats et prépare tout ce que l'interface affiche."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .backtest import simulate
from .config import Config, analysis_config
from .data import SOURCE_LABELS, load_data
from .journal import Journal
from .strategy import Analyzer, in_session

log = logging.getLogger(__name__)
CHART_BARS = 600


def _t(ts: pd.Timestamp) -> int:
    """Horodatage UNIX (secondes) pour le graphique."""
    return int(ts.timestamp())


class AnalysisService:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.journal = Journal(Path(cfg.data_dir) / "journal.db")
        self._lock = threading.RLock()
        self.an: Analyzer | None = None
        self.source_used: str | None = None
        self.updated_at: datetime | None = None
        self.error: str | None = None
        self._snapshot: dict | None = None
        self.listeners: list = []          # callbacks(event: dict)
        self.executor = None               # robot de trading (optionnel)

    # ------------------------------------------------------------------ #
    def set_config(self, cfg: Config) -> None:
        with self._lock:
            self.cfg = cfg
        if self.executor is not None:
            self.executor.set_config(cfg)

    def wait_idle(self) -> None:
        with self._lock:
            pass

    def refresh(self) -> list[dict]:
        """Recharge les données et met tout à jour. Renvoie les NOUVEAUX signaux directs."""
        with self._lock:
            try:
                df, src = load_data(self.cfg)
                if len(df) < 300:
                    raise RuntimeError(f"Pas assez de données ({len(df)} bougies)")
                an = Analyzer(df, analysis_config(self.cfg))
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                log.error("Chargement des données impossible : %s", e)
                self._emit({"type": "error", "message": self.error})
                return []
            self.an, self.source_used, self.error = an, src, None
            self.updated_at = datetime.now(timezone.utc)

            last_i = len(an.c) - 1
            new_live: list[dict] = []
            for sig in an.scan():
                origin = "direct" if sig.index == last_i else "historique"
                d = sig.to_dict()
                if origin == "direct":
                    d["ml"] = self._ml(an, sig)
                if self.journal.add(d, self.cfg.symbol, origin) and origin == "direct":
                    new_live.append(d)
            self._track_outcomes(an)
            self._snapshot = self._build_snapshot(an)

        for s in new_live:
            self._emit({"type": "signal", "signal": s})
            self._desktop(s)
        if self.executor is not None:
            last = an.df.iloc[-1]
            from .backtest import reversal_info
            self.executor.manage({"close": float(last["close"]), "high": float(last["high"]),
                                  "low": float(last["low"]), "reversal": reversal_info(an, last_i)})
            for s in new_live:
                self.executor.on_signal(s)
        self._emit({"type": "update", "updated_at": self.updated_at.isoformat()})
        return new_live

    def _ml(self, an: Analyzer, sig) -> dict | None:
        """Probabilité de gain estimée par le modèle IA (si entraîné)."""
        q = getattr(self.executor, "quality", None)
        if q is None or sig is None:
            return None
        return q.ml.annotate(an, sig, self.cfg.trading)

    def _track_outcomes(self, an: Analyzer) -> None:
        pos = {ts: i for i, ts in enumerate(an.idx)}
        s = self.cfg.strategy
        open_keys = set(self.journal.open_keys())
        rows = {r["key"]: r for r in self.journal.list(100_000) if r["key"] in open_keys}
        for key, row in rows.items():
            ts = pd.Timestamp(datetime.strptime(key[:12], "%Y%m%d%H%M"), tz="UTC")
            if ts not in pos:
                continue
            sig = an.inspect(pos[ts])
            if sig is None:
                continue
            # on simule avec les niveaux ENREGISTRÉS (ceux qui ont été annoncés)
            sig.entry, sig.sl, sig.tp1, sig.tp2 = row["entry"], row["sl"], row["tp1"], row["tp2"]
            sig.direction = row["direction"]
            out = simulate(an, sig, s.spread_pips, s.max_hold_bars)
            exit_time = (an.idx[out.exit_index] + pd.Timedelta(minutes=5)).isoformat() \
                if out.exit_index is not None else None
            self.journal.update_outcome(key, out.status, out.result_r, out.tp1_hit, exit_time)

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        with self._lock:
            if self._snapshot is None:
                return {"ready": False, "error": self.error, "symbol": self.cfg.symbol}
            snap = dict(self._snapshot)
            snap["journal_stats"] = self.journal.stats()
            snap["recent"] = self.journal.list(8)
            return snap

    def _build_snapshot(self, an: Analyzer) -> dict:
        i = len(an.c) - 1
        df = an.df.iloc[-CHART_BARS:]
        start = len(an.c) - len(df)
        price, prev_day = an.c[i], _price_24h_ago(an)

        candles = [{"time": _t(ts), "open": round(o, 5), "high": round(h, 5), "low": round(l, 5),
                    "close": round(c, 5)}
                   for ts, o, h, l, c in zip(df.index, df["open"], df["high"], df["low"], df["close"])]

        def line(col: str) -> list[dict]:
            return [{"time": _t(ts), "value": round(float(v), 5)}
                    for k, (ts, v) in enumerate(df[col].items()) if start + k >= 30]

        markers = [{"time": _t(sg.time), "direction": sg.direction, "quality": sg.quality,
                    "score": sg.score, "key": sg.key}
                   for sg in an.scan(start)]

        zones = sorted(an.zones(i), key=lambda z: abs(z.price - price))[:8]
        radar = an.inspect(i)
        if radar is not None and radar.blocked is None and not any(m["time"] == _t(radar.time) for m in markers):
            radar.blocked = (f"un signal {radar.side.lower()} a déjà été donné il y a moins de "
                             f"{self.cfg.strategy.cooldown_bars * 5} min")
        prev_radar = None
        for k in range(i - 1, max(i - 6, an.warmup), -1):  # dernier setup récent
            cand = an.inspect(k)
            if cand is not None:
                prev_radar = cand
                break

        ef, em, es = an.ef[i], an.em[i], an.es[i]
        m5_trend = 1 if price > em and ef > em else -1 if price < em and ef < em else 0
        close_t = an.idx[i] + pd.Timedelta(minutes=5)
        cfg = self.cfg
        return {
            "ready": True,
            "symbol": cfg.symbol,
            "source": self.source_used,
            "source_label": SOURCE_LABELS.get(self.source_used or "", self.source_used),
            "updated_at": self.updated_at.isoformat(),
            "last_close": close_t.isoformat(),
            "price": round(price, 5),
            "change_24h_pips": round((price - prev_day) / cfg.pip, 1) if prev_day else None,
            "context": {
                "h1_bias": int(an.bias[i]),
                "m5_trend": m5_trend,
                "rsi": round(float(an.rsi[i]), 1),
                "adx": round(float(an.adx[i]), 1),
                "atr_pips": round(float(an.atr[i]) / cfg.pip, 1),
                "in_session": in_session(close_t, cfg.strategy.sessions_utc),
                "sessions": cfg.strategy.sessions_utc,
                "ema": {"fast": round(ef, 5), "mid": round(em, 5), "slow": round(es, 5)},
                "day_high": round(float(an.df["high"].iloc[-288:].max()), 5),
                "day_low": round(float(an.df["low"].iloc[-288:].min()), 5),
            },
            "zones": [{"price": round(z.price, 5), "touches": z.touches, "kind": z.kind,
                       "distance_pips": round((z.price - price) / cfg.pip, 1)} for z in zones],
            "radar": {**radar.to_dict(), "ml": self._ml(an, radar)} if radar else None,
            "radar_recent": prev_radar.to_dict() if prev_radar else None,
            "min_score": cfg.strategy.min_score,
            "chart": {
                "candles": candles,
                "ema_fast": line("ema_fast"), "ema_mid": line("ema_mid"), "ema_slow": line("ema_slow"),
                "rsi": [{"time": _t(ts), "value": round(float(v), 1)} for ts, v in df["rsi"].items()],
                "markers": markers,
            },
        }

    # ------------------------------------------------------------------ #
    def _emit(self, event: dict) -> None:
        for cb in list(self.listeners):
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                pass

    def _desktop(self, s: dict) -> None:
        if not self.cfg.desktop_notifications:
            return
        try:
            from plyer import notification
            notification.notify(title=f"{s['side']} {self.cfg.symbol} — {s['quality']} ({s['score']}/10)",
                                message=f"Entrée {s['entry']:.2f} · SL {s['sl']:.2f} · TP {s['tp1']:.2f}",
                                app_name="Gold AI Signal Bot", timeout=15)
        except Exception:  # noqa: BLE001 — plyer est optionnel
            pass


def _price_24h_ago(an: Analyzer) -> float | None:
    target = an.idx[-1] - pd.Timedelta(hours=24)
    k = an.idx.searchsorted(target)
    return float(an.c[k]) if 0 <= k < len(an.c) - 1 else None
