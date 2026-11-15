"""Missions en langage naturel (onglet Mission)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from analyzer.config import Config
from analyzer.executor import Executor
from analyzer.mission import parse
from analyzer.mt5link import MT5Link
from analyzer.server import create_app
from tests.fake_mt5 import FakeMT5
from tests.test_trading import make_sig

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)


def test_parse_full_mission():
    r = parse("Trade seulement les cassures du range asiatique, risque 0,5 %, prends les profits à 2R, "
              "maximum 2 trades, arrête-toi à 11h et ferme tout.", 10_000, NOW)
    m = r["mission"]
    assert r["changes"]["trading"] == {"risk_percent": 0.5, "quick_target_r": 2.0, "max_trades_per_day": 2}
    assert m.patterns == ["Cassure du range asiatique baissière", "Cassure du range asiatique haussière"]
    assert m.max_trades == 2 and m.close_at_end and m.end_at == "2026-11-16T11:00:00+00:00"


@pytest.mark.parametrize("text, check", [
    ("achats seulement", lambda r: r["mission"].direction == 1),
    ("ventes uniquement", lambda r: r["mission"].direction == -1),
    ("objectif +2 %, perte max 1 %", lambda r: (r["mission"].profit_target, r["mission"].loss_stop) == (200, 100)
     and r["changes"]["trading"]["daily_loss_limit_percent"] == 1),
    ("gagne 50 $ puis arrête", lambda r: r["mission"].profit_target == 50),
    ("prends les profits à 10x et sécurise le reste avant 6h30",
     lambda r: r["changes"]["strategy"]["rr_tp2"] == 10 and r["mission"].close_at_end
     and r["mission"].end_at == "2026-11-16T06:30:00+00:00"),
    ("pendant 2 heures", lambda r: r["mission"].end_at == (NOW + timedelta(hours=2)).isoformat()),
    ("jusqu'à 5h", lambda r: r["mission"].end_at.startswith("2026-11-17T05:00")),      # demain
    ("mode prudent en simulateur", lambda r: r["changes"]["trading"]["mode"] == "paper"
     and r["changes"]["trading"]["risk_percent"] == 0.25),
    ("valide chaque trade avec l'assistant et entrée sur retest",
     lambda r: r["changes"]["trading"]["ai_confirm"] and r["changes"]["trading"]["retest_entries"]),
    ("risque 9 %", lambda r: "risk_percent" not in r["changes"].get("trading", {}) and r["warnings"]),
    ("bonjour", lambda r: r["empty"]),
])
def test_parse_variants(text, check):
    assert check(parse(text, 10_000, NOW))


@pytest.fixture()
def bot(tmp_path):
    cfg = Config()
    cfg.trading.quality_edge = False
    cfg.trading.retest_entries = False
    fake = FakeMT5(balance=1000.0)
    return Executor(cfg, tmp_path, link=MT5Link(fake)), fake


def test_mission_filters_and_limits(bot):
    ex, fake = bot
    ex.start_mission(parse("achats seulement, maximum 1 trade", 1000)["mission"])
    assert ex.enabled and ex.mission.start_balance == pytest.approx(1000)
    assert ex.on_signal(make_sig(direction=-1, key="S1")) == []                  # vente refusée
    assert any(s["code"] == "mission" for s in ex.skip_stats())
    assert ex.on_signal(make_sig(key="L1"))                                      # achat accepté
    assert ex.mission.trades == 1
    ex.store.x("UPDATE trades SET status='fermé'")
    assert ex.on_signal(make_sig(key="L2")) == []                                # 1 trade max
    assert ex.missions.load().trades == 1                                       # sauvegardée


def test_mission_profit_target_secures_gains(bot):
    ex, fake = bot
    ex.start_mission(parse("objectif +20 $", 1000)["mission"])
    assert ex.on_signal(make_sig(key="L1")) and fake.positions
    fake.account.equity = 1025.0
    ex.manage()
    m = ex.mission
    assert m.status == "terminée" and m.end_reason.startswith("objectif atteint")
    assert not ex.enabled and not fake.positions
    assert ex.store.q("SELECT message FROM bot_log ORDER BY id DESC LIMIT 1")[0]["message"].startswith("Mission terminée")


def test_mission_end_time_and_stop(bot):
    ex, fake = bot
    m = parse("ferme tout à 11h", 1000)["mission"]
    m.end_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    ex.start_mission(m)
    ex.on_signal(make_sig(key="L1"))
    ex.manage()
    assert ex.mission.status == "terminée" and not ex.enabled and not fake.positions
    ex.start_mission(parse("achats seulement", 1000)["mission"])
    ex.disable("Robot désactivé")
    assert ex.mission.status == "arrêtée"


def test_mission_api(tmp_path):
    cfg = Config(source="demo", data_dir=str(tmp_path / "data"))
    cfg.trading.mode = "paper"
    with TestClient(create_app(cfg, str(tmp_path / "config.yaml"))) as c:
        r = c.post("/api/mission", json={"text": "bonjour"}).json()
        assert not r["started"] and "Exemples" in r["reply"]
        r = c.post("/api/mission", json={"text": "achats seulement en simulateur, risque 0,5 %, objectif +2 %, "
                                                 "jusqu'à 20h"}).json()
        assert r["started"] and "C'est parti" in r["reply"]
        v = c.get("/api/mission").json()
        assert v["enabled"] and v["mission"]["status"] == "active" and v["mission"]["direction"] == 1
        assert [a["id"] for a in v["agents"]] == ["veilleur", "analyste", "executeur", "risque"]
        assert v["mission"]["profit_target"] == pytest.approx(200) and v["mission"]["multiple"] == pytest.approx(1)
        assert c.get("/api/settings").json()["trading"]["risk_percent"] == 0.5
        v = c.post("/api/mission/stop", json={}).json()
        assert v["mission"]["status"] == "arrêtée" and not v["enabled"]
        assert c.post("/api/mission", json={"text": ""}).status_code == 400
