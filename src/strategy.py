"""Stratégies : interface commune BaseStrategy + deux stratégies à règles.

Contrat anti look-ahead :
  * `prepare(df)` calcule des colonnes causales (valeur en N = f(bougies <= N)).
  * la colonne `signal` en N est connue à la CLÔTURE de N ; l'entrée se fait
    au plus tôt à l'ouverture de N+1 (backtester) ou au prix courant (live).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass
class Signal:
    direction: int               # +1 long, -1 short
    time: pd.Timestamp
    price: float                 # close de la bougie signal
    atr: float
    reason: str
    confidence: Optional[float] = None
    extra: dict = field(default_factory=dict)

    @property
    def side(self) -> str:
        return "BUY" if self.direction > 0 else "SELL"


class BaseStrategy(ABC):
    name: str = "base"

    def __init__(self, params: dict, atr_period: int = 14):
        self.params = dict(params)
        self.timeframe: str = self.params.get("timeframe", "M5")
        self.atr_period = atr_period

    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Renvoie df + colonnes 'atr', 'signal' (+1/-1/0) et 'reason'."""

    def generate_signal(self, df: pd.DataFrame) -> Optional[Signal]:
        """Signal sur la DERNIÈRE bougie clôturée de df (df ne doit contenir que des bougies closes)."""
        if df is None or len(df) < self.min_bars():
            return None
        d = self.prepare(df)
        last = d.iloc[-1]
        sig = int(last.get("signal", 0) or 0)
        if sig == 0 or not np.isfinite(last["atr"]):
            return None
        return Signal(
            direction=sig,
            time=d.index[-1],
            price=float(last["close"]),
            atr=float(last["atr"]),
            reason=str(last.get("reason", self.name)),
            confidence=float(last["confidence"]) if "confidence" in d and pd.notna(last.get("confidence")) else None,
        )

    def min_bars(self) -> int:
        return 50

    def with_params(self, **overrides) -> "BaseStrategy":
        p = dict(self.params)
        p.update(overrides)
        return type(self)(p, self.atr_period)


class MeanReversionStrategy(BaseStrategy):
    """A. RSI(7) + Bollinger(20,2) sur M1 : achat si RSI < seuil bas ET close < bande basse."""

    name = "mean_reversion"

    def min_bars(self) -> int:
        return max(int(self.params.get("bb_period", 20)), int(self.params.get("rsi_period", 7))) + 30

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        d = df.copy()
        d["rsi"] = ind.rsi(d["close"], int(p.get("rsi_period", 7)))
        lo, mid, up = ind.bollinger(d["close"], int(p.get("bb_period", 20)), float(p.get("bb_std", 2.0)))
        d["bb_low"], d["bb_mid"], d["bb_up"] = lo, mid, up
        d["atr"] = ind.atr(d, self.atr_period)
        rsi_low = float(p.get("rsi_low", 20))
        rsi_high = float(p.get("rsi_high", 100 - rsi_low))
        long_ = (d["rsi"] < rsi_low) & (d["close"] < d["bb_low"])
        short = (d["rsi"] > rsi_high) & (d["close"] > d["bb_up"])
        d["signal"] = np.where(long_, 1, np.where(short, -1, 0))
        d["reason"] = np.where(
            long_, "MR long: RSI bas + close < BB basse",
            np.where(short, "MR short: RSI haut + close > BB haute", ""),
        )
        return d


class TrendFollowingStrategy(BaseStrategy):
    """B. Croisement EMA(9)/EMA(21) filtré par EMA(100) + ATR(14) > seuil, sur M5."""

    name = "trend_following"

    def min_bars(self) -> int:
        return int(self.params.get("ema_filter", 100)) + 30

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        d = df.copy()
        fast = ind.ema(d["close"], int(p.get("ema_fast", 9)))
        slow = ind.ema(d["close"], int(p.get("ema_slow", 21)))
        filt = ind.ema(d["close"], int(p.get("ema_filter", 100)))
        d["ema_fast"], d["ema_slow"], d["ema_filter"] = fast, slow, filt
        d["atr"] = ind.atr(d, int(p.get("atr_period", self.atr_period)))
        diff = fast - slow
        cross_up = (diff > 0) & (diff.shift(1) <= 0)
        cross_dn = (diff < 0) & (diff.shift(1) >= 0)
        vol_ok = d["atr"] > float(p.get("atr_threshold", 1.0))
        long_ = cross_up & (d["close"] > filt) & vol_ok
        short = cross_dn & (d["close"] < filt) & vol_ok
        d["signal"] = np.where(long_, 1, np.where(short, -1, 0))
        d["reason"] = np.where(
            long_, "TF long: EMA9>EMA21, close>EMA100, ATR ok",
            np.where(short, "TF short: EMA9<EMA21, close<EMA100, ATR ok", ""),
        )
        return d


def build_strategy(cfg: dict, name: Optional[str] = None, predictor=None) -> BaseStrategy:
    name = name or cfg["strategy"]["name"]
    params = dict(cfg["strategy"].get(name, {}))
    atr_period = int(cfg["risk"].get("atr_period", 14))
    if name == "mean_reversion":
        return MeanReversionStrategy(params, atr_period)
    if name == "trend_following":
        return TrendFollowingStrategy(params, atr_period)
    if name == "ml_xgboost":
        from .ml.strategy_ml import MLStrategy

        return MLStrategy(params, atr_period, cfg=cfg, predictor=predictor)
    raise ValueError(f"Stratégie inconnue : {name}")
