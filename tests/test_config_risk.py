import copy
from datetime import date

import pytest

from src.config import ConfigError, validate_config
from src.risk_manager import RiskManager
from src.strategy import Signal


def test_live_mode_refused(cfg):
    c = copy.deepcopy(cfg)
    c["mode"] = "live"
    with pytest.raises(ConfigError):
        validate_config(c)


def test_risk_above_2pct_refused(cfg):
    c = copy.deepcopy(cfg)
    c["risk"]["risk_per_trade_pct"] = 2.5
    with pytest.raises(ConfigError):
        validate_config(c)
    c["risk"]["risk_per_trade_pct"] = 1.0
    c["risk"]["max_risk_per_trade_pct"] = 5.0
    with pytest.raises(ConfigError):
        validate_config(c)


def test_position_size_respects_risk(cfg):
    rm = RiskManager(cfg)
    # 10 000 $ × 0,5 % = 50 $ ; SL 3 $ × 100 oz = 300 $/lot → 0,16 lot
    vol = rm.compute_volume(10000, 3.0, 100, 0.01, 0.01, 50)
    assert vol == pytest.approx(0.16)
    assert vol * 3.0 * 100 <= 50 + 1e-9


def test_min_lot_never_forced(cfg):
    rm = RiskManager(cfg)
    assert rm.compute_volume(100, 10.0, 100, 0.01, 0.01, 50) == 0.0


def test_sl_tp_from_atr(cfg):
    rm = RiskManager(cfg)
    sig = Signal(1, None, 2000.0, atr=2.0, reason="t")
    plan = rm.plan_trade(sig, 10000, 2000.0, 100, 0.01, 0.01, 50)
    assert plan.sl == pytest.approx(2000 - 3.0)       # 1,5 × ATR
    assert plan.tp == pytest.approx(2000 + 6.0)       # 2 × SL
    short = rm.plan_trade(Signal(-1, None, 2000.0, 2.0, "t"), 10000, 2000.0, 100, 0.01, 0.01, 50)
    assert short.sl > 2000 > short.tp


def test_killswitch_daily_drawdown(cfg):
    calls = []
    notifier = type("N", (), {"alert_killswitch": lambda self, r, **k: calls.append(r)})()
    rm = RiskManager(cfg, notifier)
    d = date(2024, 1, 2)
    rm.update_equity(10000, d)
    rm.update_equity(9750, d)
    assert not rm.locked
    rm.update_equity(9690, d)  # -3,1 %
    assert rm.locked and calls and "drawdown" in calls[0]
    rm.update_equity(9690, date(2024, 1, 3))  # nouveau jour → réarmé
    assert not rm.locked


def test_killswitch_consecutive_losses(cfg):
    rm = RiskManager(cfg)
    rm.update_equity(10000, date(2024, 1, 2))
    rm.register_trade_result(-10)
    rm.register_trade_result(-10)
    rm.register_trade_result(+5)
    rm.register_trade_result(-10)
    rm.register_trade_result(-10)
    assert not rm.locked
    rm.register_trade_result(-10)
    assert rm.locked


def test_trailing_only_tightens(cfg):
    cfg["risk"]["trailing"]["enabled"] = True
    rm = RiskManager(cfg)
    # long entré à 2000, SL initial 1997 (3 $). +0,5R → pas de trailing
    assert rm.trailing_sl(1, 2000, 1997, 3.0, 2001.5, 2.0) is None
    new = rm.trailing_sl(1, 2000, 1997, 3.0, 2004.0, 2.0)  # +1,33R
    assert new == pytest.approx(2001.0)
    assert rm.trailing_sl(1, 2000, 2001.0, 3.0, 2003.5, 2.0) is None  # ne recule jamais
