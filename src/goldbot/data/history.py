"""Cache local + telechargement par morceaux.

Le serveur MT5 rejette les ranges trop larges (une requete d'un an renvoie 0 barre
sans erreur). On decoupe en semaines et on concatene.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ..config import get, resolve_path
from .bars import normalize


def cache_file(cfg: dict, symbol: str, timeframe: str) -> Path:
    folder = resolve_path(get(cfg, "data.cache_dir", "data/cache"))
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{symbol}_{timeframe}.csv.gz"


def load_cache(cfg: dict, symbol: str, timeframe: str) -> pd.DataFrame:
    path = cache_file(cfg, symbol, timeframe)
    if not path.exists():
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "tick_volume", "spread_points"])
    return normalize(pd.read_csv(path))


def save_cache(cfg: dict, symbol: str, timeframe: str, df: pd.DataFrame) -> Path:
    path = cache_file(cfg, symbol, timeframe)
    df.to_csv(path, index=False, compression="gzip")
    return path


def merge_frames(existing: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """Le serveur corrige les bougies recentes: la nouvelle version ecrase l'ancienne."""
    if existing is None or existing.empty:
        return normalize(fresh)
    joined = pd.concat([existing, fresh], ignore_index=True)
    return normalize(joined)


def fetch_history(cfg: dict, provider, timeframe: str, days: int,
                  chunk_days: int | None = None, quiet: bool = False) -> pd.DataFrame:
    chunk_days = int(chunk_days or get(cfg, "data.chunk_days", 7))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = now - timedelta(days=days)
    frames, cursor, empty_streak = [], start, 0
    while cursor < now:
        end = min(cursor + timedelta(days=chunk_days), now)
        frame = provider.bars_range(timeframe, cursor, end)
        if len(frame):
            frames.append(frame)
            empty_streak = 0
            if not quiet:
                print(f"  {cursor:%Y-%m-%d} -> {len(frame):5d} bars (cumul {sum(len(f) for f in frames)})", flush=True)
        else:
            empty_streak += 1
            if empty_streak >= 3:
                if not quiet:
                    print(f"  arrete a {cursor:%Y-%m-%d}: le serveur n'a plus d'historique au-dela", flush=True)
                break
        cursor = end
    if not frames:
        raise RuntimeError("Aucune barre recuperee: verifier que MT5 est connecte et le nom du symbole.")
    return merge_frames(pd.DataFrame(), pd.concat(frames, ignore_index=True))


def refresh(cfg: dict, provider, timeframe: str, quiet: bool = False) -> pd.DataFrame:
    """Met le cache a jour du trou jusqu'a maintenant, puis le relit."""
    df = load_cache(cfg, provider.symbol, timeframe)
    if df.empty:
        full = fetch_history(cfg, provider, timeframe, int(get(cfg, "data.history_days", 400)), quiet=quiet)
        save_cache(cfg, provider.symbol, timeframe, full)
        return full
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    tail = provider.bars_range(timeframe, df["time"].iloc[-1].to_pydatetime() - timedelta(days=1), now)
    merged = merge_frames(df, tail)
    path = save_cache(cfg, provider.symbol, timeframe, merged)
    if not quiet:
        print(f"  cache {path.name}: {len(df)} -> {len(merged)} bars", flush=True)
    return merged
