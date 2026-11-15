"""v2.1 : contexte macro, figures propres à l'or, entrées sur retest et assistant Claude."""
import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from analyzer import assistant, macro
from analyzer.config import Config
from analyzer.executor import Executor
from analyzer.mt5link import MT5Link
from analyzer.server import create_app
from analyzer.strategy import Analyzer
from tests.fake_mt5 import FakeMT5
from tests.test_analyzer import synthetic
from tests.test_trading import make_sig


# ------------------------------------------------------------------ macro
def _hourly(df, slope):
    h1 = df["close"].resample("1h").last().dropna()
    return pd.Series(100 + slope * np.arange(len(h1)), index=h1.index)


def test_macro_no_lookahead():
    df = synthetic()
    s = _hourly(df, 0.01)
    full = macro.attach(df, {"dxy": s})
    cut = df.index[3000]
    part = macro.attach(df.loc[:cut], {"dxy": s[s.index + pd.Timedelta(hours=1) <= cut]})
    a, b = full.loc[cut], part.loc[cut]
    assert a["dxy"] == b["dxy"] and a["dxy_trend"] == b["dxy_trend"]
    # la H1 ouverte à 10 h n'est connue qu'à 11 h
    t = df.index[(df.index.hour == 10) & (df.index.minute == 30)][5]
    assert full.loc[t, "dxy"] == s[t.floor("h") - pd.Timedelta(hours=1)]


def test_gold_bias_and_factor():
    assert macro.gold_bias(-1, -1) == 1 and macro.gold_bias(1, 0) == -1 and macro.gold_bias(0, 1) == 0
    df = synthetic()
    up = macro.attach(df, {"dxy": _hourly(df, 0.02), "us10y": _hourly(df, 0.001)})   # dollar ↑ taux ↑
    an = Analyzer(up, Config())
    sig = next(s for s in (an.inspect(i) for i in range(2000, len(an.c))) if s)
    lab = [f.label for f in sig.factors if f.label.startswith("Macro")]
    assert lab and sig.macro == -1
    assert ("contre" in lab[0]) == (sig.direction == 1)
    d = macro.describe(up.iloc[-1])
    assert d["gold_bias"] == -1 and {i["key"] for i in d["items"]} == {"dxy", "us10y"}
    # sans données macro : aucun facteur ajouté
    plain = Analyzer(df, Config())
    assert not any(f.label.startswith("Macro") for s in (plain.inspect(i) for i in range(2000, 2400)) if s
                   for f in s.factors)


# ------------------------------------------------------------------ figures de l'or
def _day(start, closes, highs=None, lows=None):
    idx = pd.date_range(start, periods=len(closes), freq="5min", tz="UTC")
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + 0.3 if highs is None else np.asarray(highs, float)
    lo = np.minimum(o, c) - 0.3 if lows is None else np.asarray(lows, float)
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": 1.0}, index=idx)


def test_asian_range_breakout_detected():
    rng = np.random.default_rng(3)
    base = 4000 + rng.normal(0, 0.8, 288 * 3).cumsum() * 0.3          # 3 jours calmes
    df = _day("2026-03-02", base)
    day = df.index.normalize()[-1]
    asia = df[(df.index >= day) & (df.index < day + pd.Timedelta(hours=7))]
    hi = asia["high"].max()
    k = df.index.get_loc(day + pd.Timedelta(hours=8))
    df.iloc[k, df.columns.get_loc("open")] = df["close"].iloc[k - 1]
    df.iloc[k, df.columns.get_loc("close")] = hi + 3.0
    df.iloc[k, df.columns.get_loc("high")] = hi + 3.2
    df.iloc[k, df.columns.get_loc("low")] = df["close"].iloc[k - 1] - 0.1
    df.iloc[k - 1, df.columns.get_loc("close")] = min(df["close"].iloc[k - 1], hi - 0.1)
    cfg = Config()
    an = Analyzer(df, cfg)
    names = [p.name for p in an.gold_setups(k, an.atr[k])]
    assert "Cassure du range asiatique haussière" in names
    assert an._retest[k] == pytest.approx(hi)
    cfg.strategy.asian_breakout = False
    assert not Analyzer(df, cfg).gold_setups(k, an.atr[k])


