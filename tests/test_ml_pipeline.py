import json
from datetime import datetime, timedelta, timezone

import pytest

from src.ml.features import build_features
from src.ml.predictor import FeatureMismatchError, Predictor
from src.ml.strategy_ml import MLStrategy, StaleModelError
from src.ml.trainer import train
from src.strategy import build_strategy


@pytest.fixture(scope="module")
def trained(tmp_path_factory, m5_data):
    import copy
    from tests.conftest import _BASE_CFG

    c = copy.deepcopy(_BASE_CFG)
    c["ml"]["models_dir"] = str(tmp_path_factory.mktemp("models"))
    c["ml"]["xgb"]["n_estimators"] = 60
    res = train(m5_data, c)
    return c, res


def test_training_saves_artifacts(trained):
    c, res = trained
    meta = json.loads(open(res.paths["metadata"], encoding="utf-8").read())
    assert meta["features"] == res.features
    assert meta["model_file"].startswith("xgb_") and meta["scaler_file"].startswith("scaler_")
    assert {"train", "validation", "test"} <= set(meta["metrics"])
    assert "precision_+1" in meta["metrics"]["validation"]
    assert len(open(res.paths["importance"]).read().splitlines()) == len(res.features) + 1


def test_predictor_load_and_predict(trained, m5_data):
    c, _ = trained
    p = Predictor.load_latest(c["ml"]["models_dir"])
    pred, prob = p.predict(build_features(m5_data).iloc[[-1]])
    assert pred in (-1, 0, 1) and 1 / 3 <= prob <= 1


def test_feature_mismatch_raises(trained, m5_data):
    c, _ = trained
    p = Predictor.load_latest(c["ml"]["models_dir"])
    with pytest.raises(FeatureMismatchError):
        p.predict(build_features(m5_data).drop(columns=["rsi7"]).iloc[[-1]])


def test_min_confidence_blocks_signals(trained, m5_data):
    c, _ = trained
    c = dict(c, ml=dict(c["ml"], min_confidence=0.999))
    s = build_strategy(c, "ml_xgboost")
    assert (s.prepare(m5_data)["signal"] == 0).all()
    assert s.generate_signal(m5_data) is None  # pas de repli vers une règle


def test_stale_model_refused_on_real_allowed_on_demo(trained):
    c, res = trained
    p = Predictor(res.model, res.scaler, res.features,
                  {"created_at": (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()})
    s = MLStrategy({"timeframe": "M5"}, 14, c, p)
    s.check_ready(is_demo_account=True)  # warning seulement
    with pytest.raises(StaleModelError):
        s.check_ready(is_demo_account=False)


def test_ml_strategy_backtests(trained, m5_data):
    from src.backtester import run_backtest

    c, _ = trained
    res = run_backtest(build_strategy(c, "ml_xgboost"), m5_data, c)
    assert res.metrics["trades"] >= 0 and len(res.equity) > 0
