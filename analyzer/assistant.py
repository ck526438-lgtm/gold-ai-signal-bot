"""Assistant IA : chat sur le marché de l'or et validation des trades avant envoi.

Plusieurs « cerveaux » au choix (onglet Assistant IA) :
* integre — analyste par règles, GRATUIT, hors ligne, sans clé (par défaut) ;
* groq    — IA en ligne gratuite (console.groq.com, sans carte : au-delà du quota, refus, pas de facture) ;
* gemini  — Google Gemini, clé gratuite sur aistudio.google.com (quotas limités) ;
* ollama  — IA locale sur le PC (ollama.com), gratuite et hors ligne ;
* claude  — Claude (Anthropic), payant à l'usage.

L'IA ne voit QUE le dossier préparé par l'application (prix, tendances, zones, macro, annonces,
statistiques, journal, robot). Elle ne « prédit » pas le marché : elle lit le dossier et donne un avis.

Clés : variables d'environnement (GROQ_API_KEY, GEMINI_API_KEY, ANTHROPIC_API_KEY) ou fichier
`ai_keys.json` du dossier de données (enregistré depuis l'interface, jamais renvoyé).
"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
PROVIDERS = {
    "integre": {"label": "Analyste intégré — gratuit, hors ligne, sans clé", "kind": "local", "key": False,
                "default_model": "", "signup": None},
    "groq": {"label": "Groq — IA en ligne gratuite (clé gratuite, sans carte)", "kind": "openai", "key": True,
             "base": "https://api.groq.com/openai/v1", "default_model": "llama-3.3-70b-versatile",
             "signup": "https://console.groq.com/keys", "env": ("GROQ_API_KEY",)},
    "gemini": {"label": "Google Gemini — clé gratuite (quotas limités)", "kind": "openai", "key": True,
               "base": "https://generativelanguage.googleapis.com/v1beta/openai", "default_model": "gemini-2.5-flash",
               "signup": "https://aistudio.google.com/apikey", "env": ("GEMINI_API_KEY", "GOOGLE_API_KEY")},
    "ollama": {"label": "Ollama — IA locale sur ton PC, gratuite", "kind": "openai", "key": False,
               "base": "http://localhost:11434/v1", "default_model": "llama3.1:8b",
               "signup": "https://ollama.com/download"},
    "claude": {"label": "Claude (Anthropic) — payant à l'usage", "kind": "anthropic", "key": True,
               "default_model": "claude-sonnet-5-5", "signup": "https://console.anthropic.com/settings/keys",
               "env": ("ANTHROPIC_API_KEY",)},
}
MODELS = [
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5 — équilibré (recommandé)"},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5 — le plus fin, plus lent et plus cher"},
    {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5 — rapide et économique"},
]
KEY_FILE = "ai_keys.json"
OLD_KEY_FILE = "anthropic_key.txt"


def model_for(provider: str, model: str | None) -> str:
    """Modèle choisi, sinon celui par défaut du fournisseur (un modèle Claude n'est pas envoyé à Groq…)."""
    p = PROVIDERS.get(provider, PROVIDERS["integre"])
    m = (model or "").strip()
    if not m or (provider != "claude" and m.startswith("claude-")) or (provider == "claude" and not m.startswith("claude-")):
        return p["default_model"]
    return m


CHAT_SYSTEM = """Tu es l'analyste de marché intégré à « Gold AI Signal Bot », une application d'analyse \
et de trading de l'or (XAUUSD) en M5 sur MetaTrader 5. Tu réponds en français, de façon claire, \
concrète et concise, à un trader qui utilise l'application.

Règles :
- Appuie-toi UNIQUEMENT sur le dossier de marché fourni (prix, tendances, zones, macro, annonces, \
statistiques, journal, robot). Cite les niveaux de prix exacts du dossier. Si une information manque, \
dis-le au lieu de l'inventer.
- Sur l'or, 1 pip = 0,10 $. Heures en UTC (= heure d'Abidjan).
- Tu peux donner un avis (« je privilégierais… », « j'éviterais… ») avec le scénario, le niveau \
d'invalidation et ce qui te ferait changer d'avis, mais jamais de garantie de gain : rappelle le \
risque quand tu parles d'entrer en position, sans en faire des paragraphes.
- Pour expliquer un signal, reprends les critères du score et les contrôles qualité.
- Mets en forme avec des listes courtes et du gras pour les niveaux importants."""

REVIEW_SYSTEM = """Tu es le contrôleur de risque d'un robot de trading de l'or (XAUUSD) en M5. Un signal \
vient de passer tous les filtres automatiques. Tu décides s'il faut PLACER ou IGNORER le trade, à partir \
du dossier fourni uniquement.

Méthode : vérifie la cohérence entre la figure, la tendance M15/H1/H4, la structure (zones S/R, range \
asiatique, plus haut / plus bas de la veille), la place jusqu'à l'objectif, le contexte macro (dollar, taux), \
les annonces proches, la volatilité, l'historique de la figure et les derniers résultats du robot. Ignore \
un trade quand un élément important le contredit (zone opposée proche, macro contraire et tendance H4 \
contraire, annonce imminente, série de pertes sur cette figure…). Sois sélectif : mieux vaut rater un \
trade moyen que prendre un mauvais trade.

Réponds UNIQUEMENT par un objet JSON, sans texte autour :
{"decision": "PLACER" ou "IGNORER", "confiance": entier de 0 à 100, "raison": "une ou deux phrases en français"}"""


# --------------------------------------------------------------------------- clés API
def _keys(data_dir) -> dict:
    p = Path(data_dir) / KEY_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    old = Path(data_dir) / OLD_KEY_FILE                 # v2.1.0
    if "claude" not in d and old.exists():
        try:
            d["claude"] = old.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return d


def get_key(data_dir, provider: str = "claude") -> str | None:
    meta = PROVIDERS.get(provider) or {}
    for env in meta.get("env", ()):
        v = os.environ.get(env, "").strip()
        if v:
            return v
    k = str(_keys(data_dir).get(provider) or "").strip()
    return k or None


def set_key(data_dir, key: str | None, provider: str = "claude") -> None:
    if provider not in PROVIDERS or not PROVIDERS[provider]["key"]:
        raise ValueError("Ce fournisseur n'utilise pas de clé API")
    key = (key or "").strip()
    d = _keys(data_dir)
    if key:
        if provider == "claude" and not key.startswith("sk-ant-"):
            raise ValueError("Clé Claude invalide : elle commence par « sk-ant- » (console.anthropic.com → API Keys)")
        if provider == "groq" and not key.startswith("gsk_"):
            raise ValueError("Clé Groq invalide : elle commence par « gsk_ » (console.groq.com → API Keys)")
        if len(key) < 16 or any(ch.isspace() for ch in key):
            raise ValueError("Clé API invalide")
        d[provider] = key
    else:
        d.pop(provider, None)
        if provider == "claude":
            (Path(data_dir) / OLD_KEY_FILE).unlink(missing_ok=True)
    p = Path(data_dir) / KEY_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def status(data_dir, provider: str, model: str) -> dict:
    provider = provider if provider in PROVIDERS else "integre"
    meta = PROVIDERS[provider]
    k = get_key(data_dir, provider) if meta["key"] else None
    envs = meta.get("env", ())
    return {"provider": provider, "needs_key": meta["key"], "signup": meta["signup"],
            "configured": (not meta["key"]) or bool(k),
            "from_env": any(os.environ.get(e) for e in envs),
            "key_hint": f"…{k[-4:]}" if k else None, "model": model_for(provider, model),
            "models": MODELS if provider == "claude" else [],
            "providers": [{"id": pid, "label": p["label"], "needs_key": p["key"], "signup": p["signup"],
                           "configured": (not p["key"]) or bool(get_key(data_dir, pid))}
                          for pid, p in PROVIDERS.items()]}


# --------------------------------------------------------------------------- appel API
class AssistantError(RuntimeError):
    pass


def _request(key: str, body: dict, timeout: float, url: str = API_URL, headers: dict | None = None,
             method: str = "POST", label: str = "Claude"):
    h = headers if headers is not None else {"x-api-key": key, "anthropic-version": API_VERSION}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={**h, "content-type": "application/json"})
    try:
        ctx = ssl.create_default_context() if url.startswith("https") else None
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)
    except urllib.error.HTTPError as e:
        try:
            raw = json.loads(e.read().decode("utf-8"))
            err = raw[0] if isinstance(raw, list) and raw else raw
            msg = (err.get("error") or {}).get("message", "") if isinstance(err, dict) else ""
        except Exception:  # noqa: BLE001
            msg = ""
        hint = {400: "requête refusée (modèle indisponible ?)",
                401: "clé API refusée — vérifie-la dans l'onglet Assistant IA",
                403: "accès refusé pour cette clé",
                404: "modèle introuvable — choisis-en un autre dans l'onglet Assistant IA",
                429: "limite gratuite ou crédit atteint — réessaie plus tard",
                529: "service surchargé — réessaie dans un instant"}.get(e.code, "erreur de l'API")
        raise AssistantError(f"{label} : {hint} ({e.code}{' — ' + msg[:200] if msg else ''})") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        extra = " — Ollama est-il lancé ?" if "localhost" in url or "127.0.0.1" in url else " (connexion Internet ?)"
        raise AssistantError(f"{label} injoignable{extra} : {e}") from e


