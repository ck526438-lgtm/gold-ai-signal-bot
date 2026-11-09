"""Bar schema shared by every data provider."""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pandas as pd

COLUMNS = ["time", "open", "high", "low", "close", "tick_volume", "spread_points"]

TIMEFRAME_MINUTES = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440,
}


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce any provider frame into the canonical bar frame."""
    out = df.copy()
    if "time" not in out.columns:
        raise ValueError("bars frame needs a 'time' column")
    if pd.api.types.is_numeric_dtype(out["time"]):
        out["time"] = pd.to_datetime(out["time"], unit="s", utc=True)
    else:
        out["time"] = pd.to_datetime(out["time"], utc=True)
    for col in ("open", "high", "low", "close"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "tick_volume" not in out.columns:
        out["tick_volume"] = 0
    out["tick_volume"] = pd.to_numeric(out["tick_volume"], errors="coerce").fillna(0).astype("int64")
    if "spread_points" not in out.columns:
        out["spread_points"] = math.nan
    out["spread_points"] = pd.to_numeric(out["spread_points"], errors="coerce")
    out = out[COLUMNS]
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    return out


def expected_gap_minutes(timeframe: str) -> int:
    return TIMEFRAME_MINUTES[timeframe.upper()]


def closed_only(df: pd.DataFrame, timeframe: str, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Retirer la bougie en cours de formation.

    MT5 renvoie la bougie M5 encore ouverte: la trader, c'est signaler sur une
    cloteure qui n'existe pas encore et gonfler le backtest.
    """
    if df.empty:
        return df
    gap = pd.Timedelta(minutes=expected_gap_minutes(timeframe))
    now = now or pd.Timestamp(datetime.now(timezone.utc))
    last_open = df["time"].iloc[-1]
    if last_open + gap > now:
        return df.iloc[:-1].reset_index(drop=True)
    return df


def sanity_report(df: pd.DataFrame, timeframe: str) -> dict:
    """Detect the traps that silently destroy a signal engine."""
    rep = {"bars": int(len(df))}
    if len(df) < 2:
        return rep
    spans = df["time"].diff().dt.total_seconds().div(60).dropna()
    gap = expected_gap_minutes(timeframe)
    rep["median_gap_min"] = float(spans.median())
    rep["bars_wrong_size"] = int((spans > gap * 1.5).sum())
    rep["first"] = str(df["time"].iloc[0])
    rep["last"] = str(df["time"].iloc[-1])
    rep["days_covered"] = round(float(spans.sum() / 1440), 1)
    bad_ohlc = int(((df["high"] < df[["open", "close", "low"]].max(axis=1)) |
                    (df["low"] > df[["open", "close", "high"]].min(axis=1))).sum())
    rep["bad_ohlc"] = bad_ohlc
    # Le volume reel est presque toujours 0 sur l'or CFD: exploitables = tick_volume.
    rep["zero_volume_share"] = round(float((df["tick_volume"] <= 0).mean()), 3)
    return rep
