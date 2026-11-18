import numpy as np
import pandas as pd

from src.filters import EntryFilters
from src.strategy import MeanReversionStrategy, TrendFollowingStrategy, build_strategy
from tests.conftest import make_bars


def test_build_all_rule_strategies(cfg):
    assert build_strategy(cfg, "mean_reversion").timeframe == "M1"
    assert build_strategy(cfg, "trend_following").timeframe == "M5"


def test_mean_reversion_long_after_selloff(cfg):
    closes = list(np.full(60, 2000.0) + np.random.default_rng(0).normal(0, 0.2, 60)) + [1999, 1997, 1994, 1990]
    s = MeanReversionStrategy(cfg["strategy"]["mean_reversion"])
    d = s.prepare(make_bars(closes, freq="1min"))
    assert d["signal"].iloc[-1] == 1
    assert d["rsi"].iloc[-1] < 20 and d["close"].iloc[-1] < d["bb_low"].iloc[-1]


def test_trend_following_cross_needs_filter(cfg):
    p = dict(cfg["strategy"]["trend_following"], atr_threshold=0.1)
    s = TrendFollowingStrategy(p)
    up = np.linspace(1900, 2000, 200)
    dip = np.linspace(2000, 1990, 15)
    rebound = np.linspace(1990, 2010, 15)
    d = s.prepare(make_bars(np.concatenate([up, dip, rebound]), wick=1.0))
    longs = d.index[d["signal"] == 1]
    assert len(longs) >= 1
    assert (d.loc[longs, "close"] > d.loc[longs, "ema_filter"]).all()
    assert (d["signal"] == -1).sum() == 0 or (d.loc[d["signal"] == -1, "close"] < d.loc[d["signal"] == -1, "ema_filter"]).all()


def test_generate_signal_returns_last_bar(cfg):
    closes = list(np.full(60, 2000.0) + np.random.default_rng(0).normal(0, 0.2, 60)) + [1999, 1997, 1994, 1990]
    bars = make_bars(closes, freq="1min")
    sig = MeanReversionStrategy(cfg["strategy"]["mean_reversion"]).generate_signal(bars)
    assert sig is not None and sig.direction == 1 and sig.time == bars.index[-1]


def test_filters(cfg):
    cfg["filters"]["news_events_utc"] = ["2024-01-02 13:30"]
    f = EntryFilters(cfg)
    ok_ts = pd.Timestamp("2024-01-02 10:00", tz="UTC")
    assert f.check(ok_ts, 20, 2.0, 0) == (True, "ok")
    assert not f.check(ok_ts, 50, 2.0, 0)[0]                                   # spread
    assert not f.check(ok_ts, 20, 2.0, 1)[0]                                   # position ouverte
    assert not f.check(pd.Timestamp("2024-01-02 13:15", tz="UTC"), 20, 2.0, 0)[0]  # news
    assert not f.check(ok_ts, 20, 0.1, 0)[0]                                   # ATR
    assert not f.check(pd.Timestamp("2024-01-02 03:00", tz="UTC"), 20, 2.0, 0)[0]  # session
    idx = pd.DatetimeIndex([ok_ts, pd.Timestamp("2024-01-02 13:30", tz="UTC")])
    assert f.mask(idx, np.array([20.0, 20.0]), np.array([2.0, 2.0])).tolist() == [True, False]
