"""MLStrategy : XGBoost exposé via l'interface BaseStrategy (backtest, WFA et live identiques).

* SL/TP : hérités du risk_manager (1,5 × ATR, TP = 2 × SL) — aucun override.
* Filtres existants conservés (appliqués par main.py / backtester).
* Pas de signal si probability <= min_confidence (aucun repli vers une règle).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from .. import indicators as ind
from ..strategy import BaseStrategy, Signal
from .features import build_features
from .predictor import Predictor


class StaleModelError(RuntimeError):
    pass


class MLStrategy(BaseStrategy):
    name = "ml_xgboost"

    def __init__(self, params: dict, atr_period: int = 14, cfg: Optional[dict] = None, predictor: Optional[Predictor] = None):
        super().__init__(params, atr_period)
        self.cfg = cfg or {}
        ml = self.cfg.get("ml", {})
        self.timeframe = params.get("timeframe", ml.get("timeframe", "M5"))
        self.min_confidence = float(ml.get("min_confidence", 0.60))
        self.max_age_days = float(ml.get("max_model_age_days", 90))
        self._predictor = predictor

    @property
    def predictor(self) -> Predictor:
        if self._predictor is None:
            self._predictor = Predictor.load_latest(self.cfg.get("ml", {}).get("models_dir", "src/models"))
        return self._predictor

    def with_params(self, **overrides):
        p = dict(self.params)
        p.update(overrides)
        return MLStrategy(p, self.atr_period, self.cfg, self._predictor)

    def min_bars(self) -> int:
        return 260  # EMA(200) + marge

    def check_ready(self, is_demo_account: bool) -> None:
        """Modèle > 90 jours : warning ; refus de trader si compte réel (autorisé en démo)."""
        age = self.predictor.age_days()
        if age > self.max_age_days:
            msg = f"Modèle ML vieux de {age:.0f} jours (> {self.max_age_days:.0f}) : réentraîne-le."
            if not is_demo_account:
                raise StaleModelError(msg + " Trading réel refusé.")
            logger.warning(msg + " (autorisé car compte démo)")

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        d = df.copy()
        d["atr"] = ind.atr(d, self.atr_period)
        feats = build_features(df)
        pred = self.predictor.predict_frame(feats)
        d["prediction"] = pred["prediction"].astype(int)
        d["confidence"] = pred["probability"]
        go = (d["prediction"] != 0) & (d["confidence"] > self.min_confidence)
        d["signal"] = np.where(go, d["prediction"], 0)
        d["reason"] = np.where(
            go, "ML " + np.where(d["prediction"] > 0, "long", "short") + " conf=" + d["confidence"].round(2).astype(str), ""
        )
        return d

    def generate_signal(self, df: pd.DataFrame) -> Optional[Signal]:
        if df is None or len(df) < self.min_bars():
            return None
        feats = build_features(df).iloc[[-1]]
        pred, prob = self.predictor.predict(feats)
        atr_val = float(ind.atr(df, self.atr_period).iloc[-1])
        logger.info(
            f"ML inférence {df.index[-1]} pred={pred:+d} conf={prob:.3f} top3={self.predictor.top_features(3)}"
        )
        if pred == 0 or prob <= self.min_confidence or not np.isfinite(atr_val):
            return None  # pas de fallback vers une règle
        return Signal(
            direction=pred, time=df.index[-1], price=float(df["close"].iloc[-1]), atr=atr_val,
            reason=f"ML {'long' if pred > 0 else 'short'} (conf {prob:.0%})", confidence=prob,
        )
