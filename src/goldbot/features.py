"""Features M5 pour XAUUSD.

Tout est calcule sur la bougie i avec les donnees <= i (aucun look-ahead) et
tout est vectorise pandas: pas de ta-lib, donc installable sur Kali.

Deux regles specifiques a l'or issues des donnees reelles du broker:
  - la journee de bourse va de 23:00 a 21:00 UTC, pas de minota minuit:
    le VWAP ancre "journalier" utilise trading_day = (time + 1h).date ;
  - de 21:00 a 23:00 UTC il n'y a aucune cote: un signal declenche dans
    l'heure precedente se fait deporter au reopen.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import get

SESSIONS = {
    "asia": (0, 7),
    "london": (7, 15),
    "ny": (13, 21),
    "overlap": (13, 16),
}


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False, ignore_na=True).mean()


def _slope(frame: pd.Series, period: int) -> pd.Series:
    return frame.pct_change(period) * 100


def ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False, ignore_na=True).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0), period)
    loss = _wilder((-delta).clip(lower=0), period)
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def adx(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = _wilder(true_range(df), period).replace(0, np.nan)
    plus_di = 100 * _wilder(plus_dm, period) / tr
    minus_di = 100 * _wilder(minus_dm, period) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1.0 / period, adjust=False, ignore_na=True).mean(), plus_di, minus_di


def stochastic(df: pd.DataFrame, k_period: int = 14, smooth: int = 3) -> tuple[pd.Series, pd.Series]:
    ll = df["low"].rolling(k_period, min_periods=k_period).min()
    hh = df["high"].rolling(k_period, min_periods=k_period).max()
    raw = 100 * (df["close"] - ll) / (hh - ll).replace(0, np.nan)
    k = raw.rolling(smooth, min_periods=1).mean()
    return k, k.rolling(smooth, min_periods=1).mean()


def vwap_anchored(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """VWAP par journee de bourse (23:00 -> 21:00 UTC) et sa deviation en ATR."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["tick_volume"].astype("float64")
    trading_day = (df["time"] + pd.Timedelta(hours=1)).dt.date
    cum_pv = (typical * vol).groupby(trading_day).cumsum()
    cum_v = vol.groupby(trading_day).cumsum().replace(0, np.nan)
    vwap = (cum_pv / cum_v).fillna(typical)
    day_std = typical.groupby(trading_day).transform(lambda s: s.std(ddof=0)).replace(0, np.nan)
    return vwap, (df["close"] - vwap)


