"""Indicateurs techniques (implémentation maison, sans dépendance externe)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    """Moyenne lissée de Wilder."""
    return s.ewm(alpha=1 / n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = rma(d.clip(lower=0), n)
    down = rma(-d.clip(upper=0), n)
    rs = up / down.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift()
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return rma(true_range(df), n)


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = rma(true_range(df), n)
    plus_di = 100 * rma(pd.Series(plus_dm, index=df.index), n) / tr
    minus_di = 100 * rma(pd.Series(minus_dm, index=df.index), n) / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return rma(dx.fillna(0), n)


def add_indicators(df: pd.DataFrame, s) -> pd.DataFrame:
    """Ajoute toutes les colonnes d'indicateurs. Tout est causal (pas de futur)."""
    out = df.copy()
    out["ema_fast"] = ema(out["close"], s.ema_fast)
    out["ema_mid"] = ema(out["close"], s.ema_mid)
    out["ema_slow"] = ema(out["close"], s.ema_slow)
    out["rsi"] = rsi(out["close"], s.rsi_period)
    out["atr"] = atr(out, s.atr_period)
    out["adx"] = adx(out, s.adx_period)
    out = out.join(higher_tf_bias(df), how="left")
    out["htf_bias"] = out["htf_bias"].ffill().fillna(0)
    for rule, name in (("15min", "m15_bias"), ("4h", "h4_bias")):
        out = out.join(tf_bias(df, rule, name), how="left")
        out[name] = out[name].ffill().fillna(0)
    return out


def higher_tf_bias(df: pd.DataFrame) -> pd.DataFrame:
    """Tendance H1 : +1 haussière, -1 baissière, 0 neutre.

    Une bougie H1 n'est connue qu'à sa clôture : on décale d'une heure
    pour ne jamais utiliser une H1 en cours de formation.
    """
    # Les bougies M5 sont indexées par leur heure d'OUVERTURE.
    # La H1 [09:00, 10:00) est complète à la clôture de la M5 ouverte à 09:55.
    h1 = df["close"].resample("1h").last().dropna()
    e20, e50 = ema(h1, 20), ema(h1, 50)
    bias = np.where((h1 > e50) & (e20 > e50), 1, np.where((h1 < e50) & (e20 < e50), -1, 0))
    b = pd.Series(bias, index=h1.index + pd.Timedelta(minutes=55), name="htf_bias")
    return b.to_frame()


def tf_bias(df: pd.DataFrame, rule: str, name: str) -> pd.DataFrame:
    """Tendance d'une unité de temps supérieure (M15, H4…), connue seulement à sa clôture.

    La bougie [t, t + durée) est complète à la clôture de la M5 ouverte à t + durée − 5 min.
    """
    step = pd.Timedelta(rule)
    c = df["close"].resample(rule).last().dropna()
    e20, e50 = ema(c, 20), ema(c, 50)
    bias = np.where((c > e50) & (e20 > e50), 1, np.where((c < e50) & (e20 < e50), -1, 0))
    return pd.Series(bias, index=c.index + step - pd.Timedelta(minutes=5), name=name).to_frame()
