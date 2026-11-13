"""Aller-retour complet client ↔ serveur du pont MT5, avec le faux module MetaTrader5."""
import threading
from datetime import datetime, timezone

import pytest

from analyzer.config import Config
from analyzer.data import from_mt5
from analyzer.executor import Executor
from analyzer.mt5bridge import BridgeMT5
from analyzer.mt5link import MT5Link
from bridge import mt5_bridge_server as srv
from tests.fake_mt5 import FakeMT5
from tests.test_trading import make_sig


class FakeWithRates(FakeMT5):
    def copy_rates_from_pos(self, symbol, tf, start, count):
        import numpy as np
        t0 = int(datetime.now(timezone.utc).timestamp()) // 300 * 300 - count * 300
        dt = np.dtype([("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
                       ("close", "f8"), ("tick_volume", "u8"), ("spread", "i4"), ("real_volume", "u8")])
        rows = [(t0 + i * 300, 1080, 1080.5, 1079.5, 1080.1, 100, 8, 0) for i in range(count)]
        return np.array(rows, dtype=dt)


@pytest.fixture()
def bridge():
    fake = FakeWithRates(symbols=("XAUUSDm",), server_offset=0)
    server = srv.serve(fake, "127.0.0.1", 0, token="secret")
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield fake, f"127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def test_roundtrip_types(bridge):
    fake, addr = bridge
    mt5 = BridgeMT5(addr, token="secret")
    assert mt5.ping() and mt5.initialize()
    assert mt5.ORDER_TYPE_SELL == 1 and mt5.TIMEFRAME_M5 == 5
    acc = mt5.account_info()
    assert acc.login == 123456 and acc.trade_mode == 0
    assert mt5.symbols_get(group="*XAUUSD*")[0].name == "XAUUSDm"
    rates = mt5.copy_rates_from_pos("XAUUSDm", mt5.TIMEFRAME_M5, 0, 10)
    assert len(rates) == 10 and rates[0]["close"] == pytest.approx(1080.1)
    assert mt5.history_deals_get(datetime(2026, 1, 1), datetime(2026, 1, 2)) == ()


def test_bad_token_refused(bridge):
    _, addr = bridge
    with pytest.raises(RuntimeError, match="jeton"):
        BridgeMT5(addr, token="faux").account_info()


def test_unreachable_bridge_message():
    with pytest.raises(RuntimeError, match="gold-ai-mt5"):
        BridgeMT5("127.0.0.1:1", token="x").ping()


def test_data_and_robot_through_bridge(bridge, tmp_path, monkeypatch):
    fake, addr = bridge
    link = MT5Link()
    link.set_bridge(addr)
    monkeypatch.setenv("MT5_BRIDGE_TOKEN", "secret")
    monkeypatch.setattr("analyzer.mt5link.LINK", link)
    cfg = Config(symbol="XAUUSDm", bars=400)
    cfg.trading.quality_edge = False
    df = from_mt5(cfg)
    assert len(df) >= 398 and set(df.columns) == {"open", "high", "low", "close", "volume"}

    fake.bid = 1080
    ex = Executor(cfg, tmp_path, link=link)
    ex.enable()
    done = ex.on_signal(make_sig(entry=1080.08))
    assert len(done) == 2 and len(fake.positions) == 2
    assert ex.close_all() and not fake.positions
