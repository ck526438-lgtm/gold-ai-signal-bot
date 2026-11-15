"""Assistant IA (Claude) : chat sur le marché de l'or et validation des trades avant envoi.

Claude ne voit QUE les données préparées par l'application (prix, tendances, zones, macro,
annonces, statistiques de ton historique, journal, robot). Il n'a pas accès à Internet et ne
« prédit » pas le marché : il lit le dossier comme le ferait un analyste et donne son avis.

Clé API : variable d'environnement ANTHROPIC_API_KEY, sinon fichier `anthropic_key.txt` dans le
dossier de données (enregistré depuis l'onglet Assistant IA, jamais renvoyé à l'interface).
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
MODELS = [
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5 — équilibré (recommandé)"},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5 — le plus fin, plus lent et plus cher"},
    {"id": "claude-haiku-5-5", "label": "Claude Haiku 5.5 — rapide et économique"},
]
KEY_FILE = "anthropic_key.txt"

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


# --------------------------------------------------------------------------- clé API
def key_path(data_dir) -> Path:
    return Path(data_dir) / KEY_FILE


def get_key(data_dir) -> str | None:
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env:
        return env
    p = key_path(data_dir)
    try:
        k = p.read_text(encoding="utf-8").strip()
        return k or None
    except OSError:
        return None


def set_key(data_dir, key: str | None) -> None:
    p = key_path(data_dir)
    key = (key or "").strip()
    if not key:
        p.unlink(missing_ok=True)
        return
    if not key.startswith("sk-ant-"):
        raise ValueError("Clé API invalide : elle commence par « sk-ant- » (console.anthropic.com → API Keys)")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(key, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def status(data_dir, model: str) -> dict:
    k = get_key(data_dir)
    return {"configured": bool(k), "from_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "key_hint": f"sk-ant-…{k[-4:]}" if k else None, "model": model, "models": MODELS}


# --------------------------------------------------------------------------- appel API
class AssistantError(RuntimeError):
    pass


def _request(key: str, body: dict, timeout: float):
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            msg = ""
        hint = {401: "clé API refusée — vérifie-la dans l'onglet Assistant IA",
                403: "accès refusé pour cette clé",
                404: "modèle introuvable — choisis-en un autre dans l'onglet Assistant IA",
                429: "trop de requêtes ou crédit épuisé — réessaie plus tard",
                529: "service Claude surchargé — réessaie dans un instant"}.get(e.code, "erreur de l'API")
        raise AssistantError(f"Claude : {hint} ({e.code}{' — ' + msg if msg else ''})") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AssistantError(f"Claude injoignable (connexion Internet ?) : {e}") from e


def complete(key: str, model: str, system: str, messages: list[dict], max_tokens: int = 1024,
             timeout: float = 60) -> str:
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
    with _request(key, body, timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def stream(key: str, model: str, system: str, messages: list[dict], max_tokens: int = 1500,
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


def review_signal(sig: dict, checks: list[dict], context: str, key: str, model: str,
                  timeout: float = 30) -> dict:
    ctl = "\n".join(f"- {c['label']} : {c['status']} — {c['detail']}" for c in checks)
    prompt = (f"<dossier_marche>\n{context}\n</dossier_marche>\n\n"
              f"Signal à valider : {_sig_text('Signal', sig)}\n\nContrôles automatiques :\n{ctl}\n\n"
              "Décision ?")
    text = complete(key, model, REVIEW_SYSTEM, [{"role": "user", "content": prompt}], max_tokens=300,
                    timeout=timeout)
    return parse_review(text)