# ---------------------------------------------------------------- API compatible OpenAI (Groq, Gemini, Ollama)
def _oa(provider: str, key: str | None, base: str | None = None) -> tuple[str, dict, str]:
    meta = PROVIDERS[provider]
    h = {"Authorization": f"Bearer {key}"} if key else {}
    return (base or meta["base"]).rstrip("/"), h, meta["label"].split(" —")[0]


def oa_complete(provider: str, key: str | None, model: str, system: str, messages: list[dict],
                max_tokens: int = 1024, timeout: float = 60, base: str | None = None) -> str:
    url, h, label = _oa(provider, key, base)
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0.3,
            "messages": [{"role": "system", "content": system}] + messages}
    with _request(key or "", body, timeout, f"{url}/chat/completions", h, label=label) as r:
        data = json.loads(r.read().decode("utf-8"))
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise AssistantError(f"{label} : réponse inattendue") from e


def oa_stream(provider: str, key: str | None, model: str, system: str, messages: list[dict],
              max_tokens: int = 1500, timeout: float = 120, base: str | None = None) -> Iterator[str]:
    url, h, label = _oa(provider, key, base)
    body = {"model": model, "max_tokens": max_tokens, "temperature": 0.3, "stream": True,
            "messages": [{"role": "system", "content": system}] + messages}
    with _request(key or "", body, timeout, f"{url}/chat/completions", h, label=label) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if ev.get("error"):
                raise AssistantError(f"{label} : {ev['error'].get('message', 'erreur')}")
            for ch in ev.get("choices") or []:
                txt = (ch.get("delta") or {}).get("content")
                if txt:
                    yield txt


