"""Filtres obligatoires avant toute entrée (live et backtest)."""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


class EntryFilters:
    def __init__(self, cfg: dict):
        f = cfg["filters"]
        self.max_spread = float(f["max_spread_points"])
        self.max_positions = int(f["max_positions"])
        self.min_atr = float(f["min_atr"])
        self.sessions = [tuple(map(int, s)) for s in f.get("sessions_utc", [])]
        before = pd.Timedelta(minutes=int(f.get("news_minutes_before", 30)))
        after = pd.Timedelta(minutes=int(f.get("news_minutes_after", 30)))
        self.news = [
            (pd.Timestamp(t, tz="UTC") - before, pd.Timestamp(t, tz="UTC") + after)
            for t in f.get("news_events_utc", []) or []
        ]

    # ------------------------------------------------------------- unitaires
    def in_session(self, ts: pd.Timestamp) -> bool:
        if not self.sessions:
            return True
        h = ts.hour
        return any(a <= h < b for a, b in self.sessions)

    def in_news(self, ts: pd.Timestamp) -> bool:
        return any(a <= ts <= b for a, b in self.news)

    def check(
        self,
        ts: pd.Timestamp,
        spread_points: Optional[float],
        atr_value: float,
        open_positions: int,
    ) -> tuple[bool, str]:
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        if spread_points is None or not np.isfinite(spread_points):
            return False, "spread indisponible"
        if spread_points >= self.max_spread:
            return False, f"spread {spread_points:.0f} >= {self.max_spread:.0f} pts"
        if open_positions >= self.max_positions:
            return False, f"{open_positions} position(s) déjà ouverte(s)"
        if self.in_news(ts):
            return False, "fenêtre de news"
        if not np.isfinite(atr_value) or atr_value <= self.min_atr:
            return False, f"ATR {atr_value:.2f} <= min {self.min_atr}"
        if not self.in_session(ts):
            return False, "hors session"
        return True, "ok"

    # ------------------------------------------------------------- vectorisé
    def mask(self, index: pd.DatetimeIndex, spread: np.ndarray, atr_values: np.ndarray) -> np.ndarray:
        """Masque booléen (sans la contrainte de position, gérée par le backtester)."""
        ok = np.isfinite(spread) & (spread < self.max_spread)
        ok &= np.isfinite(atr_values) & (atr_values > self.min_atr)
        if self.sessions:
            h = index.hour.values
            sess = np.zeros(len(index), dtype=bool)
            for a, b in self.sessions:
                sess |= (h >= a) & (h < b)
            ok &= sess
        for a, b in self.news:
            ok &= ~((index >= a) & (index <= b))
        return ok