def test_prev_day_sweep_detected():
    rng = np.random.default_rng(5)
    df = _day("2026-03-02", 4000 + rng.normal(0, 0.8, 288 * 3).cumsum() * 0.3)
    an0 = Analyzer(df, Config())
    k = len(df) - 100
    pdh = an0.pdh[k]
    assert not np.isnan(pdh)
    df.iloc[k, df.columns.get_loc("open")] = pdh - 0.5
    df.iloc[k, df.columns.get_loc("high")] = pdh + 3.0
    df.iloc[k, df.columns.get_loc("close")] = pdh - 1.0
    df.iloc[k, df.columns.get_loc("low")] = pdh - 1.2
    an = Analyzer(df, Config())
    hits = [p for p in an.gold_setups(k, an.atr[k]) if p.name == "Balayage du plus haut de la veille"]
    assert hits and hits[0].direction == -1 and an._retest[k] == pytest.approx(pdh)


# ------------------------------------------------------------------ robot : retest et Claude
@pytest.fixture()
def bot(tmp_path):
    def build(**trading):
        cfg = Config()
        cfg.trading.quality_edge = False
        for k, v in trading.items():
            setattr(cfg.trading, k, v)
        fake = FakeMT5()
        ex = Executor(cfg, tmp_path, link=MT5Link(fake))
        ex.enable()
        return ex, fake
    return build


def test_retest_entry_waits_for_level(bot):
    ex, fake = bot(trailing=False)
    sig = dict(make_sig(entry=1080.08, sl=1079.0, tp1=1081.7, tp2=1082.8), retest=1079.6, atr_pips=40)
    assert ex.on_signal(sig) == [] and ex._pending[sig["key"]]["kind"] == "retest"
    level = ex._pending[sig["key"]]["level"]
    assert level == pytest.approx(1079.6 + 0.4)   # 0,1 ATR avant le niveau
    fake.bid = round(level - fake.spread, 2)
    ex.manage()
    assert fake.requests and all(abs(r["price"] - level) < 1e-6 for r in fake.requests)


def test_ai_validation_blocks_or_confirms(bot):
    ex, fake = bot(ai_confirm=True, ai_min_confidence=60, trailing=False, retest_entries=False)
    seen = {}

    def reviewer(sig, checks):
        seen.update(pattern=sig["pattern"], n=len(checks))
        return {"decision": "IGNORER", "confidence": 80, "reason": "zone opposée trop proche"}
    ex.ai_reviewer, ex.ai_name = reviewer, (lambda: "Groq")
    assert ex.on_signal(make_sig(key="K1")) == [] and not fake.requests
    assert seen["pattern"] == "Avalement" and seen["n"] >= 1
    assert any(s["code"] == "ai" for s in ex.skip_stats())
    ex.ai_reviewer = lambda *a: {"decision": "PLACER", "confidence": 55, "reason": "moyen"}
    assert ex.on_signal(make_sig(key="K2")) == []                    # confiance trop basse
    ex.ai_reviewer = lambda *a: {"decision": "PLACER", "confidence": 75, "reason": "structure propre"}
    done = ex.on_signal(make_sig(key="K3"))
    assert done and fake.requests
    checks = json.loads(ex.store.q("SELECT analysis FROM trades WHERE key='K3' LIMIT 1")[0]["analysis"])
    assert checks[-1]["id"] == "ai" and checks[-1]["status"] == "ok" and checks[-1]["label"] == "Validation Groq"


def test_ai_default_is_free_rule_review(bot):
    ex, fake = bot(ai_confirm=True, ai_min_confidence=50, trailing=False, retest_entries=False)
    good = dict(make_sig(key="G1", score=8.5), mtf={"m15": 1, "h1": 1, "h4": 1}, macro=1)
    assert ex.on_signal(good) and fake.requests                      # analyste intégré : aucune clé
    bad = dict(make_sig(key="B1", score=6.5), mtf={"m15": 1, "h1": 1, "h4": -1}, macro=-1)
    n = len(fake.requests)
    ex.store.x("UPDATE trades SET status='fermé'")
    assert ex.on_signal(bad) == [] and len(fake.requests) == n


def test_ai_error_policy(bot):
    ex, fake = bot(ai_confirm=True, ai_block_on_error=False, retest_entries=False)

    def boom(*a):
        raise assistant.AssistantError("Groq injoignable")
    ex.ai_reviewer = boom
    assert ex.on_signal(make_sig()) and fake.requests                # non bloquant si demandé
    ex2, fake2 = bot(ai_confirm=True, ai_block_on_error=True, retest_entries=False)
    ex2.ai_reviewer = boom
    assert ex2.on_signal(make_sig(key="X")) == [] and not fake2.requests


