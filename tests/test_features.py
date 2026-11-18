import numpy as np

from src.ml.features import BASE_FEATURES, build_features, feature_columns


def test_all_base_features_present(m5_data):
    f = build_features(m5_data)
    for c in BASE_FEATURES:
        assert c in f.columns
    assert "spread_mean20" in feature_columns(f)


def test_no_nan_after_warmup(m5_data):
    f = build_features(m5_data).iloc[300:]
    assert not f[feature_columns(f)].isna().any().any()


def test_ranges(m5_data):
    f = build_features(m5_data).dropna()
    assert f["range_pos20"].between(0, 1).all()
    assert f["rsi7"].between(0, 1).all() and f["rsi14"].between(0, 1).all()
    for c in ("hour_sin", "hour_cos", "dow_sin", "dow_cos"):
        assert f[c].between(-1, 1).all()
    assert np.isfinite(f["vwap_dist"]).all()
