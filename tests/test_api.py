from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from analyzer.config import Config
from analyzer.server import create_app, seconds_to_next_close


@pytest.fixture()
def client(tmp_path):
    cfg = Config(source="demo", data_dir=str(tmp_path / "data"))
    app = create_app(cfg, str(tmp_path / "config.yaml"))
    with TestClient(app) as c:
        yield c


def test_state(client):
    s = client.get("/api/state").json()
    assert s["ready"] and s["source"] == "demo"
    assert len(s["chart"]["candles"]) == 600
    assert {"h1_bias", "rsi", "adx", "atr_pips"} <= set(s["context"])


def test_journal_filled_and_tracked(client):
    j = client.get("/api/journal").json()
    assert j["signals"], "le journal doit contenir les signaux de l'historique"
    assert {r["status"] for r in j["signals"]} <= {"en cours", "TP2", "BE", "SL", "temps"}
    key = j["signals"][0]["key"]
    assert client.put(f"/api/journal/{key}/note", json={"note": "pris"}).status_code == 200
    assert client.get("/api/journal").json()["signals"][0]["note"] == "pris"
    assert client.get("/api/journal.csv").text.startswith("time,side")


def test_settings_roundtrip(client, tmp_path):
    r = client.put("/api/settings", json={"strategy": {"min_score": 7}})
    assert r.status_code == 200 and r.json()["strategy"]["min_score"] == 7
    assert (tmp_path / "config.yaml").exists()
    assert client.put("/api/settings", json={"strategy": {"min_sl_pips": 300}}).status_code == 400


def test_backtest_endpoint(client):
    r = client.post("/api/backtest", json={"bars": 3000}).json()
    assert r["period"]["bars"] == 3000 and "stats" in r


def test_index_served(client):
    assert "Signal Bot" in client.get("/").text
    assert client.get("/static/vendor/lightweight-charts.standalone.production.js").status_code == 200


def test_next_close():
    t = datetime(2026, 1, 1, 10, 4, 0, tzinfo=timezone.utc)
    assert seconds_to_next_close(t) == pytest.approx(68)
    t = datetime(2026, 1, 1, 10, 5, 3, tzinfo=timezone.utc)
    assert seconds_to_next_close(t) == pytest.approx(5)


def test_trading_api_paper(client):
    st = client.get("/api/trading").json()
    assert st["enabled"] is False and st["mode"] == "mt5"           # désactivé par défaut
    r = client.put("/api/trading/settings", json={"trading": {"mode": "paper", "risk_percent": 1}})
    assert r.status_code == 200 and r.json()["mode"] == "paper"
    assert client.post("/api/trading/enable", json={}).json()["enabled"] is True
    assert client.post("/api/trading/disable").json()["ok"]
    assert client.get("/api/trading").json()["enabled"] is False
    assert client.post("/api/trading/close/999").status_code == 404


def test_allow_live_requires_confirmation(client):
    r = client.put("/api/trading/settings", json={"trading": {"allow_live": True}})
    assert r.status_code == 403
    r = client.put("/api/trading/settings", json={"trading": {"allow_live": True}, "confirm": "REEL"})
    assert r.status_code == 200 and r.json()["settings"]["allow_live"] is True


def test_trading_settings_validation(client):
    assert client.put("/api/trading/settings", json={"trading": {"risk_percent": 50}}).status_code == 400


def test_autonomous_mode(client, monkeypatch, tmp_path):
    import analyzer.autonomy as au
    target = tmp_path / "autostart" / "gold-ai-autonome.desktop"
    monkeypatch.setattr(au, "_startup_file", lambda: target)
    r = client.post("/api/trading/autonomous", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"]
    assert (tmp_path / "autostart" / "gold-ai-autonome.desktop").exists()
    st = client.get("/api/trading").json()
    assert st["settings"]["autonomous"] and st["settings"]["start_enabled"]
    r = client.post("/api/trading/autonomous", json={"enabled": False})
    assert not r.json()["autostart"]
    assert not (tmp_path / "autostart" / "gold-ai-autonome.desktop").exists()


def test_place_endpoint_unknown_signal(client):
    assert client.get("/api/trading/place/inconnu").status_code == 404
