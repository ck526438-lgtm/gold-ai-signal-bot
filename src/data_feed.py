"""Données de marché : bougies CLÔTURÉES uniquement (anti look-ahead)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from .config import resolve
from .mt5_connector import TIMEFRAMES

COLUMNS = ["open", "high", "low", "close", "tick_volume", "spread"]


def rates_to_df(rates) -> pd.DataFrame:
    df = pd.DataFrame(rates)
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time").sort_index()
    if "spread" not in df:
        df["spread"] = np.nan
    return df[[c for c in COLUMNS if c in df.columns]].astype(float)


def drop_unclosed(df: pd.DataFrame, timeframe: str, now: Optional[datetime] = None) -> pd.DataFrame:
    """Supprime la dernière bougie si elle n'est pas encore clôturée."""
    if df.empty:
        return df
    now = now or datetime.now(timezone.utc)
    minutes = TIMEFRAMES[timeframe]
    closes_at = df.index + pd.Timedelta(minutes=minutes)
    return df[closes_at <= pd.Timestamp(now)]


def resample(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """M1 -> M5/M15… ; ne garde que les bougies complètes."""
    minutes = TIMEFRAMES[timeframe]
    if minutes == 1:
        return df
    rule = f"{minutes}min"
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "tick_volume": "sum"}
    if "spread" in df:
        agg["spread"] = "mean"
    out = df.resample(rule, label="left", closed="left").agg(agg)
    counts = df["close"].resample(rule, label="left", closed="left").count()
    # une bougie agrégée est "complète" si le bloc suivant a commencé (ou si elle est pleine)
    out = out[counts > 0].dropna(subset=["open", "close"])
    last_full = counts.reindex(out.index).iloc[-1] if len(out) else 0
    if len(out) and last_full < minutes:
        out = out.iloc[:-1]
    return out


def load_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(resolve(path))
    tcol = "time" if "time" in df.columns else df.columns[0]
    if pd.api.types.is_numeric_dtype(df[tcol]):
        df[tcol] = pd.to_datetime(df[tcol], unit="s", utc=True)
    else:
        df[tcol] = pd.to_datetime(df[tcol], utc=True)
    df = df.set_index(tcol).sort_index()
    df.index.name = "time"
    if "tick_volume" not in df:
        df["tick_volume"] = df.get("volume", 1.0)
    if "spread" not in df:
        df["spread"] = np.nan
    return df[COLUMNS].astype(float)


class DataFeed:
    def __init__(self, connector, cfg: dict):
        self.connector = connector
        self.cfg = cfg
        self.symbol = cfg["symbol"]

    def get_closed_bars(self, timeframe: str, count: int = 500) -> pd.DataFrame:
        """start_pos=1 : on saute la bougie 0 (en cours de formation)."""
        mt5 = self.connector.mt5
        rates = mt5.copy_rates_from_pos(self.symbol, self.connector.timeframe(timeframe), 1, count)
        if rates is None or len(rates) == 0:
            logger.warning(f"Aucune bougie reçue ({mt5.last_error()})")
            return pd.DataFrame(columns=COLUMNS)
        return drop_unclosed(rates_to_df(rates), timeframe)

    def get_history(self, timeframe: str, start: str, end: str) -> pd.DataFrame:
        mt5 = self.connector.mt5
        t0 = pd.Timestamp(start, tz="UTC").to_pydatetime()
        t1 = pd.Timestamp(end, tz="UTC").to_pydatetime()
        rates = mt5.copy_rates_range(self.symbol, self.connector.timeframe(timeframe), t0, t1)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"Historique vide pour {self.symbol} {timeframe} : {mt5.last_error()}")
        return drop_unclosed(rates_to_df(rates), timeframe)


def load_history(cfg: dict, connector_factory=None) -> pd.DataFrame:
    """Charge l'historique M1 depuis un CSV (si fourni) ou depuis MT5."""
    bt = cfg["backtest"]
    if bt.get("data_csv"):
        df = load_csv(bt["data_csv"])
        return df.loc[bt["start"]: bt["end"]] if bt.get("start") else df
    from .mt5_connector import MT5Connector

    conn = connector_factory() if connector_factory else MT5Connector(cfg)
    if not conn.connect():
        raise RuntimeError("Connexion MT5 impossible pour charger l'historique.")
    try:
        return DataFeed(conn, cfg).get_history(bt.get("timeframe_source", "M1"), bt["start"], bt["end"])
    finally:
        conn.shutdown()