def list_models(provider: str, key: str | None, base: str | None = None) -> list[str]:
    """Modèles proposés par le fournisseur (liste à jour, rien d'écrit en dur)."""
    meta = PROVIDERS.get(provider) or {}
    if meta.get("kind") == "anthropic":
        return [m["id"] for m in MODELS]
    if meta.get("kind") != "openai":
        return []
    url, h, label = _oa(provider, key, base)
    with _request(key or "", None, 20, f"{url}/models", h, method="GET", label=label) as r:
        data = json.loads(r.read().decode("utf-8"))
    ids = [str(m.get("id", "")).removeprefix("models/") for m in data.get("data", []) if isinstance(m, dict)]
    skip = ("whisper", "tts", "embed", "guard", "image", "vision-preview", "aqa", "imagen", "veo", "audio")
    return sorted({i for i in ids if i and not any(x in i.lower() for x in skip)})


def claude_complete(key: str, model: str, system: str, messages: list[dict], max_tokens: int = 1024,
                    timeout: float = 60) -> str:
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
    with _request(key, body, timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def claude_stream(key: str, model: str, system: str, messages: list[dict], max_tokens: int = 1500,
                  timeout: float = 90) -> Iterator[str]:
    """Texte de la réponse au fil de l'eau (événements SSE de l'API Messages)."""
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages, "stream": True}
    with _request(key, body, timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                yield ev["delta"]["text"]
            elif ev.get("type") == "error":
                raise AssistantError(f"Claude : {ev.get('error', {}).get('message', 'erreur')}")


def complete(provider: str, key: str | None, model: str, system: str, messages: list[dict],
             max_tokens: int = 1024, timeout: float = 60, base: str | None = None) -> str:
    if PROVIDERS[provider]["kind"] == "anthropic":
        return claude_complete(key or "", model, system, messages, max_tokens, timeout)
    return oa_complete(provider, key, model, system, messages, max_tokens, timeout, base)


def stream(provider: str, key: str | None, model: str, system: str, messages: list[dict],
           max_tokens: int = 1500, base: str | None = None) -> Iterator[str]:
    if PROVIDERS[provider]["kind"] == "anthropic":
        return claude_stream(key or "", model, system, messages, max_tokens)
    return oa_stream(provider, key, model, system, messages, max_tokens, base=base)


def clean_history(messages: list, limit: int = 12) -> list[dict]:
    """Garde les derniers échanges valides (rôles alternés, premier message de l'utilisateur)."""
    out: list[dict] = []
    for m in (messages or [])[-limit:]:
        role, text = m.get("role"), str(m.get("content", "")).strip()
        if role not in ("user", "assistant") or not text:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + text[:4000]
        else:
            out.append({"role": role, "content": text[:4000]})
    while out and out[0]["role"] != "user":
        out.pop(0)
    if not out or out[-1]["role"] != "user":
        raise ValueError("Le dernier message doit venir de l'utilisateur")
    return out


def with_context(messages: list[dict], context: str) -> list[dict]:
    """Ajoute le dossier de marché à jour au dernier message de l'utilisateur."""
    msgs = [dict(m) for m in messages]
    msgs[-1]["content"] = f"<dossier_marche>\n{context}\n</dossier_marche>\n\n{msgs[-1]['content']}"
    return msgs


# --------------------------------------------------------------------------- dossier de marché
def _arrow(v) -> str:
    return "haussière" if v == 1 else "baissière" if v == -1 else "neutre"


def build_context(snap: dict | None, extra: dict | None = None) -> str:
    """Dossier texte compact à partir de l'instantané de l'interface (+ robot, statistiques)."""
    extra = extra or {}
    if not snap or not snap.get("ready"):
        return "Les données de marché ne sont pas encore chargées."
    c, L = snap.get("context", {}), []
    now = datetime.now(timezone.utc)
    L.append(f"Heure actuelle : {now:%Y-%m-%d %H:%M} UTC. Symbole {snap.get('symbol')} "
             f"(source : {snap.get('source_label') or snap.get('source')}). Dernière bougie M5 close : "
             f"{str(snap.get('last_close', ''))[:16].replace('T', ' ')} UTC.")
    ch = snap.get("change_24h_pips")
    L.append(f"Prix {snap.get('price'):.2f} ; variation 24 h {'n/d' if ch is None else f'{ch:+.0f} pips'} ; "
             f"plus haut / bas 24 h {c.get('day_high')} / {c.get('day_low')}.")
    L.append(f"Tendance H1 {_arrow(c.get('h1_bias'))}, M5 {_arrow(c.get('m5_trend'))} ; RSI {c.get('rsi')} ; "
             f"ADX {c.get('adx')} ; ATR M5 {c.get('atr_pips')} pips ; "
             f"{'en session Londres/New York' if c.get('in_session') else 'hors session principale'}.")
    e = c.get("ema") or {}
    L.append(f"EMA 20 / 50 / 200 (M5) : {e.get('fast')} / {e.get('mid')} / {e.get('slow')}.")
    lv = snap.get("levels") or {}
    if lv:
        L.append(f"Range asiatique ({lv.get('asia_hours')} h UTC{'' if lv.get('asia_done') else ', en cours'}) : "
                 f"{lv.get('asia_low')} – {lv.get('asia_high')} ; veille : plus haut {lv.get('prev_day_high')}, "
                 f"plus bas {lv.get('prev_day_low')}.")
    zones = snap.get("zones") or []
    if zones:
        L.append("Zones S/R proches : " + " ; ".join(
            f"{z['kind']} {z['price']:.2f} ({z['touches']} touche(s), {z['distance_pips']:+.0f} pips)" for z in zones[:6]))
    m = snap.get("macro")
    if m:
        L.append("Macro : " + " ; ".join(
            f"{it['label']} {it['value']} tendance H1 {_arrow(it['trend'])}"
            + (f", {it['chg24']:+.2f} % sur 24 h" if it.get("chg24") is not None else "") for it in m["items"])
            + f". {m['text']}")
    else:
        L.append("Macro (dollar, taux) : indisponible.")
    r = snap.get("radar")
    if r:
        L.append(_sig_text("Radar (dernière bougie)", r))
    rr = snap.get("radar_recent")
    if rr and (not r or rr.get("key") != r.get("key")):
        L.append(_sig_text("Setup récent", rr))
    st = snap.get("journal_stats") or {}
    if st:
        L.append("Journal des signaux : " + ", ".join(f"{k} {v}" for k, v in st.items() if not isinstance(v, (dict, list))))
    rec = snap.get("recent") or []
    if rec:
        L.append("Derniers signaux : " + " ; ".join(
            f"{str(x.get('time', ''))[5:16].replace('T', ' ')} {x.get('side')} {x.get('pattern')} "
            f"{x.get('score')}/10 → {x.get('status') or 'en cours'}"
            + (f" ({x['result_r']:+.2f} R)" if x.get("result_r") is not None else "") for x in rec[:6]))
    news = extra.get("news") or []
    if news:
        L.append("Annonces USD à venir : " + " ; ".join(
            f"{str(n.get('time', ''))[5:16].replace('T', ' ')} {n.get('title')}" for n in news[:6]))
    edge = extra.get("edge") or {}
    rows = edge.get("patterns") or edge.get("rows") or []
    if rows:
        L.append("Historique par figure (backtest de validation) : " + " ; ".join(
            f"{p.get('pattern') or p.get('name')} {p.get('trades')} trades, espérance "
            f"{float(p.get('expectancy_r', p.get('expectancy', 0)) or 0):+.2f} R" for p in rows[:16] if isinstance(p, dict)))
    bot = extra.get("robot") or {}
    if bot:
        L.append(f"Robot : {'actif' if bot.get('enabled') else 'désactivé'}, mode {bot.get('mode')}"
                 + (f", {bot.get('open')} position(s) ouverte(s)" if bot.get("open") else "")
                 + (f", pause : {bot['paused']}" if bot.get("paused") else "") + ".")
        if bot.get("recent"):
            L.append("Derniers trades du robot : " + " ; ".join(bot["recent"][:6]))
    return "\n".join(L)


def _sig_text(title: str, s: dict) -> str:
    fac = ", ".join(f"{f['label']} ({f['points']:+g})" for f in s.get("factors", []))
    mtf = s.get("mtf") or {}
    t = (f"{title} : {s.get('side')} {s.get('pattern')} — score {s.get('score')}/10 ({s.get('quality')}) ; "
         f"entrée {s.get('entry'):.2f}, stop {s.get('sl'):.2f} ({s.get('risk_pips')} pips), "
         f"TP1 {s.get('tp1'):.2f} (1:{s.get('rr1')}), TP2 {s.get('tp2'):.2f} (1:{s.get('rr2')}) ; "
         f"M15/H1/H4 {mtf.get('m15')}/{mtf.get('h1')}/{mtf.get('h4')} ; critères : {fac}")
    if s.get("retest"):
        t += f" ; niveau cassé à retester {s['retest']:.2f}"
    if s.get("ml") and isinstance(s["ml"], dict) and s["ml"].get("proba") is not None:
        t += f" ; probabilité IA {s['ml']['proba']:.0%}"
    t += f" ; {'REJETÉ : ' + s['blocked'] if s.get('blocked') else 'VALIDE'}."
    return t


# --------------------------------------------------------------------------- validation d'un trade
def parse_review(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise AssistantError("réponse de Claude illisible")
    d = json.loads(m.group(0))
    dec = str(d.get("decision", "")).upper()
    if dec not in ("PLACER", "IGNORER"):
        raise AssistantError(f"décision inconnue : {dec}")
    conf = int(max(0, min(100, float(d.get("confiance", 0)))))
    return {"decision": dec, "confidence": conf, "reason": str(d.get("raison", "")).strip()[:400]}


def review_signal(sig: dict, checks: list[dict], context: str, key: str | None, model: str,
                  timeout: float = 45, provider: str = "claude", base: str | None = None) -> dict:
    ctl = "\n".join(f"- {c['label']} : {c['status']} — {c['detail']}" for c in checks)
    prompt = (f"<dossier_marche>\n{context}\n</dossier_marche>\n\n"
              f"Signal à valider : {_sig_text('Signal', sig)}\n\nContrôles automatiques :\n{ctl}\n\n"
              "Décision ? Réponds uniquement par le JSON demandé.")
    text = complete(provider, key, model, REVIEW_SYSTEM, [{"role": "user", "content": prompt}],
                    max_tokens=400, timeout=timeout, base=base)
    return parse_review(text)
