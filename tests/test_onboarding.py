import time

import pytest
from fastapi.testclient import TestClient

from analyzer import server as server_mod
from analyzer.config import Config
from analyzer.mt5link import MT5Link
from analyzer.onboarding import detect, offset_from_tick, pick_symbol
from analyzer.server import create_app
from tests.fake_mt5 import FakeMT5


def test_pick_symbol():
    assert pick_symbol(["XAUUSDm", "XAUUSD", "XAUUSD.pro"]) == "XAUUSD"
    assert pick_symbol(["XAUUSD.pro", "XAUUSDm"]) == "XAUUSDm"
    assert pick_symbol(["mXAUUSD"]) == "mXAUUSD"
    assert pick_symbol([]) is None
    # courtiers qui appellent l'or GOLD
    assert pick_symbol(["GOLDm", "GOLD"]) == "GOLD"
    assert pick_symbol(["GOLD.a", "XAUUSD#"]) == "XAUUSD#"


def test_detect_gold_named_gold():
    fake = FakeMT5(symbols=("GOLDm", "XAUEUR", "SILVERm"), server_offset=2)
    d = detect(MT5Link(fake))
    assert d["symbol"] == "GOLDm" and d["symbols"] == ["GOLDm"]


def test_offset_from_tick():
    now = 1_800_000_000
    assert offset_from_tick(now + 2 * 3600 - 5, now) == 2
    assert offset_from_tick(now + 3 * 3600 + 60, now) == 3
    assert offset_from_tick(now - 30, now) == 0
    assert offset_from_tick(now - 2 * 86400, now) is None      # week-end : cotation ancienne


def test_detect_with_fake_mt5():
    fake = FakeMT5(symbols=("XAUUSDm", "XAUUSD.ecn", "XAGUSDm"), server_offset=3)
    d = detect(MT5Link(fake))
    assert d["connected"] and d["symbol"] == "XAUUSDm"
    assert d["symbols"] == ["XAUUSD.ecn", "XAUUSDm"]
    assert d["offset"] == 3 and d["account"]["type"] == "DÉMO" and d["algo_trading"] is True


def test_detect_without_terminal(monkeypatch):
    monkeypatch.setattr("analyzer.mt5link.find_terminals", lambda: [])
    d = detect(MT5Link(FakeMT5(fail_init=True)))
    assert not d["connected"] and "impossible" in d["error"]


def test_link_tries_installed_terminals(monkeypatch):
    fake = FakeMT5(fail_init=True)
    monkeypatch.setattr("analyzer.mt5link.find_terminals", lambda: ["C:/A/terminal64.exe", "C:/B/terminal64.exe"])
    with pytest.raises(RuntimeError):
        MT5Link(fake).ensure()
    assert [c.get("path", "") for c in fake.init_calls] == ["", "C:/A/terminal64.exe", "C:/B/terminal64.exe"]


def test_setup_flow_api(tmp_path, monkeypatch):
    fake = FakeMT5(symbols=("XAUUSDm",), server_offset=2)
    monkeypatch.setattr(server_mod, "LINK", MT5Link(fake))
    cfg = Config(source="demo", data_dir=str(tmp_path / "data"))
    with TestClient(create_app(cfg, str(tmp_path / "config.yaml"))) as c:
        assert c.get("/api/setup").json()["setup_done"] is False
        d = c.get("/api/setup/detect").json()
        assert d["symbol"] == "XAUUSDm" and d["offset"] == 2
        r = c.post("/api/setup/apply", json={"source": "demo", "symbol": d["symbol"],
                                             "broker_utc_offset": d["offset"]}).json()
        assert r["settings"]["setup_done"] and r["settings"]["symbol"] == "XAUUSDm"
        assert c.get("/api/setup").json()["setup_done"] is True
        assert "setup_done: true" in (tmp_path / "config.yaml").read_text()
        diag = c.get("/api/diagnostics").json()
        ids = {x["id"]: x["status"] for x in diag["checks"]}
        assert ids["mt5"] == "ok" and ids["symbol"] == "ok" and ids["algo"] == "ok"
        assert c.get("/guide").status_code == 200


def test_skip_setup(tmp_path):
    cfg = Config(source="demo", data_dir=str(tmp_path / "data"))
    with TestClient(create_app(cfg, str(tmp_path / "config.yaml"))) as c:
        assert c.post("/api/setup/skip").json()["ok"]
        assert c.get("/api/setup").json()["setup_done"] is True
    time.sleep(0)
