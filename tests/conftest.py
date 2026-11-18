import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from tools.make_synthetic_data import make_m1  # noqa: E402

_BASE_CFG = load_config(ROOT / "config.yaml")


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(_BASE_CFG)
    c["logging"]["dir"] = str(tmp_path / "logs")
    c["logging"]["trades_csv"] = str(tmp_path / "logs" / "trades.csv")
    c["monitoring"]["pause_file"] = str(tmp_path / "state.json")
    c["monitoring"]["dry_run"] = True
    c["backtest"]["output_dir"] = str(tmp_path / "reports")
    c["ml"]["models_dir"] = str(tmp_path / "models")
    c["filters"]["news_events_utc"] = []
    return c


@pytest.fixture(scope="session")
def m1_data():
    return make_m1(months=3, seed=11)


@pytest.fixture(scope="session")
def m5_data(m1_data):
    from src.data_feed import resample

    return resample(m1_data, "M5")


def make_bars(closes, start="2024-01-02 08:00", freq="5min", spread=20.0, wick=0.3):
    closes = np.asarray(closes, float)
    idx = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC")
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({
        "open": opens, "high": np.maximum(opens, closes) + wick, "low": np.minimum(opens, closes) - wick,
        "close": closes, "tick_volume": 100.0, "spread": spread,
    }, index=idx)


# ---------------------------------------------------------------- faux MetaTrader5
class FakeMT5:
    TIMEFRAME_M1, TIMEFRAME_M5 = 1, 5
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TIME_GTC = 0
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2

    def __init__(self, bid=2000.0, spread=0.20, trade_mode=0, retcodes=None):
        self.bid, self.spread = bid, spread
        self.trade_mode = trade_mode
        self.retcodes = list(retcodes or [])
        self.requests, self.positions, self.deals = [], [], {}
        self.equity = 10000.0
        self.initialized = False
        self.next_ticket = 1000
        self.rates = None

    def initialize(self, **kw):
        self.initialized = True
        return True

    def shutdown(self):
        self.initialized = False

    def last_error(self):
        return (0, "ok")

    def symbol_select(self, s, b):
        return True

    def terminal_info(self):
        return SimpleNamespace(connected=self.initialized)

    def account_info(self):
        return SimpleNamespace(login=1, server="Demo", currency="USD", equity=self.equity,
                               balance=self.equity, trade_mode=self.trade_mode)

    def symbol_info(self, s):
        return SimpleNamespace(point=0.01, digits=2, trade_contract_size=100.0, volume_min=0.01,
                               volume_step=0.01, volume_max=50.0, filling_mode=1)

    def symbol_info_tick(self, s):
        return SimpleNamespace(bid=self.bid, ask=round(self.bid + self.spread, 2), time=0)

    def positions_get(self, symbol=None):
        return list(self.positions)

    def copy_rates_from_pos(self, s, tf, start, count):
        assert start == 1, "le bot doit ignorer la bougie en cours (start_pos=1)"
        return self.rates

    def history_deals_get(self, position=None):
        return self.deals.get(position, [])

    def order_send(self, req):
        self.requests.append(dict(req))
        code = self.retcodes.pop(0) if self.retcodes else 10009
        if code != 10009:
            return SimpleNamespace(retcode=code, comment="fake", price=0, order=0, deal=0)
        if req["action"] == self.TRADE_ACTION_DEAL and "position" not in req:
            t = self.next_ticket
            self.next_ticket += 1
            self.positions.append(SimpleNamespace(
                ticket=t, identifier=t, type=req["type"], volume=req["volume"], price_open=req["price"],
                sl=req["sl"], tp=req["tp"], profit=0.0, magic=req["magic"], time=0,
            ))
            return SimpleNamespace(retcode=10009, comment="done", price=req["price"], order=t, deal=t)
        if "position" in req and req["action"] == self.TRADE_ACTION_DEAL:
            self.positions = [p for p in self.positions if p.ticket != req["position"]]
        return SimpleNamespace(retcode=10009, comment="done", price=req.get("price", 0), order=0, deal=0)

    def close_position(self, ticket, profit, price):
        self.positions = [p for p in self.positions if p.ticket != ticket]
        self.deals[ticket] = [
            SimpleNamespace(profit=0.0, commission=-3.5, swap=0.0, fee=0.0, entry=0, price=0, time=0),
            SimpleNamespace(profit=profit, commission=-3.5, swap=0.0, fee=0.0, entry=1, price=price, time=1_700_000_000),
        ]


@pytest.fixture
def fake_mt5():
    return FakeMT5()
