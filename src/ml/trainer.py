"""Entraînement XGBoost avec split TEMPOREL strict, purge et embargo.

    |------ train ------|purge|emb|---- validation ----|emb|---- test ----|
  * purge   : on retire du train les échantillons dont la barrière temporelle (t1)
              déborde sur la validation (sinon le label "voit" la période de validation).
  * embargo : on saute `embargo_bars` bougies au début de la validation / du test
              (autocorrélation des features).
  * StandardScaler : fit sur le train UNIQUEMENT, puis transform sur val/test.
  * Jamais de train_test_split, jamais de shuffle.
"""
from __future__ import annotations

import json
import pickle
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger
from sklearn.metrics import confusion_matrix, precision_score, recall_score
from sklearn.preprocessing import StandardScaler

from ..config import resolve
from .features import build_features, feature_columns
from .labeler import triple_barrier_labels

LABEL_TO_CLASS = {-1: 0, 0: 1, 1: 2}
CLASS_TO_LABEL = np.array([-1, 0, 1])


@dataclass
class Dataset:
    X: pd.DataFrame
    y: pd.Series
    w: pd.Series
    t1: pd.Series      # position (entière) de fin de label
    pos: pd.Series     # position (entière) de la bougie dans le df d'origine
    features: list[str] = field(default_factory=list)


@dataclass
class TrainResult:
    model: object
    scaler: StandardScaler
    features: list[str]
    metrics: dict
    metadata: dict
    importance: pd.DataFrame
    paths: dict = field(default_factory=dict)


def build_dataset(df: pd.DataFrame, cfg: dict) -> Dataset:
    ml = cfg["ml"]
    f = build_features(df)
    cols = feature_columns(f)
    lab = triple_barrier_labels(df, float(ml["barrier_atr_mult"]), int(ml["barrier_horizon"]))
    data = f[cols].copy()
    data["label"] = lab["label"]
    data["w"] = lab["sample_weight"]
    data["t1"] = lab["t1"]
    data["pos"] = np.arange(len(df))
    data = data.dropna()
    return Dataset(
        X=data[cols], y=data["label"].astype(int), w=data["w"].astype(float),
        t1=data["t1"].astype(int), pos=data["pos"].astype(int), features=cols,
    )


