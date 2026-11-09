"""Version 2 du signal: modele ML entraine en walk-forward.

Contraintes non negociables:
  - chaque prediction n'utilise que des donnees anterieures a la bougie predite
    (entrainnement glissant), sinon le backtest raconte n'importe quoi ;
  - les bougies dont l'horizon de label déborde sur l'evaluateur suivant sont
    supprimees a la frontiere: sans ca le futur fuit par les labels ;
  - le modele ne predit pas "up/down" en general mais un franchissement de seuil
    en ATR, sinon 50.6% de haussier (mesure sur ces donnees) donne un AUC
    flatteur sur du bruit.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from ..config import get, resolve_path
from ..features import FEATURE_COLS, compute_features, drop_warmed_up

MIN_LABEL_ATR = 0.3


def make_labels(scored: pd.DataFrame, cfg: dict) -> pd.Series:
    """1 = mouvement franchement haussier, 0 = franchement baissier, NaN = ambigue."""
    horizon = int(get(cfg, "signal.ml.horizon_bars", 12))
    edge = MIN_LABEL_ATR * scored["atr"] / scored["close"]
    fwd = scored["fwd_ret"]
    return pd.Series(np.where(fwd > edge, 1.0, np.where(fwd < -edge, 0.0, np.nan)), index=scored.index)


def feature_columns(scored: pd.DataFrame, cfg: dict) -> list[str]:
    extra = get(cfg, "signal.ml.extra_features", []) or []
    cols = [c for c in FEATURE_COLS + list(extra) if c in scored.columns]
    return cols


def train_model(X: pd.DataFrame, y: pd.Series) -> HistGradientBoostingClassifier:
    model = HistGradientBoostingClassifier(
        max_iter=260, learning_rate=0.06, max_leaf_nodes=31, min_samples_leaf=120,
        l2_regularization=1.0, early_stopping=True, validation_fraction=0.15,
        random_state=7,
    )
    model.fit(X, y)
    return model


def walk_forward(scored: pd.DataFrame, cfg: dict, months: int = 3, quiet: bool = False) -> pd.DataFrame:
    """Predict chaque bougie avec un modele entraine uniquement sur le passe."""
    horizon = int(get(cfg, "signal.ml.horizon_bars", 12))
    cols = feature_columns(scored, cfg)
    labels = make_labels(scored, cfg)
    out = scored.copy()
    out["ml_prob_up"] = np.nan
    out["ml_label"] = labels

    times = scored["time"]
    start = times.iloc[0] + pd.DateOffset(months=months)
    folds = pd.date_range(start, times.iloc[-1], freq=f"{months}MS", tz="UTC")
    n_done = 0
    for fold_start in folds:
        fold_end = fold_start + pd.DateOffset(months=months)
        # Marge de securite: les `horizon` bougies avant fold_end ont un label
        # qui chevauche la periode d'evaluation.
        train_mask = (times < fold_start - pd.Timedelta(minutes=horizon * 5))
        eval_mask = (times >= fold_start) & (times < fold_end)
        valid = train_mask & labels.notna()
        if valid.sum() < 6000 or eval_mask.sum() < 200:
            continue
        X = scored.loc[valid, cols]
        y = labels.loc[valid]
        model = train_model(X, y)
        prob = model.predict_proba(scored.loc[eval_mask, cols])[:, 1]
        out.loc[eval_mask, "ml_prob_up"] = prob
        n_done += 1
        if not quiet:
            train_end = times[train_mask].max()
            yhat = (prob >= 0.5).astype(float)
            realized = labels.loc[eval_mask]
            known = realized.notna().to_numpy()
            auc = None
            if known.sum() > 100 and len(np.unique(realized.dropna())) == 2:
                auc = round(float(roc_auc_score(realized.dropna(), prob[known])), 4)
            print(f"  fold {fold_start:%Y-%m}: train<{str(train_end)[:10]} "
                  f"({int(valid.sum())} lignes) eval={int(eval_mask.sum())} bougies AUC={auc}", flush=True)
    print(f"  {n_done} folds walk-forward")
    return out


def model_path(cfg: dict) -> Path:
    return resolve_path(get(cfg, "signal.ml.model_dir", "data/models"))


def fit_and_save(cfg: dict, scored: pd.DataFrame) -> dict:
    """Entrainne sur tout l'historique et persiste pour le live."""
    cols = feature_columns(scored, cfg)
    labels = make_labels(scored, cfg)
    train = drop_warmed_up(scored, 220)
    sub = train.loc[labels.loc[train.index].notna()]
    model = train_model(sub[cols], labels.loc[sub.index])
    folder = model_path(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    meta = {
        "features": cols,
        "horizon_bars": int(get(cfg, "signal.ml.horizon_bars", 12)),
        "label_min_atr": MIN_LABEL_ATR,
        "trained_rows": int(len(sub)),
        "trained_until": str(sub["time"].max()),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "sklearn": __import__("sklearn").__version__,
    }
    joblib.dump({"model": model, "meta": meta}, folder / "gold_m5.joblib")
    (folder / "gold_m5.meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def load(cfg: dict):
    path = model_path(cfg) / "gold_m5.joblib"
    if not path.exists():
        return None, None
    blob = joblib.load(path)
    return blob["model"], blob["meta"]


def add_ml_columns(scored: pd.DataFrame, cfg: dict, walk_forward_mode: bool = True) -> pd.DataFrame:
    """Colonnes ml_side / ml_conf / ml_fire pour le backtester et le live.

    walk_forward_mode=True  -> predictions honnetes pour evaluer la performance.
    walk_forward_mode=False -> modele final, pour le live.
    """
    out = scored.copy()
    if walk_forward_mode:
        if "ml_prob_up" not in out or out["ml_prob_up"].isna().all():
            out = walk_forward(out, cfg)
    else:
        model, meta = load(cfg)
        if model is None:
            raise RuntimeError("Modele absent: lancer `python bot.py train` d'abord.")
        cols = meta["features"]
        out["ml_prob_up"] = model.predict_proba(out[cols])[:, 1]

    thr = float(get(cfg, "signal.ml.prob_threshold", 0.58))
    p = out["ml_prob_up"]
    out["ml_side"] = np.where(p >= thr, "BUY", np.where(p <= 1 - thr, "SELL", "FLAT"))
    out["ml_conf"] = np.round(np.where(p >= 0.5, p, 1 - p) * 100, 1)
    out["ml_state"] = out["ml_side"] != "FLAT"
    from .rules import _apply_cooldown, gates

    g = gates(out, cfg)
    out["allowed"] = out["allowed"] if "allowed" in out else g["allowed"]
    state = out["ml_state"] & g["allowed"]
    previous = state.shift(1).fillna(True).astype(bool)
    cooldown = int(get(cfg, "signal.cooldown_bars", 6))
    fire = state & ~previous
    out["ml_fire"] = _apply_cooldown(fire, max(1, cooldown)) if cooldown > 1 else fire
    return out


def cmd_train(cfg: dict, args) -> int:
    from ..data.bars import closed_only
    from ..data.history import load_cache

    tf = str(get(cfg, "symbol.timeframe", "M5"))
    df = closed_only(load_cache(cfg, args.symbol, tf), tf)
    scored = compute_features(df, cfg)
    print("walk-forward...")
    scored = walk_forward(scored, cfg, months=args.months)
    thr = float(get(cfg, "signal.ml.prob_threshold", 0.58))
    known = scored["ml_label"].notna() & scored["ml_prob_up"].notna()
    sub = scored[known]
    pred = np.where(sub["ml_prob_up"] >= thr, 1.0, np.where(sub["ml_prob_up"] <= 1 - thr, 0.0, np.nan))
    hit = sub["ml_label"].to_numpy() == pred
    bins = pd.cut(sub["ml_prob_up"], [0, .2, .35, .45, .55, .65, .8, 1.0])
    calib = {
        str(band): {"n": int(len(grp)), "realized_up": round(float(grp["ml_label"].mean()), 3)}
        for band, grp in sub.groupby(bins, observed=True)
    }
    report = {
        "evaluated_bars": int(known.sum()),
        "covered_by_threshold": int(np.isfinite(pred).sum()),
        "coverage_pct": round(float(np.isfinite(pred).mean() * 100), 2),
        "auc": round(float(roc_auc_score(sub["ml_label"], sub["ml_prob_up"])), 4),
        "hit_rate_when_fires": round(float(hit[np.isfinite(pred)].mean() * 100), 2) if np.isfinite(pred).any() else None,
        "baseline_hit_rate": round(float(max((sub["ml_label"] == 1).mean(), (sub["ml_label"] == 0).mean()) * 100), 2),
        "probability_calibration": calib,
        "threshold": thr,
    }
    print(json.dumps(report, indent=2, default=str))
    if not args.no_save:
        print("entrainement final sur tout l'historique...")
        meta = fit_and_save(cfg, scored)
        print(json.dumps(meta, indent=2, default=str))
    return 0
