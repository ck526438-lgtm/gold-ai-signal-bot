"""Features ML calculées UNIQUEMENT à partir des bougies clôturées.

Règle : la feature à l'indice N ne dépend que des bougies 0..N.
  - shift(k) avec k > 0 uniquement (jamais de shift négatif)
  - rolling / ewm « à gauche » (jamais de fenêtre centrée)
  - VWAP de session cumulé depuis 00:00 UTC (cumsum causal)
Vérifié par tests/test_no_lookahead_ml.py (perturbation des bougies N+1..N+100).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators as ind

BASE_FEATURES = [
    "ret_1", "ret_3", "ret_5", "ret_15",
    "atr14_norm", "std20",
    "rsi7", "rsi14", "roc10",
    "ema9_21", "ema50_200", "vwap_dist",
    "range_pos20",
    "vol_ratio20",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
]
OPTIONAL_FEATURES = ["spread_mean20"]


def build_features(df: pd.DataFrame, include_optional: bool = True) -> pd.DataFrame:
    close = df["close"]
    logc = np.log(close)
    f = pd.DataFrame(index=df.index)

    # Retours (log) sur 1, 3, 5, 15 bougies — regard vers le passé uniquement
    for k in (1, 3, 5, 15):
        f[f"ret_{k}"] = logc - logc.shift(k)

    # Volatilité
    atr14 = ind.atr(df, 14)
    f["atr14_norm"] = atr14 / close
    f["std20"] = f["ret_1"].rolling(20, min_periods=20).std()

    # Momentum (RSI normalisé dans [0,1])
    f["rsi7"] = ind.rsi(close, 7) / 100.0
    f["rsi14"] = ind.rsi(close, 14) / 100.0
    f["roc10"] = ind.roc(close, 10)

    # Tendance
    f["ema9_21"] = ind.ema(close, 9) / ind.ema(close, 21) - 1.0
    f["ema50_200"] = ind.ema(close, 50) / ind.ema(close, 200) - 1.0
    typical = (df["high"] + df["low"] + close) / 3.0
    vol = df["tick_volume"].clip(lower=1.0)
    day = df.index.floor("D")
    vwap = (typical * vol).groupby(day).cumsum() / vol.groupby(day).cumsum()
    f["vwap_dist"] = close / vwap - 1.0

    # Structure : position du close dans le range des 20 dernières bougies
    hh = df["high"].rolling(20, min_periods=20).max()
    ll = df["low"].rolling(20, min_periods=20).min()
    rng = (hh - ll).replace(0.0, np.nan)
    f["range_pos20"] = ((close - ll) / rng).clip(0.0, 1.0)

    # Volume
    f["vol_ratio20"] = df["tick_volume"] / df["tick_volume"].rolling(20, min_periods=20).mean()

    # Temps (sessions Londres/NY, jour de semaine)
    hour = df.index.hour + df.index.minute / 60.0
    f["hour_sin"] = np.sin(2 * np.pi * hour / 24.0)
    f["hour_cos"] = np.cos(2 * np.pi * hour / 24.0)
    dow = df.index.dayofweek
    f["dow_sin"] = np.sin(2 * np.pi * dow / 7.0)
    f["dow_cos"] = np.cos(2 * np.pi * dow / 7.0)

    if include_optional and "spread" in df and df["spread"].notna().any():
        f["spread_mean20"] = df["spread"].rolling(20, min_periods=1).mean()

    return f.replace([np.inf, -np.inf], np.nan)


def feature_columns(f: pd.DataFrame) -> list[str]:
    return [c for c in BASE_FEATURES + OPTIONAL_FEATURES if c in f.columns]
