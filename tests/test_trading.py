from datetime import datetime, timedelta, timezone

import pytest

from analyzer.broker import PaperBroker, floor_step
from analyzer.config import Config
from analyzer.executor import Executor
from analyzer.mt5link import MT5Link
from tests.fake_mt5 import FakeMT5

UTC = timezone.utc


def make_sig(direction=1, entry=1080.08, sl=None, tp1=None, tp2=None, score=8.0, age_s=10, key=None):
    d = direction
    sl = sl if sl is not None else round(entry - d * 0.8, 2)   # 8 pips
    tp1 = tp1 if tp1 is not None else round(entry + d * 1.2, 2)
    tp2 = tp2 if tp2 is not None else round(entry + d * 2, 2)
    close = datetime.now(UTC) - timedelta(seconds=age_s)
    return {"key": key or f"{close:%Y%m%d%H%M}{'L' if d == 1 else 'S'}", "direction": d,
            "side": "ACHAT" if d == 1 else "VENTE", "pattern": "Avalement", "score": score,
            "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "close_time": close.isoformat()}


@pytest.fixture()
def mt5_bot(tmp_path):
    def build(**kw):
        cfg = Config()
        cfg.trading.risk_percent = 1.0
        cfg.trading.quality_edge = False      # contrôle qualité testé dans test_quality.py
        for k, v in kw.pop("trading", {}).items():
            setattr(cfg.trading, k, v)
        fake = FakeMT5(**kw)
        ex = Executor(cfg, tmp_path, link=MT5Link(fake))
        return ex, fake
    return build


def test_floor_step():
    assert floor_step(0.129, 0.01) == 0.12
    assert floor_step(0.3, 0.1) == 0.3


def test_lot_sizing_and_split(mt5_bot):
    ex, fake = mt5_bot(balance=1000.0)
    ex.enable()
    done = ex.on_signal(make_sig())
    # risque 1 % de 1000 = 10 $ ; stop ≈ 8 pips depuis l'ask -> ≈ 0.12 lot, coupé en 0.06 + 0.06
    assert [d["leg"] for d in done] == [1, 2]
    assert sum(d["volume"] for d in done) == pytest.approx(0.12)
    risk = sum(abs(r["price"] - r["sl"]) * r["volume"] * 100 for r in fake.requests)
    assert risk <= 10.0 + 1e-6, "le risque réel ne doit jamais dépasser le risque prévu"
    assert all(r["type_filling"] == fake.ORDER_FILLING_IOC for r in fake.requests)  # filling_mode=2
    assert all(r["magic"] == 730526 and len(r["comment"]) <= 31 for r in fake.requests)


def test_disabled_does_nothing(mt5_bot):
    ex, fake = mt5_bot()
    assert ex.on_signal(make_sig()) == [] and not fake.requests


def test_real_account_refused_by_default(mt5_bot):
    ex, fake = mt5_bot(demo=False)
    with pytest.raises(PermissionError):
        ex.enable()
    ex2, _ = mt5_bot(demo=False, trading={"allow_live": True})
    with pytest.raises(PermissionError):
        ex2.enable(confirm_live=False)
    assert ex2.enable(confirm_live=True)["enabled"]


def test_algo_trading_off(mt5_bot):
    ex, _ = mt5_bot(algo=False)
    with pytest.raises(PermissionError, match="Algo Trading"):
        ex.enable()


@pytest.mark.parametrize("kw, sig_kw, reason", [
    ({}, {"score": 6.5}, "score"),
    ({}, {"age_s": 600}, "trop ancien"),
    ({"spread": 0.8}, {}, "spread"),
    ({"bid": 1081}, {}, "trop bougé"),
    ({"balance": 50.0}, {}, "lot minimum"),
])
def test_guards(mt5_bot, kw, sig_kw, reason):
    ex, fake = mt5_bot(**kw)
    ex.enable()
    assert ex.on_signal(make_sig(**sig_kw)) == []
    assert not fake.requests
    if reason != "score":
        assert reason in ex.store.q("SELECT message FROM bot_log ORDER BY id DESC LIMIT 1")[0]["message"]


def test_no_duplicate_and_max_open(mt5_bot):
    ex, fake = mt5_bot()
    ex.enable()
    s = make_sig()
    assert ex.on_signal(s)
    assert ex.on_signal(s) == []                      # même signal : jamais deux fois
    assert ex.on_signal(make_sig(key="202601010000L")) == []   # max 1 trade ouvert
    assert len(fake.requests) == 2


def test_breakeven_after_tp1(mt5_bot):
    ex, fake = mt5_bot(trading={"trailing": False})
    ex.enable()
    done = ex.on_signal(make_sig())
    t1, t2 = done[0]["ticket"], done[1]["ticket"]
    fake.bid = done[0]["tp"] + 0.02                 # prix au-delà de TP1
    fake.hit(t1, done[0]["tp"])
    ex.manage()
    assert fake.positions[t2].sl == pytest.approx(fake.positions[t2].price_open)
    rows = {r["ticket"]: r for r in ex.store.q("SELECT * FROM trades")}
    assert rows[t1]["status"] == "fermé" and rows[t1]["profit"] > 0
    assert rows[t2]["be_done"] == 1


def test_max_hold_close(mt5_bot):
    ex, fake = mt5_bot()
    ex.enable()
    done = ex.on_signal(make_sig())
    for p in fake.positions.values():
        p.time -= 5 * 3600                              # ouvertes il y a 5 h
    ex.manage()
    assert not fake.positions
    assert {r["status"] for r in ex.store.q("SELECT status FROM trades")} == {"fermé"}
    assert done


def test_daily_loss_pause(mt5_bot):
    ex, fake = mt5_bot(balance=1000.0)
    ex.enable()
    from types import SimpleNamespace as NS
    fake.deals.append(NS(magic=730526, time=int(datetime.now(UTC).timestamp()), profit=-25.0,
                         commission=0, swap=0, fee=0, position_id=1))
    assert ex.on_signal(make_sig()) == []
    assert ex.paused and "perte du jour" in ex.paused


def test_broker_rejection_logged(mt5_bot):
    ex, fake = mt5_bot(reject=10019)
    ex.enable()
    assert ex.on_signal(make_sig()) == []
    assert "Fonds insuffisants" in ex.store.q("SELECT message FROM trades LIMIT 1")[0]["message"]


def test_close_all(mt5_bot):
    ex, fake = mt5_bot()
    ex.enable()
    ex.on_signal(make_sig())
    msgs = ex.close_all()
    assert len(msgs) == 2 and not fake.positions and not ex.enabled


def test_paper_broker_full_cycle(tmp_path):
    cfg = Config()
    cfg.trading.mode = "paper"
    cfg.trading.quality_edge = False
    ex = Executor(cfg, tmp_path, paper=PaperBroker(10_000))
    ex.manage({"close": 1080})
    ex.enable()
    done = ex.on_signal(make_sig(entry=1080))
    assert len(done) == 2
    ex.manage({"close": 1081.3, "high": 1081.35, "low": 1079.9})   # TP1 touché
    ex.manage({"close": 1082, "high": 1082.3, "low": 1081})   # TP2 touché
    st = ex.status()
    assert not st["positions"] and st["account"]["balance"] > 10_000
    assert {r["status"] for r in st["trades"]} == {"fermé"}


def test_trailing_stop_follows_gains(mt5_bot):
    ex, fake = mt5_bot(trading={"split_targets": False, "breakeven": False})
    ex.enable()
    done = ex.on_signal(make_sig())
    t = done[0]["ticket"]
    pos = fake.positions[t]
    risk = pos.price_open - pos.sl
    fake.bid = pos.price_open + 0.5 * risk                  # +0,5 R : pas encore actif
    ex.manage()
    assert fake.positions[t].sl == pytest.approx(pos.price_open - risk)
    fake.bid = round(pos.price_open + 1.4 * risk, 2)        # +1,4 R : stop à +0,7 R
    ex.manage()
    sl1 = fake.positions[t].sl
    assert sl1 == pytest.approx(pos.price_open + 0.7 * risk, abs=2e-5)
    fake.bid = round(pos.price_open + 1.0 * risk, 2)        # le prix recule : le stop ne recule jamais
    ex.manage()
    assert fake.positions[t].sl == pytest.approx(sl1)
    fake.bid = round(pos.price_open + 2.0 * risk, 2)        # nouveau sommet : le stop remonte
    ex.manage()
    assert fake.positions[t].sl == pytest.approx(pos.price_open + 1.3 * risk, abs=2e-5)
    assert any("Stop suiveur" in r["message"] for r in ex.store.q("SELECT * FROM bot_log"))


def _rev(later, **kw):
    info = {"time": (later + timedelta(minutes=5)).isoformat(), "close": 1079, "candidate": None,
            "momentum": {1: False, -1: False}, "swing_low": None, "swing_high": None}
    info.update(kw)
    return {"close": 1080, "high": 1080, "low": 1080, "reversal": info}


def test_reversal_closes_position(mt5_bot):
    ex, fake = mt5_bot(trading={"trailing": False})
    ex.enable()
    ex.on_signal(make_sig())
    later = datetime.now(UTC) + timedelta(minutes=5)
    weak = {"key": "x1", "direction": -1, "score": 5.0, "pattern": "Étoile filante", "blocked": None}
    ex.manage(_rev(later, candidate=weak))
    assert len(ex.broker.positions("XAUUSD", 730526)) == 2
    ex.manage(_rev(later + timedelta(minutes=5), candidate=weak | {"score": 7.5}))
    assert ex.broker.positions("XAUUSD", 730526) == []
    assert any("Retournement" in r["message"] for r in ex.store.q("SELECT * FROM bot_log"))


def test_reversal_momentum_and_structure(mt5_bot):
    ex, fake = mt5_bot(trading={"trailing": False, "reversal_momentum": True})
    ex.enable()
    ex.on_signal(make_sig())
    later = datetime.now(UTC) + timedelta(minutes=5)
    ex.manage(_rev(later, momentum={1: True, -1: False}))
    assert ex.broker.positions("XAUUSD", 730526) == []
    # structure : creux formé APRÈS l'entrée puis cassé
    ex, fake = mt5_bot(trading={"trailing": False, "reversal_structure": True})
    ex.enable()
    assert ex.on_signal(make_sig(key="202601020000L"))
    old_low = {"price": 1079.5, "time": (later - timedelta(hours=8)).isoformat()}
    ex.manage(_rev(later, swing_low=old_low))                       # creux d'avant l'entrée : ignoré
    assert len(ex.broker.positions("XAUUSD", 730526)) == 2
    new_low = {"price": 1079.5, "time": later.isoformat()}
    ex.manage(_rev(later + timedelta(minutes=5), swing_low=new_low))
    assert ex.broker.positions("XAUUSD", 730526) == []
    assert any("cassure du dernier creux" in r["message"] for r in ex.store.q("SELECT * FROM bot_log"))


def test_low_score_trades_with_reduced_risk(mt5_bot):
    ex, fake = mt5_bot(balance=1000.0)
    ex.enable()
    assert ex.on_signal(make_sig(score=5.5)) == []           # désactivé par défaut
    ex, fake = mt5_bot(balance=1000.0, trading={"low_score_enabled": True, "low_score_min": 5.0,
                                                "low_score_risk_factor": 0.5})
    ex.enable()
    done = ex.on_signal(make_sig(score=5.5))
    assert sum(d["volume"] for d in done) == pytest.approx(0.06)   # moitié du lot normal (0,12)
    assert ex.on_signal(make_sig(score=4.5, key="202601010000S", direction=-1)) == []


def test_refusals_are_explained(mt5_bot):
    ex, fake = mt5_bot()
    ex.on_signal(make_sig())                                     # robot désactivé
    ex.enable()
    ex.on_signal(make_sig(score=6.0, key="202601010000S", direction=-1))   # score sous le minimum
    codes = {s["code"] for s in ex.skip_stats()}
    assert {"disabled", "score"} <= codes
    st = ex.status()
    assert any("score" in r["text"] for r in st["readiness"])


def test_order_test_opens_and_closes(mt5_bot):
    ex, fake = mt5_bot()
    r = ex.test_order()
    assert r["ok"] and r["closed"] and not fake.positions
    ex, fake = mt5_bot(demo=False)
    with pytest.raises(PermissionError):
        ex.test_order()


def test_pullback_entry_waits_for_level(mt5_bot):
    ex, fake = mt5_bot(trading={"entry_mode": "pullback", "pullback_r": 0.3, "trailing": False})
    ex.enable()
    sig = make_sig(entry=1080.08)                          # stop 8 pips -> repli attendu 2,4 pips
    assert ex.on_signal(sig) == [] and not fake.requests and len(ex._pending) == 1
    level = ex._pending[sig["key"]]["level"]
    assert level == pytest.approx(1080.08 - 0.3 * 0.8)
    fake.bid = 1080                                     # ask 1080.08 : pas encore au niveau
    ex.manage()
    assert not fake.requests
    fake.bid = round(level - fake.spread, 2)               # ask = niveau
    ex.manage()
    assert fake.requests and not ex._pending
    assert all(abs(r["price"] - level) < 2e-5 for r in fake.requests)
    assert ex.status()["pending"] == []


def test_pullback_cancelled_when_tp1_reached(mt5_bot):
    ex, fake = mt5_bot(trading={"entry_mode": "pullback"})
    ex.enable()
    sig = make_sig()
    ex.on_signal(sig)
    fake.bid = sig["tp1"] + 0.01
    ex.manage()
    assert not fake.requests and not ex._pending
    assert any(s["code"] == "pullback" for s in ex.skip_stats())


def test_profiles_apply_safely():
    from analyzer.config import PROFILES, apply_profile
    cfg = Config()
    cfg.trading.mode = "paper"
    for name in PROFILES:
        c = apply_profile(cfg, name)
        assert c.trading.mode == "paper" and c.trading.allow_live is False
    assert apply_profile(cfg, "actif").trading.max_trades_per_day == 12
    assert apply_profile(cfg, "debutant").trading.risk_percent == 0.25
    with pytest.raises(ValueError):
        apply_profile(cfg, "magique")


def test_quick_profit_single_close_target(mt5_bot):
    ex, fake = mt5_bot(trading={"quick_target_r": 0.6})
    ex.enable()
    sig = make_sig()
    done = ex.on_signal(sig)
    assert len(done) == 1                                        # une seule position, un seul objectif
    req = fake.requests[-1]
    expected = round(sig["entry"] + 0.6 * abs(sig["entry"] - sig["sl"]), 2)
    assert req["tp"] == pytest.approx(expected, abs=1e-5)


def test_manual_place_works_with_robot_disabled(mt5_bot):
    ex, fake = mt5_bot(trading={"min_score": 9.0})
    sig = make_sig(score=5.0)                                # sous le minimum : le robot ne l'aurait pas pris
    p = ex.preview_manual(sig)
    assert p["volume"] > 0 and p["side"] == "ACHAT" and not fake.requests
    done = ex.place_manual(sig)
    assert done and fake.requests and not ex.enabled
    with pytest.raises(ValueError):                          # pas deux fois
        ex.place_manual(sig)


def test_manual_place_guards(mt5_bot):
    ex, fake = mt5_bot(demo=False)
    with pytest.raises(PermissionError):
        ex.place_manual(make_sig())
    ex, fake = mt5_bot()
    old = make_sig(key="202601030000L")
    old["close_time"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with pytest.raises(ValueError):
        ex.place_manual(old)