def temporal_split(
    ds: Dataset, index: pd.DatetimeIndex, split_time: pd.Timestamp, end_time: Optional[pd.Timestamp],
    purge_bars: int, embargo_bars: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Masques (train, val) : train < split purgé, val = [split + embargo, end)."""
    split_pos = int(index.searchsorted(split_time))
    end_pos = len(index) if end_time is None else int(index.searchsorted(end_time))
    pos, t1 = ds.pos.to_numpy(), ds.t1.to_numpy()
    train = (pos < split_pos - purge_bars) & (t1 < split_pos)          # purge
    val = (pos >= split_pos + embargo_bars) & (pos < end_pos)          # embargo
    if end_time is not None:
        val &= t1 < end_pos  # le label de validation ne doit pas déborder sur la période suivante
    return train, val


def make_model(cfg: dict, early_stopping: bool = True):
    from xgboost import XGBClassifier

    p = dict(cfg["ml"]["xgb"])
    esr = int(p.pop("early_stopping_rounds", 30))
    return XGBClassifier(
        objective="multi:softprob", num_class=3, eval_metric="mlogloss", tree_method="hist",
        early_stopping_rounds=esr if early_stopping else None, n_jobs=-1, random_state=42,
        **p,
    )


def fit_model(cfg, X_tr, y_tr, w_tr, X_val, y_val, w_val):
    classes = set(np.unique(y_tr))
    if classes != {-1, 0, 1}:
        raise ValueError(f"Le train doit contenir les 3 classes (-1, 0, +1) ; trouvé {sorted(classes)}")
    scaler = StandardScaler().fit(X_tr)  # fit sur TRAIN uniquement
    Xt, Xv = scaler.transform(X_tr), scaler.transform(X_val)
    yt = np.vectorize(LABEL_TO_CLASS.get)(y_tr)
    has_val = len(X_val) > 0 and set(np.unique(y_val)) <= {-1, 0, 1}
    model = make_model(cfg, early_stopping=has_val)
    if has_val:
        yv = np.vectorize(LABEL_TO_CLASS.get)(y_val)
        model.fit(Xt, yt, sample_weight=w_tr, eval_set=[(Xv, yv)], sample_weight_eval_set=[w_val], verbose=False)
    else:
        model.fit(Xt, yt, sample_weight=w_tr, verbose=False)
    return model, scaler


def evaluate(model, scaler, X, y) -> dict:
    if len(X) == 0:
        return {"n": 0}
    pred = CLASS_TO_LABEL[model.predict_proba(scaler.transform(X)).argmax(axis=1)]
    cm = confusion_matrix(y, pred, labels=[-1, 0, 1])
    per_class = {
        str(c): float(cm[k, k] / cm[k].sum()) if cm[k].sum() else None for k, c in enumerate([-1, 0, 1])
    }
    return {
        "n": int(len(y)),
        "accuracy": float((pred == y.to_numpy()).mean()),
        "accuracy_per_class": per_class,
        "confusion_matrix": cm.tolist(),
        "precision_+1": float(precision_score(y, pred, labels=[1], average="macro", zero_division=0)),
        "recall_+1": float(recall_score(y, pred, labels=[1], average="macro", zero_division=0)),
        "precision_-1": float(precision_score(y, pred, labels=[-1], average="macro", zero_division=0)),
        "recall_-1": float(recall_score(y, pred, labels=[-1], average="macro", zero_division=0)),
    }


def train(
    df: pd.DataFrame, cfg: dict, train_end: Optional[str] = None, val_end: Optional[str] = None,
    save: bool = True, models_dir: Optional[str] = None,
) -> TrainResult:
    ml = cfg["ml"]
    ds = build_dataset(df, cfg)
    idx = df.index
    t_end = pd.Timestamp(train_end or ml["train_end"], tz="UTC")
    v_end = pd.Timestamp(val_end or ml["val_end"], tz="UTC")
    if not (idx[0] < t_end < v_end < idx[-1]):
        # données insuffisantes pour les dates fixes : split 70 / 15 / 15 chronologique
        t_end, v_end = idx[int(len(idx) * 0.70)], idx[int(len(idx) * 0.85)]
        logger.warning(f"Dates train/val hors données → split chronologique 70/15/15 ({t_end} / {v_end})")
    purge, emb = int(ml["purge_bars"]), int(ml["embargo_bars"])
    tr, va = temporal_split(ds, idx, t_end, v_end, purge, emb)
    _, te = temporal_split(ds, idx, v_end, None, purge, emb)

    model, scaler = fit_model(cfg, ds.X[tr], ds.y[tr], ds.w[tr], ds.X[va], ds.y[va], ds.w[va])
    metrics = {
        "train": evaluate(model, scaler, ds.X[tr], ds.y[tr]),
        "validation": evaluate(model, scaler, ds.X[va], ds.y[va]),
        "test": evaluate(model, scaler, ds.X[te], ds.y[te]),  # évalué, JAMAIS entraîné dessus
    }
    importance = (
        pd.DataFrame({"feature": ds.features, "importance": model.feature_importances_})
        .sort_values("importance", ascending=False).reset_index(drop=True)
    )
    logger.info("Top 10 features :\n" + importance.head(10).to_string(index=False))
    for split in ("validation", "test"):
        m = metrics[split]
        if m.get("n"):
            logger.info(
                f"[{split}] n={m['n']} acc={m['accuracy']:.3f} P(+1)={m['precision_+1']:.3f} "
                f"R(+1)={m['recall_+1']:.3f} P(-1)={m['precision_-1']:.3f} R(-1)={m['recall_-1']:.3f}"
            )
            logger.info(f"[{split}] matrice de confusion (lignes = vrai -1/0/+1) : {m['confusion_matrix']}")

    now = datetime.now(timezone.utc)
    metadata = {
        "created_at": now.isoformat(),
        "symbol": cfg["symbol"],
        "timeframe": ml.get("timeframe", "M5"),
        "features": ds.features,
        "hyperparameters": cfg["ml"]["xgb"],
        "best_iteration": int(getattr(model, "best_iteration", -1) or -1),
        "split": {
            "data_start": str(idx[0]), "train_end": str(t_end), "val_end": str(v_end), "data_end": str(idx[-1]),
            "purge_bars": purge, "embargo_bars": emb,
        },
        "labeling": {"method": "triple_barrier", "atr_mult": ml["barrier_atr_mult"], "horizon": ml["barrier_horizon"]},
        "class_mapping": {"0": -1, "1": 0, "2": 1},
        "metrics": metrics,
    }
    res = TrainResult(model, scaler, ds.features, metrics, metadata, importance)
    if save:
        res.paths = save_model(res, models_dir or ml["models_dir"], now)
    return res


def save_model(res: TrainResult, models_dir: str, when: datetime) -> dict:
    d = resolve(models_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = when.strftime("%Y%m%d")
    model_p, scaler_p = d / f"xgb_{stamp}.json", d / f"scaler_{stamp}.pkl"
    res.model.save_model(str(model_p))
    with open(scaler_p, "wb") as fh:
        pickle.dump(res.scaler, fh)
    meta = dict(res.metadata, model_file=model_p.name, scaler_file=scaler_p.name)
    text = json.dumps(meta, indent=2, default=str)
    (d / f"metadata_{stamp}.json").write_text(text, encoding="utf-8")
    (d / "metadata.json").write_text(text, encoding="utf-8")  # pointe vers le modèle le plus récent
    imp_p = d / f"feature_importance_{stamp}.csv"
    res.importance.to_csv(imp_p, index=False)
    logger.info(f"Modèle sauvegardé : {model_p.name}, {scaler_p.name}, metadata.json")
    return {"model": str(model_p), "scaler": str(scaler_p), "metadata": str(d / "metadata.json"), "importance": str(imp_p)}
