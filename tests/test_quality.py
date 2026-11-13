import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from analyzer.config import Config
from analyzer.executor import Executor
from analyzer.mt5link import MT5Link
from analyzer.quality import QualityGate
from tests.fake_mt5 import FakeMT5
from tests.test_analyzer import synthetic
from tests.test_trading import make_sig

UTC = timezone.utc


def sig_q(**kw):
    s = make_sig(**{k: v for k, v in kw.items() if k in ("direction", "score", "entry")})
    s.update(pattern=kw.get("pattern", "Avalement haussier"), atr_pips=kw.get("atr", 40.0),
             mtf=kw.get("mtf", {"m15": 1, "h1": 1, "h4": 0}))
    return s


def gate_with_edge(tmp_path, exp=0.2, trades=40, hour_exp=0.1):
    g = QualityGate(tmp_path, fetcher=lambda: [])
    hour = datetime.now(UTC).hour
    g.edge.data = {"computed_at": datetime.now(UTC).isoformat(), "period": {}, "score_min": 7, "stats": {},
                   "patterns": {"Avalement haussier": {"trades": trades, "win_rate": 55, "expectancy_r": exp,
                                                       "total_r": exp * trades, "profit_factor": 1.6 if exp > 0 else 0.7}},
                   "hours": {str(hour): {"trades": 30, "win_rate": 50, "expectancy_r": hour_exp, "total_r": 3}}}
    return g


def statuses(checks):
    return {c["id"]: c["status"] for c in checks}


def test_edge_compute_on_history(tmp_path):
    g = QualityGate(tmp_path, fetcher=lambda: [])
    data = g.edge.compute(synthetic(), Config())
    assert data["patterns"] and (tmp_path / "edge.json").exists()
    s = g.edge.summary(Config().trading)
    assert s["ready"] and {r["pattern"] for r in s["patterns"]} == set(data["patterns"])
    assert QualityGate(tmp_path).edge.data["computed_at"] == data["computed_at"]   # rechargé du disque


def test_profitable_pattern_passes(tmp_path):
    ok, checks = gate_with_edge(tmp_path).check(sig_q(), Config())
    assert ok and statuses(checks)["edge"] == "ok" and statuses(checks)["mtf"] == "ok"


def test_small_sample_not_blocking_by_default(tmp_path):
    ok, checks = gate_with_edge(tmp_path, trades=8).check(sig_q(), Config())
    assert ok and statuses(checks)["edge"] == "skip"
    cfg = Config()
    cfg.trading.edge_block_unproven = True
    ok, checks = gate_with_edge(tmp_path, trades=8).check(sig_q(), cfg)
    assert not ok and statuses(checks)["edge"] == "fail"


@pytest.mark.parametrize("edge_kw, sig_kw, failing", [
    ({"exp": -0.1}, {}, "edge"),                                    # figure perdante
    ({"hour_exp": -0.3}, {}, "hour"),                               # heure perdante
    ({}, {"mtf": {"m15": 1, "h1": 1, "h4": -1}}, "mtf"),            # H4 contre le trade
    ({}, {"mtf": {"m15": 0, "h1": 1, "h4": 0}}, "mtf"),             # une seule UT alignée
    ({}, {"atr": 5.0}, "vol"),                                      # marché mort
    ({}, {"atr": 200}, "vol"),                                       # panique
    ({}, {"score": 6.5}, "score"),
])
def test_quality_blocks(tmp_path, edge_kw, sig_kw, failing):
    ok, checks = gate_with_edge(tmp_path, **edge_kw).check(sig_q(**sig_kw), Config())
    assert not ok and statuses(checks)[failing] == "fail"


def test_no_stats_not_blocking_until_computed(tmp_path):
    ok, checks = QualityGate(tmp_path, fetcher=lambda: []).check(sig_q(), Config())
    edge = [c for c in checks if c["id"] == "edge"][0]
    assert ok and edge["status"] == "skip" and "Recalculer" in edge["detail"]
    cfg = Config()
    cfg.trading.edge_block_unproven = True
    ok, _ = QualityGate(tmp_path, fetcher=lambda: []).check(sig_q(), cfg)
    assert not ok


