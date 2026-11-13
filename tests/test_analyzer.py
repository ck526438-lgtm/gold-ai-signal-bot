import numpy as np
import pandas as pd
import pytest

from analyzer import patterns
from analyzer.backtest import run_backtest
from analyzer.config import Config, update_config
from analyzer.strategy import Analyzer


def synthetic(n=6000, seed=1):
    """Série XAUUSD M5 simulée : tendances alternées + volatilité intrajournalière."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-06-01", periods=n, freq="5min", tz="UTC")
    hours = idx.hour.to_numpy()
    vol = np.where((hours >= 7) & (hours < 20), 1.8, 0.8)   # $ par bougie M5
    drift = np.repeat(rng.choice([-1, 1], n // 400 + 1), 400)[:n] * 0.25
    close = 4000.0 + np.cumsum(drift + rng.normal(0, 1, n) * vol)
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, 1, (2, n))) * vol
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + wick[0],
                         "low": np.minimum(open_, close) - wick[1], "close": close,
                         "volume": 100.0}, index=idx)


def test_bullish_engulfing():
    o = np.array([1.1, 1.1, 1.1010, 1.1000])
    c = np.array([1.1, 1.1, 1.1000, 1.1015])
    h, l = np.maximum(o, c) + 0.0001, np.minimum(o, c) - 0.0001
    assert "Avalement haussier" in [p.name for p in patterns.detect(o, h, l, c, 3, atr=0.0010)]


def test_hammer():
    o = np.array([1.1, 1.1, 1.1, 1.10100])
    c = np.array([1.1, 1.1, 1.1, 1.10110])
    h = np.array([1.1, 1.1, 1.1, 1.10115])
    l = np.array([1.1, 1.1, 1.1, 1.10000])
    found = patterns.detect(o, h, l, c, 3, atr=0.0008)
    assert any(p.direction == 1 and "marteau" in p.name for p in found)


def test_pattern_names_complete():
    assert len(set(patterns.PATTERN_NAMES)) == 12


def test_no_lookahead():
    """Un signal à la bougie i ne doit pas changer si on ajoute des bougies futures."""
    df, cfg = synthetic(), Config()
    full = Analyzer(df, cfg)
    for i in range(1000, 5800, 37):
        a, b = full.inspect(i), Analyzer(df.iloc[: i + 1], cfg).inspect(i)
        assert (a is None) == (b is None), i
        if a:
            assert (a.entry, a.sl, a.score, a.blocked, a.mtf) == (b.entry, b.sl, b.score, b.blocked, b.mtf)


def test_signal_risk_rules():
    cfg = Config()
    sigs = Analyzer(synthetic(), cfg).scan()
    assert sigs, "aucun signal sur 6000 bougies"
    for s in sigs:
        assert s.blocked is None and s.score >= cfg.strategy.min_score
        assert cfg.strategy.min_sl_pips - 0.01 <= s.risk_pips <= cfg.strategy.max_sl_pips
        if s.direction == 1:
            assert s.sl < s.entry < s.tp1 <= s.tp2
        else:
            assert s.sl > s.entry > s.tp1 >= s.tp2
        assert sum(f.points for f in s.factors) == pytest.approx(s.score, abs=0.01) or s.score in (0, 10)


def test_disabled_patterns():
    cfg = Config()
    an = Analyzer(synthetic(), cfg)
    first = an.scan()[0].pattern
    cfg.strategy.disabled_patterns = [first]
    assert all(s.pattern != first for s in Analyzer(synthetic(), cfg).scan())


def test_backtest():
    res = run_backtest(synthetic(), Config())
    st = res["stats"]
    assert st["trades"] > 0 and len(res["equity"]) == st["trades"]
    assert set(st["exits"]) <= {"SL", "BE", "TP2", "temps", "suiveur", "retournement"}
    assert sum(g["trades"] for g in res["by_pattern"]) == st["trades"]


def test_settings_validation():
    cfg = Config()
    assert update_config(cfg, {"strategy": {"min_score": "7.5"}}).strategy.min_score == 7.5
    assert cfg.strategy.min_score == 6.0  # l'original n'est pas modifié
    with pytest.raises(ValueError):
        update_config(cfg, {"strategy": {"rr_tp1": 3, "rr_tp2": 2}})
    with pytest.raises(ValueError):
        update_config(cfg, {"source": "inconnue"})


def test_management_compared_to_classic():
    from analyzer.backtest import Management, simulate
    cfg = Config()
    res = run_backtest(synthetic(), cfg)
    m = res["management"]
    assert m["rules"]["active"] and {"robot", "classic"} <= set(m)
    assert m["robot"]["trades"] == res["stats"]["trades"]
    cfg.trading.trailing = cfg.trading.reversal_exit = False
    res2 = run_backtest(synthetic(), cfg)
    assert res2["management"]["robot"] == res2["management"]["classic"]
    # un stop suiveur ne peut pas transformer un trade en perte plus grande que 1 R (+ spread)
    an = Analyzer(synthetic(), Config())
    mg = Management(trailing=True, reversal_exit=True)
    for sig in an.scan()[:80]:
        out = simulate(an, sig, 3.3, 48, mg)
        assert out.result_r >= -1.2
        if out.status == "suiveur":
            assert out.result_r > 0


def test_low_score_backtest_weights_risk():
    from analyzer.config import analysis_config
    cfg = Config()
    cfg.trading.low_score_enabled, cfg.trading.low_score_min, cfg.trading.low_score_risk_factor = True, 5.0, 0.5
    res = run_backtest(synthetic(), analysis_config(cfg))
    low = [t for t in res["trades"] if t["score"] < cfg.trading.min_score]
    assert low and all(t["risk"] == 0.5 for t in low)
    assert all(t["score"] >= 5.0 for t in res["trades"])


def test_pullback_backtest_no_free_lunch_on_random_walk():
    """Sur une marche aléatoire, l'entrée sur repli ne doit pas créer d'avantage artificiel."""
    import numpy as np
    from analyzer.backtest import Management, simulate
    rng = np.random.default_rng(5)
    n = 12000
    p = (rng.normal(0, 1.0, n * 5).cumsum() + 4000.0).reshape(n, 5)
    idx = pd.date_range("2025-01-06", periods=n, freq="5min", tz="UTC")
    df = pd.DataFrame({"open": p[:, 0], "high": p.max(1), "low": p.min(1), "close": p[:, -1], "volume": 100.0},
                      index=idx)
    an = Analyzer(df, Config())
    mg = Management(entry_mode="pullback", pullback_r=0.3, pullback_bars=3)
    rs = [o.result_r for o in (simulate(an, s, 3.3, 48, mg) for s in an.scan())
          if o.status not in ("non rempli", "annulé", "en cours")]
    assert len(rs) > 20 and np.mean(rs) < 0.15


def test_quick_target_exits_at_target():
    from analyzer.backtest import Management, quick_target, simulate
    an = Analyzer(synthetic(), Config())
    mg = Management(target_r=0.6)
    seen = set()
    for sig in an.scan()[:120]:
        out = simulate(an, sig, 3.3, 48, mg)
        seen.add(out.status)
        assert out.status != "TP2"
        if out.status == "objectif":
            tp = quick_target(sig.entry, sig.sl, sig.direction, 0.6)
            assert out.exit_price == tp and 0 < out.result_r <= 0.6
    assert "objectif" in seen


def test_gold_defaults():
    """Réglages de l'or : 1 pip = 0,10 $, stop 2,50 $ à 15 $, Yahoo = contrat COMEX."""
    from analyzer.broker import PaperBroker
    from analyzer.data import yahoo_ticker
    cfg = Config()
    assert cfg.symbol == "XAUUSD" and cfg.pip == 0.1
    assert cfg.strategy.min_sl_pips * cfg.pip == pytest.approx(2.5)
    assert cfg.strategy.max_sl_pips * cfg.pip == pytest.approx(15.0)
    assert yahoo_ticker("XAUUSDm") == "GC=F" and yahoo_ticker("GOLD") == "GC=F"
    pb = PaperBroker(10_000, cfg.strategy.spread_pips, cfg.pip)
    assert pb.loss_per_lot("XAUUSD", 1, 4000.0, 3995.0) == pytest.approx(500.0)   # 5 $ × 100 onces


def test_signals_sized_for_gold():
    sigs = Analyzer(synthetic(), Config()).scan()
    assert sigs and all(25 <= s.risk_pips <= 150 for s in sigs)
    assert 15 < float(sum(s.atr_pips for s in sigs) / len(sigs)) < 120
