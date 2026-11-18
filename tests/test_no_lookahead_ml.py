"""CRITIQUE — anti look-ahead du pipeline ML."""
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.ml.features import build_features, feature_columns
from src.ml.trainer import build_dataset, fit_model, temporal_split

ROOT = Path(__file__).resolve().parent.parent


# 1. Aucune feature ne contient de shift(-k)
def test_no_negative_shift_in_feature_code():
    for f in ("src/ml/features.py", "src/indicators.py", "src/ml/strategy_ml.py", "src/ml/predictor.py"):
        code = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"shift\(\s*-", code), f"shift négatif dans {f}"
        assert "center=True" not in code, f"fenêtre centrée dans {f}"


# 2. Modifier les bougies i+1 … i+100 ne change PAS les features en i
@pytest.mark.parametrize("i", [400, 900, 1500])
def test_future_perturbation_does_not_change_features(m5_data, i):
    df = m5_data.iloc[:2000].copy()
    base = build_features(df)
    pert = df.copy()
    rng = np.random.default_rng(i)
    sl = slice(i + 1, i + 101)
    factor = 1 + rng.normal(0, 0.02, (100, 1))
    pert.iloc[sl, :4] = pert.iloc[sl, :4].to_numpy() * factor
    pert.iloc[sl, pert.columns.get_loc("tick_volume")] *= 7
    pert.iloc[sl, pert.columns.get_loc("spread")] += 30
    after = build_features(pert)
    cols = feature_columns(base)
    pd.testing.assert_frame_equal(base[cols].iloc[: i + 1], after[cols].iloc[: i + 1])
    assert not base[cols].iloc[i + 1: i + 101].equals(after[cols].iloc[i + 1: i + 101])  # le test a bien perturbé


def test_features_on_truncated_equal_full(m5_data):
    df = m5_data.iloc[:1500]
    full = build_features(df)
    for n in (500, 1000, 1499):
        trunc = build_features(df.iloc[: n + 1])
        cols = feature_columns(full)
        np.testing.assert_allclose(full[cols].iloc[n].to_numpy(float), trunc[cols].iloc[-1].to_numpy(float), equal_nan=True)


# 3. Le scaler est fit sur le train uniquement
def test_scaler_fit_on_train_only(cfg, m5_data):
    ds = build_dataset(m5_data, cfg)
    idx = m5_data.index
    split = idx[int(len(idx) * 0.7)]
    tr, va = temporal_split(ds, idx, split, None, 15, 15)
    X_va = ds.X[va] * 3 + 10  # stats de validation volontairement très différentes
    model, scaler = fit_model(cfg, ds.X[tr], ds.y[tr], ds.w[tr], X_va, ds.y[va], ds.w[va])
    np.testing.assert_allclose(scaler.mean_, ds.X[tr].mean().to_numpy(), rtol=1e-9)
    np.testing.assert_allclose(scaler.var_, ds.X[tr].var(ddof=0).to_numpy(), rtol=1e-6)
    assert not np.allclose(scaler.mean_, X_va.mean().to_numpy())
    assert scaler.n_samples_seen_ == int(tr.sum())


def test_purge_and_embargo(cfg, m5_data):
    ds = build_dataset(m5_data, cfg)
    idx = m5_data.index
    split_pos = int(len(idx) * 0.6)
    tr, va = temporal_split(ds, idx, idx[split_pos], None, purge_bars=15, embargo_bars=15)
    assert (ds.t1[tr] < split_pos).all()               # aucun label du train ne déborde sur la validation
    assert (ds.pos[tr] < split_pos - 15).all()
    assert (ds.pos[va] >= split_pos + 15).all()        # embargo
    assert not (set(ds.pos[tr]) & set(ds.pos[va]))


def test_no_shuffle_split_in_code():
    for f in (ROOT / "src").rglob("*.py"):
        code = f.read_text(encoding="utf-8")
        assert not re.search(r"train_test_split\s*\(|import\s+train_test_split", code), f"train_test_split interdit ({f.name})"
        assert not re.search(r"shuffle\s*=\s*True", code)
