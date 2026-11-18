"""Inférence live : charge le modèle le plus récent et vérifie la cohérence des features."""
from __future__ import annotations

import json
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from ..config import resolve
from .trainer import CLASS_TO_LABEL


class ModelNotFoundError(FileNotFoundError):
    pass


class FeatureMismatchError(ValueError):
    pass


class Predictor:
    def __init__(self, model, scaler, features: list[str], metadata: Optional[dict] = None):
        self.model = model
        self.scaler = scaler
        self.features = list(features)
        self.metadata = metadata or {}
        imp = getattr(model, "feature_importances_", None)
        self.importance = (
            pd.Series(imp, index=self.features).sort_values(ascending=False) if imp is not None else pd.Series(dtype=float)
        )

    # ------------------------------------------------------------------ chargement
    @classmethod
    def load_latest(cls, models_dir: str | Path) -> "Predictor":
        from xgboost import XGBClassifier

        d = resolve(models_dir)
        meta_p = d / "metadata.json"
        if not meta_p.exists():
            raise ModelNotFoundError(
                f"Aucun modèle dans {d}. Lance d'abord : python train_ml.py"
            )
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        model = XGBClassifier()
        model.load_model(str(d / meta["model_file"]))
        with open(d / meta["scaler_file"], "rb") as fh:
            scaler = pickle.load(fh)
        return cls(model, scaler, meta["features"], meta)

    @classmethod
    def from_train_result(cls, res) -> "Predictor":
        return cls(res.model, res.scaler, res.features, res.metadata)

    # ------------------------------------------------------------------ contrôles
    def check_features(self, columns) -> None:
        missing = [c for c in self.features if c not in columns]
        if missing:
            raise FeatureMismatchError(
                f"Features attendues par le modèle absentes : {missing}. Réentraîne le modèle."
            )

    def age_days(self, now: Optional[datetime] = None) -> float:
        created = self.metadata.get("created_at")
        if not created:
            return float("inf")
        now = now or datetime.now(timezone.utc)
        return (now - datetime.fromisoformat(created)).total_seconds() / 86400.0

    def top_features(self, k: int = 3) -> list[str]:
        return list(self.importance.head(k).index)

    # ------------------------------------------------------------------ inférence
    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        self.check_features(features.columns)
        X = features[self.features]
        return self.model.predict_proba(self.scaler.transform(X))

    def predict_frame(self, features: pd.DataFrame) -> pd.DataFrame:
        """Prédictions vectorisées (backtest). Les lignes avec NaN donnent pred=0, prob=NaN."""
        out = pd.DataFrame(index=features.index, data={"prediction": 0, "probability": np.nan})
        self.check_features(features.columns)
        ok = features[self.features].notna().all(axis=1)
        if ok.any():
            proba = self.predict_proba(features.loc[ok])
            out.loc[ok, "prediction"] = CLASS_TO_LABEL[proba.argmax(axis=1)]
            out.loc[ok, "probability"] = proba.max(axis=1)
        return out

    def predict(self, features_row: pd.DataFrame) -> tuple[int, float]:
        """(prediction ∈ {-1,0,1}, probability = max softmax) pour UNE ligne."""
        row = features_row.iloc[[-1]] if len(features_row) > 1 else features_row
        self.check_features(row.columns)
        if row[self.features].isna().any(axis=1).iloc[0]:
            return 0, 0.0
        proba = self.predict_proba(row)[0]
        k = int(proba.argmax())
        return int(CLASS_TO_LABEL[k]), float(proba[k])
