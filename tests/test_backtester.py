import numpy as np
import pandas as pd
import pytest

from src.backtester import run_backtest
from src.metrics import compute_metrics, max_drawdown_pct
from src.strategy import BaseStrategy
from tests.conftest import make_bars


class OneShot(BaseStrategy):
    """Signal unique sur une bougie donnée, ATR fixe."""

    name = "oneshot"

    def __init__(self, at, direction=1, atr=2.0):
        super().__init__({"timeframe": "M5"})
        self.at, self.direction, self.atr_value = at, direction, atr

    def prepare(self, df):
        d = df.copy()
        d["atr"] = self.atr_value
        d["signal"] = 0
        d.iloc[self.at, d.columns.get_loc("signal")] = self.direction
        d["reason"] = "test"
        return d


def _cfg(cfg):
    cfg["filters"]["sessions_utc"] = []
    cfg["backtest"]["slippage_points"] = 0
    cfg["backtest"]["commission_per_lot"] = 0
    return cfg


def test_entry_at_next_open_and_tp(cfg):
    cfg = _cfg(cfg)
    closes = [2000] * 10 + [2001, 2003, 2005, 2008]
    bars = make_bars(closes, spread=20)
    res = run_backtest(OneShot(at=9), bars, cfg)
    t = res.trades.iloc[0]
    assert t["open_time"] == bars.index[10]                   # ouverture de N+1, pas close de N
    assert t["entry_price"] == pytest.approx(bars["open"].iloc[10] + 0.20)  # ask = bid + spread
    assert t["exit_price"] == pytest.approx(t["tp"])
    assert t["r_multiple"] == pytest.approx(2.0, abs=0.05)


def test_sl_assumed_first_when_both_hit(cfg):
    cfg = _cfg(cfg)
    bars = make_bars([2000] * 12, spread=0)
    bars.iloc[11, bars.columns.get_loc("high")] = 2010  # TP et SL dans la même bougie
    bars.iloc[11, bars.columns.get_loc("low")] = 1990
    res = run_backtest(OneShot(at=9), bars, cfg)
    assert res.trades.iloc[0]["pnl"] < 0


def test_costs_reduce_pnl(cfg):
    cfg = _cfg(cfg)
    bars = make_bars([2000] * 10 + [2001, 2003, 2005, 2008])
    base = run_backtest(OneShot(at=9), bars, cfg).trades.iloc[0]["pnl"]
    cfg["backtest"]["commission_per_lot"] = 7
    cfg["backtest"]["slippage_points"] = 10
    costly = run_backtest(OneShot(at=9), bars, cfg).trades.iloc[0]["pnl"]
    assert costly < base


def test_no_entry_on_last_bar_signal(cfg):
    cfg = _cfg(cfg)
    bars = make_bars([2000] * 10)
    assert run_backtest(OneShot(at=9), bars, cfg).metrics["trades"] == 0


def test_real_strategies_run(cfg, m5_data, m1_data):
    from src.strategy import build_strategy
    for name, data in (("trend_following", m5_data), ("mean_reversion", m1_data.iloc[:30000])):
        res = run_backtest(build_strategy(cfg, name), data, cfg)
        assert res.metrics["trades"] > 0
        assert np.isfinite(res.equity).all()
        # aucune perte individuelle ne dépasse ~2 % du capital
        assert (res.trades["pnl"] > -0.02 * 10000 * 3).all()


def test_metrics():
    idx = pd.date_range("2024-01-01", periods=30, freq="D", tz="UTC")
    eq = pd.Series(np.linspace(10000, 11000, 30), index=idx)
    eq.iloc[10] = 9000
    assert max_drawdown_pct(eq) > 10
    tr = pd.DataFrame({"pnl": [100, -50, 200], "r_multiple": [2, -1, 2]})
    m = compute_metrics(tr, eq, 10000)
    assert m["trades"] == 3 and m["winrate_pct"] == pytest.approx(66.67) and m["profit_factor"] == 6.0