def test_news_filter(tmp_path):
    soon = (datetime.now(UTC) + timedelta(minutes=12)).isoformat()
    events = [{"country": "USD", "title": "Non-Farm Payrolls", "impact": "High", "date": soon},
              {"country": "JPY", "title": "BoJ", "impact": "High", "date": soon},
              {"country": "EUR", "title": "PMI", "impact": "Medium", "date": soon}]
    g = gate_with_edge(tmp_path)
    g.news.fetcher = lambda: events
    g.news.refresh(force=True)
    ok, checks = g.check(sig_q(), Config())
    news = [c for c in checks if c["id"] == "news"][0]
    assert not ok and news["status"] == "fail" and "Non-Farm" in news["detail"] and "BoJ" not in news["detail"]
    assert [e["title"] for e in g.news.upcoming(datetime.now(UTC))] == ["Non-Farm Payrolls"]   # l'or : annonces USD seulement


def test_news_unavailable(tmp_path):
    def boom():
        raise OSError("pas de réseau")
    g = gate_with_edge(tmp_path)
    g.news.fetcher = boom
    g.news.refresh(force=True)
    assert g.check(sig_q(), Config())[0]                       # non bloquant par défaut
    cfg = Config()
    cfg.trading.news_block_on_error = True
    assert not g.check(sig_q(), cfg)[0]


def test_executor_refuses_then_accepts_with_report(tmp_path):
    cfg = Config()
    fake = FakeMT5(bid=1080)
    ex = Executor(cfg, tmp_path, link=MT5Link(fake), quality=gate_with_edge(tmp_path, exp=-0.2))
    ex.enable()
    assert ex.on_signal(sig_q()) == [] and not fake.requests
    assert "contrôle qualité" in ex.store.q("SELECT message FROM bot_log ORDER BY id DESC LIMIT 1")[0]["message"]

    ex.quality = gate_with_edge(tmp_path, exp=0.25)
    s = sig_q()
    s["key"] = "202611101200L"
    done = ex.on_signal(s)
    assert len(done) == 2
    trade = ex.status()["trades"][0]
    assert {c["id"] for c in trade["analysis"]} >= {"score", "edge", "hour", "mtf", "news", "vol"}
    assert ex.status()["quality"]["last"]["passed"] is True


def test_old_database_migrated(tmp_path):
    db = sqlite3.connect(tmp_path / "trades.db")
    db.execute("""CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, leg INTEGER NOT NULL,
        mode TEXT NOT NULL, ticket INTEGER, side TEXT NOT NULL, volume REAL NOT NULL, price REAL, sl REAL NOT NULL,
        tp REAL NOT NULL, tp1 REAL NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '', profit REAL,
        be_done INTEGER NOT NULL DEFAULT 0, time TEXT NOT NULL, close_time TEXT)""")
    db.commit(); db.close()
    ex = Executor(Config(), tmp_path)
    cols = {r["name"] for r in ex.store.q("PRAGMA table_info(trades)")}
    assert "analysis" in cols


def test_quality_status_through_api(tmp_path):
    """Les statistiques réelles (types pandas/numpy) doivent passer en JSON dans l'API."""
    from fastapi.testclient import TestClient

    from analyzer.server import create_app

    cfg = Config(source="demo", data_dir=str(tmp_path / "data"))
    cfg.trading.mode, cfg.trading.edge_bars = "paper", 4000
    with TestClient(create_app(cfg, str(tmp_path / "c.yaml"))) as c:
        r = c.post("/api/trading/quality/recompute")
        assert r.status_code == 200, r.text
        st = c.get("/api/trading")
        assert st.status_code == 200, st.text
        edge = st.json()["quality"]["edge"]
        assert edge["ready"] and all(p["qualified"] in (True, False, None) for p in edge["patterns"])
