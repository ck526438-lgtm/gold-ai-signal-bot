"""Anti look-ahead des stratégies à règles et du backtester.

Un signal sur la bougie N ne doit utiliser AUCUNE donnée de N+1.
"""
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.backtester import run_backtest
from src.data_feed import drop_unclosed, resample
from src.strategy import build_strategy

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["mean_reversion", "trend_following"])
def test_signal_at_n_ignores_future(cfg, m5_data, name):
    strat = build_strategy(cfg, name)
    df = m5_data.iloc[:3000]
    full = strat.prepare(df)
    rng = np.random.default_rng(1)
    for n in rng.integers(300, len(df) - 2, 40):
        truncated = strat.prepare(df.iloc[: n + 1])           # données jusqu'à N seulement
        for col in ("signal", "atr"):
            a, b = full[col].iloc[n], truncated[col].iloc[-1]
            assert (pd.isna(a) and pd.isna(b)) or a == pytest.approx(b), f"{name}.{col} dépend du futur en {n}"


@pytest.mark.parametrize("name", ["mean_reversion", "trend_following"])
def test_perturbing_next_bar_does_not_change_signal(cfg, m5_data, name):
    strat = build_strategy(cfg, name)
    df = m5_data.iloc[:2000].copy()
    base = strat.prepare(df)
    n = 1500
    df.iloc[n + 1:, :4] *= 1.05                               # on change violemment N+1…
    pert = strat.prepare(df)
    pd.testing.assert_series_equal(base["signal"].iloc[: n + 1], pert["signal"].iloc[: n + 1])


def test_backtester_enters_after_signal_bar(cfg, m5_data):
    cfg["filters"]["sessions_utc"] = []
    strat = build_strategy(cfg, "trend_following")
    res = run_backtest(strat, m5_data, cfg)
    prepared = strat.prepare(m5_data)
    sig_times = set(prepared.index[prepared["signal"] != 0])
    for t in res.trades["open_time"]:
        prev = prepared.index[prepared.index.get_loc(t) - 1]
        assert prev in sig_times and t > prev                 # entrée strictement après la bougie signal


def test_drop_unclosed_bar():
    idx = pd.date_range("2024-01-02 10:00", periods=3, freq="5min", tz="UTC")
    df = pd.DataFrame({"close": [1, 2, 3]}, index=idx)
    now = pd.Timestamp("2024-01-02 10:13", tz="UTC")          # bougie 10:10 pas finie
    assert list(drop_unclosed(df, "M5", now).index) == list(idx[:2])


def test_resample_drops_incomplete_last_bar(m1_data):
    part = m1_data.iloc[:13]                                  # 2 bougies M5 pleines + 3 minutes
    out = resample(part, "M5")
    assert len(out) == 2


def test_live_feed_skips_forming_bar(cfg, fake_mt5):
    """copy_rates_from_pos doit être appelé avec start_pos=1 (FakeMT5 l'exige)."""
    from src.data_feed import DataFeed
    from src.mt5_connector import MT5Connector

    t0 = int(pd.Timestamp("2024-01-02 10:00", tz="UTC").timestamp())
    fake_mt5.rates = np.array(
        [(t0 + 300 * i, 1.0, 2.0, 0.5, 1.5, 10, 20, 0) for i in range(5)],
        dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
               ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")],
    )
    bars = DataFeed(MT5Connector(cfg, fake_mt5), cfg).get_closed_bars("M5", 5)
    assert len(bars) == 5


def test_no_negative_shift_in_rule_code():
    for f in ("src/indicators.py", "src/strategy.py", "src/backtester.py"):
        code = (ROOT / f).read_text(encoding="utf-8")
        assert not re.search(r"shift\(\s*-", code), f"shift négatif interdit dans {f}"