# ------------------------------------------------------------------ analyste intégré
def test_rule_review_logic():
    from analyzer.analyst import rule_review
    base = dict(make_sig(score=8), mtf={"m15": 1, "h1": 1, "h4": 1}, macro=1, atr_pips=40)
    good = rule_review(base, [])
    assert good["decision"] == "PLACER" and good["confidence"] >= 70
    snap = {"zones": [{"price": base["entry"] + 0.5, "touches": 3, "kind": "resistance"}]}   # 0,6 R
    blocked = rule_review(dict(base, score=7, macro=0), [], snap)
    assert blocked["decision"] == "IGNORER" and "zone opposée" in blocked["reason"]
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    news = rule_review(dict(base, score=7, macro=0, mtf={}), [], None, {"news": [{"time": soon, "title": "NFP"}]})
    assert news["confidence"] < good["confidence"] and "NFP" in news["reason"]


# ------------------------------------------------------------------ fournisseurs compatibles OpenAI
class _Resp:
    def __init__(self, lines):
        self.lines = lines

    def __iter__(self):
        return iter(self.lines)

    def read(self):
        return b"".join(self.lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_openai_compatible_stream_and_models(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None, context=None):
        calls.append((req.full_url, dict(req.header_items()), req.data))
        if req.full_url.endswith("/models"):
            return _Resp([json.dumps({"data": [{"id": "llama-3.3-70b-versatile"}, {"id": "whisper-large-v3"},
                                               {"id": "models/gemini-2.5-flash"}]}).encode()])
        chunks = [{"choices": [{"delta": {"content": "Or "}}]}, {"choices": [{"delta": {"content": "haussier"}}]}]
        return _Resp([f"data: {json.dumps(c)}\n".encode() for c in chunks] + [b"data: [DONE]\n"])
    monkeypatch.setattr(assistant.urllib.request, "urlopen", fake_urlopen)
    out = "".join(assistant.stream("groq", "gsk_test", "llama-3.3-70b-versatile", "SYS",
                                   [{"role": "user", "content": "q"}]))
    assert out == "Or haussier"
    url, headers, data = calls[-1]
    body = json.loads(data)
    assert url == "https://api.groq.com/openai/v1/chat/completions" and headers["Authorization"] == "Bearer gsk_test"
    assert body["messages"][0] == {"role": "system", "content": "SYS"} and body["stream"] is True
    assert assistant.list_models("groq", "gsk_test") == ["gemini-2.5-flash", "llama-3.3-70b-versatile"]
    assistant.list_models("ollama", None, "http://localhost:11434/v1")
    assert calls[-1][0] == "http://localhost:11434/v1/models" and "Authorization" not in calls[-1][1]
    assert assistant.model_for("groq", "claude-sonnet-5-5") == "llama-3.3-70b-versatile"
    assert assistant.model_for("claude", "") == "claude-sonnet-5-5"


# ------------------------------------------------------------------ assistant
def test_assistant_helpers(tmp_path):
    with pytest.raises(ValueError):
        assistant.set_key(tmp_path, "abc", "claude")
    with pytest.raises(ValueError):
        assistant.set_key(tmp_path, "sk-ant-xxxxxxxxxxxx", "groq")
    with pytest.raises(ValueError):
        assistant.set_key(tmp_path, "x" * 20, "integre")
    assistant.set_key(tmp_path, "sk-ant-abcdef9876", "claude")
    assistant.set_key(tmp_path, "gsk_abcdefghij1234", "groq")
    st = assistant.status(tmp_path, "groq", "")
    assert st["configured"] and st["key_hint"].endswith("1234") and "abcdefghij" not in json.dumps(st)
    assert {p["id"]: p["configured"] for p in st["providers"]} == \
        {"integre": True, "groq": True, "gemini": False, "ollama": True, "claude": True}
    assistant.set_key(tmp_path, "", "groq")
    assert not assistant.get_key(tmp_path, "groq") and assistant.get_key(tmp_path, "claude")
    (tmp_path / "anthropic_key.txt").write_text("sk-ant-old11112222")     # clé de la v2.1.0
    (tmp_path / "ai_keys.json").unlink()
    assert assistant.get_key(tmp_path, "claude") == "sk-ant-old11112222"
    h = assistant.clean_history([{"role": "assistant", "content": "x"}, {"role": "user", "content": "a"},
                                 {"role": "user", "content": "b"}])
    assert h == [{"role": "user", "content": "a\n\nb"}]
    with pytest.raises(ValueError):
        assistant.clean_history([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    r = assistant.parse_review('Voici : {"decision": "placer", "confiance": 140, "raison": "ok"}')
    assert r == {"decision": "PLACER", "confidence": 100, "reason": "ok"}
    with pytest.raises(assistant.AssistantError):
        assistant.parse_review("je ne sais pas")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = Config(source="demo", data_dir=str(tmp_path / "data"))
    with TestClient(create_app(cfg, str(tmp_path / "config.yaml"))) as c:
        yield c


def test_state_has_gold_context(client):
    s = client.get("/api/state").json()
    assert s["macro"] and s["macro"]["items"]
    assert {"asia_high", "asia_low", "prev_day_high", "prev_day_low"} <= set(s["levels"])
    ctx = client.get("/api/assistant/context").json()["context"]
    assert "Range asiatique" in ctx and "Dollar (DXY)" in ctx and "Prix" in ctx


def test_assistant_free_by_default(client):
    st = client.get("/api/assistant").json()
    assert st["provider"] == "integre" and st["configured"] and not st["needs_key"]
    for intent, mark in [("marche", "### Scénario"), ("session", "### Session"), ("journal", "### Journal"),
                         ("annonces", "annonce"), ("signal", "")]:
        r = client.post("/api/assistant/chat", json={"messages": [{"role": "user", "content": "x"}], "intent": intent})
        assert r.status_code == 200 and mark.lower() in r.text.lower() and len(r.text) > 40, intent
    r = client.post("/api/assistant/chat", json={"messages": [{"role": "user", "content": "Analyse mes derniers signaux"}]})
    assert "### Journal" in r.text
    rv = client.post("/api/assistant/review", json={})
    if rv.status_code == 200:
        assert rv.json()["decision"] in ("PLACER", "IGNORER") and rv.json()["by"] == "Analyste intégré"


def test_assistant_online_providers(client, monkeypatch):
    assert client.put("/api/settings", json={"assistant_provider": "groq"}).status_code == 200
    r = client.post("/api/assistant/chat", json={"messages": [{"role": "user", "content": "Analyse"}]})
    assert r.status_code == 400 and "clé API Groq" in r.json()["detail"]
    assert client.put("/api/assistant/key", json={"key": "pas-une-cle", "provider": "groq"}).status_code == 400
    st = client.put("/api/assistant/key", json={"key": "gsk_zzzzzzzz1111", "provider": "groq"}).json()
    assert st["configured"] and st["key_hint"].endswith("1111") and st["model"] == "llama-3.3-70b-versatile"

    sent = {}

    def fake_stream(provider, key, model, system, messages, **kw):
        sent.update(provider=provider, key=key, model=model, messages=messages)
        yield "L'or est "
        yield "**haussier**."
    monkeypatch.setattr(assistant, "stream", fake_stream)
    r = client.post("/api/assistant/chat", json={"messages": [{"role": "user", "content": "Analyse"}]})
    assert r.status_code == 200 and r.text == "L'or est **haussier**."
    assert sent["provider"] == "groq" and sent["key"] == "gsk_zzzzzzzz1111"
    assert "<dossier_marche>" in sent["messages"][-1]["content"]

    monkeypatch.setattr(assistant, "complete",
                        lambda *a, **k: '{"decision": "IGNORER", "confiance": 70, "raison": "macro contraire"}')
    r = client.post("/api/assistant/review", json={})
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert r.json()["decision"] == "IGNORER" and r.json()["by"] == "Groq"

    assert client.put("/api/settings", json={"assistant_provider": "chatgpt"}).status_code == 400
    assert client.put("/api/settings", json={"assistant_provider": "ollama", "assistant_model": "qwen2.5:7b"}).status_code == 200
    st = client.get("/api/assistant").json()
    assert st["provider"] == "ollama" and st["configured"] and st["model"] == "qwen2.5:7b"
    m = client.get("/api/assistant/models").json()                       # Ollama non lancé ici
    assert m["provider"] == "ollama" and (m["models"] or m["error"])
