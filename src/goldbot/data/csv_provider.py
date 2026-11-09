"""Provider sur fichier CSV: sert au travail hors ligne et au transfert Kali manuel.

Colonnes acceptees: time,open,high,low,close[,tick_volume][,spread_points]
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from ..config import get, resolve_path
from .bars import normalize


class CSVProvider:
    name = "csv"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._df: pd.DataFrame | None = None
        self.symbol = str(get(cfg, "symbol.candidates", ["XAUUSD"])[0])

    def connect(self) -> dict:
        path = resolve_path(get(self.cfg, "data.csv_path", "data/manual/xauusd_m5.csv"))
        if not Path(path).exists():
            raise FileNotFoundError(f"CSV introuvable: {path}")
        self._df = normalize(pd.read_csv(path))
        return {"provider": "csv", "path": str(path), "bars": int(len(self._df)),
                "first": str(self._df["time"].iloc[0]) if len(self._df) else None,
                "last": str(self._df["time"].iloc[-1]) if len(self._df) else None}

    def _frame(self) -> pd.DataFrame:
        if self._df is None:
            self.connect()
        return self._df

    def bars_range(self, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        def utc(value: datetime) -> pd.Timestamp:
            ts = pd.Timestamp(value)
            return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

        df = self._frame()
        mask = (df["time"] >= utc(start)) & (df["time"] <= utc(end))
        return df[mask].reset_index(drop=True)

    def bars_count(self, timeframe: str, count: int, start_pos: int = 0) -> pd.DataFrame:
        df = self._frame()
        end = len(df) - start_pos
        return df[max(0, end - count):end].reset_index(drop=True)

    def tick(self) -> dict:
        df = self._frame()
        if df.empty:
            return {}
        last = df.iloc[-1]
        return {"symbol": self.symbol, "bid": float(last["close"]), "ask": float(last["close"]),
                "spread_points": float(last["spread_points"]) if pd.notna(last["spread_points"]) else 0.0,
                "time": last["time"].isoformat()}

    def point(self) -> float:
        return 0.01

    def terminal_info(self) -> dict:
        return {"provider": "csv"}

    def shutdown(self) -> None:
        self._df = None
