"""Modèle IA : pas de fuite de données, validation chronologique, contrôle avant ordre."""
from datetime import datetime, timezone

import numpy as np
import pytest

pytest.importorskip("sklearn")

from analyzer.config import Config, load_config, update_config  # noqa: E402
from analyzer.data import load_data  # noqa: E402
from analyzer.ml import FEATURE_NAMES, WinModel, build_dataset, features, train_and_validate  # noqa: E402
from analyzer.quality import QualityGate  # noqa: E402
from analyzer.strategy import Analyzer  # noqa: E402
from tests.test_analyzer import synthetic  # noqa: E402
from tests.test_quality import sig_q, statuses  # noqa: E402

UTC = timezone.utc


@pytest.fixture(scope="module")
def long_history():
    cfg = update_config(Config(), {"source": "demo", "bars": 20000})
    df, _ = load_data(cfg)
    return df, cfg


def test_features_no_lookahead():
    """Les caractéristiques d'un signal ne changent pas si on ajoute des bougies futures."""
    df, cfg = synthetic(), Config()
    full = Analyzer(df, cfg)
    pats = sorted({"Avalement haussier", "Avalement baissier"})
    n = 0
    for i in range(1000, 5800, 23):
        a = full.inspect(i)
        if a is None:
            continue
        b = Analyzer(df.iloc[: i + 1], cfg).inspect(i)
        fa, fb = features(full, a, pats), features(Analyzer(df.iloc[: i + 1], cfg), b, pats)
        assert np.allclose(fa, fb, equal_nan=True), i
        n += 1
    assert n > 5
    assert len(fa) == len(FEATURE_NAMES)


def test_dataset_excludes_unfinished_trades():
    df, cfg = synthetic(), Config()
    an = Analyzer(df, cfg)
    ds = build_dataset(an, cfg)
    assert len(ds["y"]) > 50
    assert ds["idx"].max() < len(an.c) - cfg.strategy.max_hold_bars - 1
    assert set(np.unique(ds["y"])) <= {0, 1}
    assert ((ds["r"] > 0) == (ds["y"] == 1)).all()


def test_walk_forward_is_chronological(long_history):
    df, cfg = long_history
    an = Analyzer(df, cfg)
    out = train_and_validate(build_dataset(an, cfg), cfg, 7.0)
    rep = out["report"]
    P = rep["periods"]
    assert P["train"]["end"] < P["validation"]["start"] < P["validation"]["end"] < P["test"]["start"]
    assert rep["threshold"] == 0 or 0.4 <= rep["threshold"] <= 0.76
    assert rep["auc_test"] is None or 0 <= rep["auc_test"] <= 1
    for k in ("test_baseline", "test_filtered"):
        assert {"trades", "win_rate", "expectancy_r"} <= set(rep[k])
    # « utile » exige de battre la stratégie seule sur la période test
    if rep["useful"]:
        assert rep["test_filtered"]["expectancy_r"] > rep["test_baseline"]["expectancy_r"]


def test_model_persists_and_predicts(tmp_path, long_history):
    df, cfg = long_history
    m = WinModel(tmp_path)
    assert not m.ready
    m.compute(df, cfg)
    assert m.ready and (tmp_path / "ml_model.pkl").exists() and (tmp_path / "ml.json").exists()
    again = WinModel(tmp_path)
    assert again.ready
    an = Analyzer(df, cfg)
    sig = next(s for s in reversed(an.scan()))
    p1, p2 = m.predict(an, sig), again.predict(an, sig)
    assert p1 == p2 and 0 <= p1 <= 1
    ml = again.annotate(an, sig, cfg.trading)
    assert ml["pass"] == (ml["proba"] >= ml["threshold"])


def _gate(tmp_path, useful: bool):
    g = QualityGate(tmp_path, fetcher=lambda: [])
    rep = {"computed_at": datetime.now(UTC).isoformat(), "useful": useful,
           "test_baseline": {"trades": 40, "win_rate": 45}, "test_filtered": {"trades": 20, "win_rate": 58}}
    g.ml.bundle, g.ml.report = {"version": 1, "threshold": 0.55, "report": rep}, rep
    return g


def _cfg():
    cfg = Config()
    cfg.trading.quality_edge = cfg.trading.quality_news = False
    return cfg


def test_gate_blocks_low_probability_when_model_useful(tmp_path):
    g, cfg = _gate(tmp_path, useful=True), _cfg()
    low = sig_q(score=8) | {"ml": {"proba": 0.41, "threshold": 0.55, "pass": False, "useful": True}}
    high = sig_q(score=8) | {"ml": {"proba": 0.67, "threshold": 0.55, "pass": True, "useful": True}}
    ok, checks = g.check(low, cfg)
    assert not ok and statuses(checks)["ml"] == "fail"
    ok, checks = g.check(high, cfg)
    assert ok and statuses(checks)["ml"] == "ok"


def test_gate_does_not_block_when_model_not_better(tmp_path):
    g, cfg = _gate(tmp_path, useful=False), _cfg()
    low = sig_q(score=8) | {"ml": {"proba": 0.30, "threshold": 0.55, "pass": False, "useful": False}}
    ok, checks = g.check(low, cfg)
    assert ok and statuses(checks)["ml"] == "skip"
    # seuil manuel : l'utilisateur force le filtre
    cfg.trading.ml_min_proba = 0.5
    ok, checks = g.check(low, cfg)
    assert not ok and statuses(checks)["ml"] == "fail"


def test_gate_untrained_is_not_blocking(tmp_path):
    g = QualityGate(tmp_path, fetcher=lambda: [])
    ok, checks = g.check(sig_q(score=8), _cfg())
    assert ok and statuses(checks)["ml"] == "skip"


def test_ml_config_validation():
    with pytest.raises(ValueError):
        update_config(load_config("config.yaml"), {"trading": {"ml_min_proba": 0.99}})
    assert update_config(Config(), {"trading": {"ml_min_proba": 0.6}}).trading.ml_min_proba == 0.6
