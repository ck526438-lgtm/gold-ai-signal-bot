import pandas as pd
import pytest

from src.execution import Executor
from src.mt5_connector import MT5Connector
from src.risk_manager import RiskManager, TradePlan
from tests.conftest import FakeMT5


class Spy:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        return lambda *a, **k: self.events.append((name, a, k))


def _plan(direction=1):
    return TradePlan(direction, 0.10, 2000.0, 1997.0, 2006.0, 3.0, 30.0, "test")


def _ex(cfg, mt5, notifier=None):
    conn = MT5Connector(cfg, mt5)
    conn.connect()
    return Executor(conn, cfg, RiskManager(cfg), notifier)


def test_open_position_logs_and_notifies(cfg):
    mt5, spy = FakeMT5(bid=2000.0), Spy()
    ex = _ex(cfg, mt5, spy)
    t = ex.open_position(_plan(), expected_price=2000.2)
    assert t is not None and t.ticket in ex.open_trades
    req = mt5.requests[-1]
    assert req["sl"] == pytest.approx(req["price"] - 3.0) and req["tp"] == pytest.approx(req["price"] + 6.0)
    assert any(e[0] == "notify_entry" for e in spy.events)


def test_retry_on_requote_then_success(cfg):
    mt5 = FakeMT5(retcodes=[10004, 10004])
    cfg["execution"]["retry_delay_s"] = 0
    t = _ex(cfg, mt5).open_position(_plan(), 2000.2)
    assert t is not None and len(mt5.requests) == 3


def test_gives_up_after_3_and_alerts(cfg):
    mt5, spy = FakeMT5(retcodes=[10004, 10004, 10004]), Spy()
    cfg["execution"]["retry_delay_s"] = 0
    assert _ex(cfg, mt5, spy).open_position(_plan(), 2000.2) is None
    assert len(mt5.requests) == 3
    assert any(e[0] == "alert_error" for e in spy.events)


def test_non_transient_error_no_retry(cfg):
    mt5, spy = FakeMT5(retcodes=[10019]), Spy()  # fonds insuffisants
    assert _ex(cfg, mt5, spy).open_position(_plan(), 2000.2) is None
    assert len(mt5.requests) == 1 and any(e[0] == "alert_error" for e in spy.events)


def test_refuses_excessive_slippage(cfg):
    mt5 = FakeMT5(bid=2001.0)  # 80 pts au-dessus du prix attendu
    assert _ex(cfg, mt5).open_position(_plan(), expected_price=2000.2) is None
    assert mt5.requests == []


def test_sync_detects_close_writes_csv_and_feeds_risk(cfg):
    mt5, spy = FakeMT5(), Spy()
    ex = _ex(cfg, mt5, spy)
    t = ex.open_position(_plan(), 2000.2)
    mt5.close_position(t.ticket, profit=-30.0, price=1997.0)
    closed = ex.sync_positions()
    assert len(closed) == 1 and closed[0]["pnl"] == pytest.approx(-37.0)
    assert ex.risk.consecutive_losses == 1
    df = pd.read_csv(cfg["logging"]["trades_csv"])
    assert len(df) == 1 and df["ticket"].iloc[0] == t.ticket
    assert any(e[0] == "notify_exit" for e in spy.events)


def test_close_all(cfg):
    mt5 = FakeMT5()
    ex = _ex(cfg, mt5)
    ex.open_position(_plan(), 2000.2)
    assert ex.close_all() == 1 and mt5.positions == []


def test_connector_checks_expected_account(cfg, monkeypatch):
    monkeypatch.setenv("MT5_LOGIN", "5059124546")
    monkeypatch.delenv("MT5_PASSWORD", raising=False)
    assert MT5Connector(cfg, FakeMT5()).connect() is False   # le terminal est sur le compte 1
    monkeypatch.setenv("MT5_LOGIN", "1")
    assert MT5Connector(cfg, FakeMT5()).connect() is True