def compute_features(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Retourne df + une colonne par feature, alignee sur les lignes d'entree."""
    rules = get(cfg, "signal.rules", {}) or {}
    close_hour = int(get(cfg, "market.daily_close_utc_hour", 21))
    open_hour = int(get(cfg, "market.daily_open_utc_hour", 23))

    f = df.copy()
    c, h, l = f["close"], f["high"], f["low"]
    atr_period = int(rules.get("atr_period", 14))

    f["atr"] = _wilder(true_range(f), atr_period)
    f["tr"] = true_range(f)
    ema_periods = [int(p) for p in rules.get("trend_ema", [20, 50, 200])]
    for p in ema_periods:
        f[f"ema_{p}"] = ema(c, p)
        f[f"slope_ema_{p}"] = _slope(f[f"ema_{p}"], 6)

    fast, slow, mid = (12, 26, 9)
    macd = ema(c, fast) - ema(c, slow)
    f["macd"] = macd
    f["macd_signal"] = ema(macd, mid)
    f["macd_hist"] = macd - f["macd_signal"]
    f["macd_hist_slope"] = f["macd_hist"].diff(3)

    f["rsi"] = rsi(c, int(rules.get("rsi_period", 14)))
    f["rsi_slope"] = f["rsi"].diff(3)
    f["adx"], f["plus_di"], f["minus_di"] = adx(f, atr_period)

    k, d = stochastic(f)
    f["stoch_k"], f["stoch_d"] = k, d

    bb_mid = c.rolling(20, min_periods=20).mean()
    bb_std = c.rolling(20, min_periods=20).std(ddof=0).replace(0, np.nan)
    f["bb_z"] = (c - bb_mid) / bb_std
    f["bb_width"] = (4 * bb_std) / bb_mid
    f["bb_width_pct"] = f["bb_width"].rolling(384, min_periods=96).rank(pct=True)

    f["vwap"], f["vwap_dist"] = vwap_anchored(f)
    f["vwap_atr"] = f["vwap_dist"] / f["atr"].replace(0, np.nan)

    hh20 = h.rolling(20, min_periods=20).max()
    ll20 = l.rolling(20, min_periods=20).min()
    f["donchian_pos"] = ((c - ll20) / (hh20 - ll20).replace(0, np.nan)).clip(0, 1)
    hh96 = h.rolling(96, min_periods=48).max()
    ll96 = l.rolling(96, min_periods=48).min()
    f["range_pos_8h"] = ((c - ll96) / (hh96 - ll96).replace(0, np.nan)).clip(0, 1)

    for n in (1, 3, 6, 12, 24):
        f[f"ret_{n}"] = c.pct_change(n) * 100
    f["vol_12"] = c.pct_change().rolling(12).std()
    f["vol_48"] = c.pct_change().rolling(48).std()
    f["vol_regime"] = (f["vol_12"] / f["vol_48"].replace(0, np.nan)).clip(0, 5)
    f["atr_pct_of_price"] = f["atr"] / c * 100
    f["atr_rank"] = f["atr"].rolling(384, min_periods=96).rank(pct=True)

    rng = (h - l).replace(0, np.nan)
    body = (c - f["open"]).abs()
    f["body_ratio"] = body / rng
    f["upper_wick"] = (h - np.maximum(c, f["open"])) / rng
    f["lower_wick"] = (np.minimum(c, f["open"]) - l) / rng
    sign = np.sign(c.diff().fillna(0))
    f["consec_closes"] = sign.groupby((sign != sign.shift()).cumsum()).cumsum()
    f["close_loc"] = ((c - l) / rng).fillna(0.5)

    vol_ma = f["tick_volume"].rolling(48, min_periods=12).mean().replace(0, np.nan)
    f["volume_z"] = ((f["tick_volume"] - vol_ma) / np.sqrt(vol_ma)).clip(-6, 12)

    hour = f["time"].dt.hour
    minute = f["time"].dt.minute
    f["minute_of_day"] = hour * 60 + minute
    f["dow"] = f["time"].dt.day_of_week
    for name, (start, end) in SESSIONS.items():
        f[f"session_{name}"] = ((hour >= start) & (hour < end)).astype("int64")
    # Minutes avant la coupure quotidienne: -1 quand la session est deja finie.
    mins_to_close = (close_hour * 60 - f["minute_of_day"])
    mins_to_close = mins_to_close.where(mins_to_close > 0, mins_to_close + 1440)
    f["min_to_break"] = mins_to_close.clip(0, 240)
    mins_from_open = (f["minute_of_day"] - open_hour * 60)
    f["min_since_open"] = mins_from_open.where(mins_from_open >= 0, mins_from_open + 1440).clip(0, 1440)
    f["is_sunday_reopen"] = (f["dow"] == 6).astype("int64")
    f["spread_points"] = f["spread_points"].fillna(f["spread_points"].median())

    horizon = int(get(cfg, "signal.ml.horizon_bars", 12))
    f = f.drop(columns=[col for col in ("fwd_ret", "label") if col in f.columns])
    f["fwd_ret"] = c.shift(-horizon) / c - 1.0
    f["fwd_max_up"] = h.rolling(horizon).max().shift(-horizon) / c - 1.0
    f["fwd_max_dn"] = l.rolling(horizon).min().shift(-horizon) / c - 1.0
    return f


# Colonnes utilisees par le modele ML. Les fwd_* servent aux labels, jamais en entree.
LABEL_COLS = ["fwd_ret", "fwd_max_up", "fwd_max_dn"]
FEATURE_COLS = [
    "ret_1", "ret_3", "ret_6", "ret_12", "ret_24",
    "slope_ema_20", "slope_ema_50", "slope_ema_200",
    "rsi", "rsi_slope", "macd_hist", "macd_hist_slope", "adx", "plus_di", "minus_di",
    "stoch_k", "stoch_d", "bb_z", "bb_width", "bb_width_pct",
    "vwap_atr", "donchian_pos", "range_pos_8h",
    "atr_pct_of_price", "atr_rank", "vol_regime",
    "body_ratio", "upper_wick", "lower_wick", "consec_closes", "close_loc",
    "volume_z", "minute_of_day", "dow", "min_to_break", "min_since_open",
    "session_asia", "session_london", "session_ny", "session_overlap",
    "spread_points",
]


def drop_warmed_up(f: pd.DataFrame, min_bars: int = 220) -> pd.DataFrame:
    """Les premieres lignes ont des EMA/rolling non stables: on les exclut du training."""
    return f.iloc[min_bars:].reset_index(drop=True)
