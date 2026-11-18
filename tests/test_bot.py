import numpy as np
import pandas as pd
import pytest

from src.bot import TradingBot
from src.state import StateStore
from tests.conftest import FakeMT5


class Spy:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return lambda *a, **k: self.events.append((name, a, k))


def _rates(closes, start):
    t0 = int(pd.Timestamp(start, tz="UTC").timestamp())
    return np.array(
        [(t0 + 300 * i, c, c + 0.5, c - 0.5, c, 100, 20, 0) for i, c in enumerate(closes)],
        dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
               ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")],
    )


def test_bot_refuses_non_demo_mode(cfg):
    cfg["mode"] = "backtest"
    with pytest.raises(RuntimeError):
        TradingBot(cfg, mt5_module=FakeMT5(), notifier=Spy())


def test_bot_refuses_real_account(cfg):
    bot = TradingBot(cfg, mt5_module=FakeMT5(trade_mode=2), notifier=Spy())
    with pytest.raises(RuntimeError, match="démo"):
        bot.start()


def test_bot_step_pause_and_stop(cfg):
    mt5 = FakeMT5()
    mt5.rates = _rates(np.linspace(1990, 2000, 400), "2024-01-02 00:00")
    bot = TradingBot(cfg, mt5_module=mt5, notifier=Spy())
    bot.start()
    st = StateStore(cfg["monitoring"]["pause_file"])
    st.update(paused=True)
    assert bot.step() is True and mt5.requests == []     # en pause : aucun ordre
    st.update(stop_requested=True)
    assert bot.step() is False                           # arrêt propre


def test_bot_does_not_trade_when_killswitch(cfg):
    mt5 = FakeMT5()
    mt5.rates = _rates(np.linspace(1990, 2000, 400), "2024-01-02 00:00")
    bot = TradingBot(cfg, mt5_module=mt5, notifier=Spy())
    bot.start()
    bot.risk.locked, bot.risk.lock_reason = True, "test"
    bot.step()
    assert mt5.requests == []
