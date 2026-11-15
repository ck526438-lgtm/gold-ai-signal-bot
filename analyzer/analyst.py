"""Analyste intégré : gratuit, hors ligne, sans clé API.

Rédige l'analyse du marché, explique les signaux, résume la session, commente le journal et
valide les trades (PLACER / IGNORER) avec des règles de trader, à partir des mêmes données que
l'interface. Aucune « intelligence » cachée : chaque phrase vient d'un calcul visible.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def _f(x, d=2) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{d}f}"


def _trend(v) -> str:
    return "haussière" if v == 1 else "baissière" if v == -1 else "neutre"


def _pips(a, b, pip=0.1) -> float:
    return round((a - b) / pip, 0)


# --------------------------------------------------------------------------- biais du marché
def bias(snap: dict) -> tuple[int, int, list[str]]:
    """(sens −1/0/+1, force 0–3, raisons) à partir des tendances, EMA 200 et du macro."""
    c, why, pts = snap.get("context", {}), [], 0.0
    h1, m5 = c.get("h1_bias", 0), c.get("m5_trend", 0)
    pts += 2 * h1 + m5
    if h1:
        why.append(f"tendance H1 {_trend(h1)}")
    if m5:
        why.append(f"M5 {_trend(m5)}")
    price, e200 = snap.get("price"), (c.get("ema") or {}).get("slow")
    if price and e200:
        side = 1 if price > e200 else -1
        pts += side
        why.append(f"prix {'au-dessus' if side == 1 else 'en dessous'} de l'EMA 200 ({_f(e200)})")
    m = snap.get("macro")
    if m and m.get("gold_bias"):
        pts += 1.5 * m["gold_bias"]
        why.append("dollar / taux " + ("favorables" if m["gold_bias"] == 1 else "défavorables") + " à l'or")
    d = 1 if pts >= 2 else -1 if pts <= -2 else 0
    strength = 0 if d == 0 else 1 if abs(pts) < 3.5 else 2 if abs(pts) < 5 else 3
    return d, strength, why


def _zones(snap: dict) -> tuple[list[dict], list[dict]]:
    price = snap.get("price", 0)
    zs = snap.get("zones") or []
    above = sorted([z for z in zs if z["price"] > price], key=lambda z: z["price"])
    below = sorted([z for z in zs if z["price"] <= price], key=lambda z: -z["price"])
    return above, below


# --------------------------------------------------------------------------- textes
def market_analysis(snap: dict, extra: dict | None = None) -> str:
    extra = extra or {}
    if not snap or not snap.get("ready"):
        return "Les données de marché ne sont pas encore chargées : réessaie dans quelques secondes."
    c, lv = snap.get("context", {}), snap.get("levels") or {}
    price, atr = snap["price"], c.get("atr_pips", 0)
    d, strength, why = bias(snap)
    above, below = _zones(snap)
    L = ["### Vue d'ensemble"]
    ch = snap.get("change_24h_pips")
    L.append(f"- Or à **{_f(price)}**" + (f", {ch:+.0f} pips sur 24 h" if ch is not None else "")
             + f" (plus haut {_f(c.get('day_high'))}, plus bas {_f(c.get('day_low'))}).")
    L.append(f"- Tendance H1 **{_trend(c.get('h1_bias'))}**, M5 {_trend(c.get('m5_trend'))} ; RSI {c.get('rsi', 0):.0f}"
             f" ; ADX {c.get('adx', 0):.0f} ({'marché directionnel' if c.get('adx', 0) >= 18 else 'marché sans direction'}).")
    vol = "calme" if atr < 20 else "normale" if atr < 60 else "agitée" if atr < 100 else "extrême"
    L.append(f"- Volatilité {vol} : ATR M5 {atr:.0f} pips ({atr / 10:.1f} $ par bougie).")
    m = snap.get("macro")
    L.append(f"- Macro : {m['text']}" if m else "- Macro : dollar et taux indisponibles.")
    word = {0: "neutre", 1: "légèrement", 2: "nettement", 3: "fortement"}
    if d:
        L.append(f"- **Biais {word[strength]} {'haussier' if d == 1 else 'baissier'}** : {', '.join(why)}."
                 + (" Mais l'ADX est faible : tendance molle, objectifs modestes." if c.get("adx", 0) < 18 else ""))
    else:
        L.append(f"- **Pas de biais clair** ({', '.join(why) or 'signaux contradictoires'}).")

    L.append("\n### Niveaux clés")
    for z in above[:3][::-1]:
        L.append(f"- Résistance **{_f(z['price'])}** ({z['touches']} touche(s), {z['distance_pips']:+.0f} pips)")
    L.append(f"- *Prix actuel {_f(price)}*")
    for z in below[:3]:
        L.append(f"- Support **{_f(z['price'])}** ({z['touches']} touche(s), {z['distance_pips']:+.0f} pips)")
    if lv.get("asia_high") is not None:
        L.append(f"- Range asiatique {_f(lv['asia_low'])} – {_f(lv['asia_high'])}"
                 + ("" if lv.get("asia_done") else " (en cours de formation)"))
    if lv.get("prev_day_high") is not None:
        L.append(f"- Veille : plus haut {_f(lv['prev_day_high'])}, plus bas {_f(lv['prev_day_low'])}")

    L.append("\n### Scénario")
    L += scenario(snap, d)
    warn = _warnings(snap, extra)
    if warn:
        L.append("\n### Prudence")
        L += [f"- {w}" for w in warn]
    r = snap.get("radar")
    if r:
        L.append(f"\n**Radar** : {r['side']} {r['pattern']} {r['score']}/10 — "
                 + (f"rejeté ({r['blocked']})." if r.get("blocked") else "signal valide."))
    L.append("\n*Analyse automatique à partir des données du bot : elle décrit le marché, elle ne garantit rien.*")
    return "\n".join(L)


def _spaced(levels: list[float], start: float, d: int, gap: float, n: int = 2) -> list[float]:
    """Niveaux dans le sens d, chacun à au moins `gap` du précédent (évite les niveaux collés)."""
    out, ref = [], start
    for x in sorted(levels, key=lambda v: v * d):
        if (x - ref) * d >= gap:
            out.append(x)
            ref = x
            if len(out) == n:
                break
    return out


def scenario(snap: dict, d: int) -> list[str]:
    c, price = snap.get("context", {}), snap.get("price", 0)
    atr = max(c.get("atr_pips", 30), 5) * 0.1                     # en $
    lv = snap.get("levels") or {}
    zs = [z["price"] for z in snap.get("zones") or []]
    extra = [lv.get(k) for k in ("asia_high", "asia_low", "prev_day_high", "prev_day_low") if lv.get(k) is not None]
    levels = zs + extra + [c.get("day_high"), c.get("day_low")]
    levels = [x for x in levels if x is not None]
    ema_mid = (c.get("ema") or {}).get("mid")
    out = []
    if d in (1, -1):
        word, opp = ("Achats privilégiés", "acheter") if d == 1 else ("Ventes privilégiées", "vendre")
        # zone d'entrée : niveau contre le sens (repli) à au moins 0,3 ATR du prix
        entry_c = _spaced(levels + ([ema_mid] if ema_mid else []), price, -d, 0.3 * atr, 1)
        entry = entry_c[0] if entry_c else price - d * 0.5 * atr
        inval_c = _spaced(levels, entry, -d, 0.5 * atr, 1)
        inval = inval_c[0] - d * 0.2 * atr if inval_c else entry - d * 1.2 * atr
        tgt = _spaced(levels, entry, d, 1.0 * atr, 2) or [entry + d * 1.5 * abs(entry - inval)]
        rr = abs(tgt[0] - entry) / max(abs(entry - inval), 1e-9)
        out.append(f"- **{word}** sur {'repli' if d == 1 else 'rebond'} vers **{_f(entry)}** "
                   f"(niveau clé / EMA 50 {_f(ema_mid)}), avec une figure de retournement "
                   f"{'haussière' if d == 1 else 'baissière'} sur M5.")
        out.append(f"- Objectifs : {', '.join('**' + _f(t) + '**' for t in tgt)} "
                   f"(premier objectif ≈ {rr:.1f} fois le risque).")
        out.append(f"- **Invalidation** : clôture M5 {'sous' if d == 1 else 'au-dessus de'} **{_f(inval)}** "
                   f"({abs(entry - inval) / 0.1:.0f} pips sous l'entrée{'' if d == 1 else ' inversée'}).".replace(
                       "sous l'entrée inversée", "au-dessus de l'entrée"))
        if rr < 1.2:
            out.append("- Le premier objectif est proche : rapport gain / risque faible, mieux vaut attendre un "
                       "meilleur prix d'entrée.")
        out.append(f"- Ne pas {opp.replace('acheter', 'vendre') if d == 1 else 'acheter'} contre la tendance tant "
                   "que l'invalidation n'est pas touchée.")
    else:
        hi_c = _spaced(levels, price, 1, 0.5 * atr, 1)
        lo_c = _spaced(levels, price, -1, 0.5 * atr, 1)
        hi = hi_c[0] if hi_c else c.get("day_high")
        lo = lo_c[0] if lo_c else c.get("day_low")
        out.append(f"- **Marché sans direction** : le prix évolue entre **{_f(lo)}** et **{_f(hi)}**.")
        out.append(f"- Achat seulement après une cassure au-dessus de {_f(hi)} suivie d'un retest ; "
                   f"vente seulement sous {_f(lo)} avec retest.")
        out.append("- Au milieu du range, le rapport gain / risque est mauvais : patience.")
    return out


def _warnings(snap: dict, extra: dict) -> list[str]:
    c, out = snap.get("context", {}), []
    if not c.get("in_session"):
        out.append("Hors des sessions Londres / New York : mouvements moins fiables, spread souvent plus large.")
    atr = c.get("atr_pips", 0)
    if atr > 100:
        out.append(f"Volatilité très élevée ({atr:.0f} pips) : réduis la taille ou attends.")
    elif atr < 12:
        out.append(f"Volatilité très faible ({atr:.0f} pips) : le spread pèse lourd sur les petits mouvements.")
    rsi = c.get("rsi", 50)
    if rsi >= 72 or rsi <= 28:
        out.append(f"RSI en excès ({rsi:.0f}) : risque de respiration, évite d'entrer en poursuite.")
    soon = upcoming_news(extra, hours=3)
    if soon:
        out.append("Annonces USD dans les 3 h : " + "; ".join(f"{n['when']} {n['title']}" for n in soon)
                   + ". Pas de nouvelle position 30 min avant / après.")
    return out


def upcoming_news(extra: dict, hours: float = 24) -> list[dict]:
    now = datetime.now(UTC)
    out = []
    for n in (extra or {}).get("news") or []:
        try:
            t = datetime.fromisoformat(str(n.get("time")))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        if now - timedelta(minutes=30) <= t <= now + timedelta(hours=hours):
            out.append({"when": f"{t:%H:%M} UTC", "title": n.get("title", ""), "time": t})
    return out


def explain_signal(snap: dict, extra: dict | None = None) -> str:
    r = (snap or {}).get("radar") or (snap or {}).get("radar_recent")
    if not r:
        d, _, _ = bias(snap or {})
        txt = ["Aucune figure de chandelier sur les dernières bougies : le radar attend un setup.",
               "\n### Setup attendu"]
        txt += scenario(snap, d) if snap and snap.get("ready") else []
        return "\n".join(txt)
    pos = [f for f in r.get("factors", []) if f["points"] > 0]
    neg = [f for f in r.get("factors", []) if f["points"] < 0]
    neu = [f for f in r.get("factors", []) if f["points"] == 0]
    L = [f"### {r['side']} — {r['pattern']} · {r['score']}/10 (qualité {r['quality']})",
         f"Entrée **{_f(r['entry'])}**, stop **{_f(r['sl'])}** ({r['risk_pips']:.0f} pips), "
         f"TP1 {_f(r['tp1'])} (1:{r['rr1']}), TP2 {_f(r['tp2'])} (1:{r['rr2']})."]
    if pos:
        L.append("\n**Ce qui est solide**")
        L += [f"- {f['label']} (+{f['points']:g})" for f in pos]
    if neg or neu:
        L.append("\n**Ce qui est fragile**")
        L += [f"- {f['label']} ({f['points']:+g})" for f in neg] + [f"- {f['label']}" for f in neu]
    if r.get("retest"):
        L.append(f"\nNiveau cassé / balayé : **{_f(r['retest'])}** — le robot attend son retest pour entrer.")
    if r.get("blocked"):
        L.append(f"\n**Signal rejeté** : {r['blocked']}.")
    rv = rule_review(r, [], snap, extra)
    L.append(f"\n**Mon avis : {rv['decision']}** (confiance {rv['confidence']} %) — {rv['reason']}")
    return "\n".join(L)


def session_summary(snap: dict, extra: dict | None = None) -> str:
    if not snap or not snap.get("ready"):
        return "Les données ne sont pas encore chargées."
    candles = (snap.get("chart") or {}).get("candles") or []
    lv, c = snap.get("levels") or {}, snap.get("context", {})
    if not candles:
        return market_analysis(snap, extra)
    last = datetime.fromtimestamp(candles[-1]["time"], UTC)
    day0 = last.replace(hour=0, minute=0, second=0, microsecond=0)
    today = [k for k in candles if k["time"] >= day0.timestamp()]
    h = last.hour
    sess = "asiatique" if h < 7 else "de Londres" if h < 12 else "de New York" if h < 21 else "de fin de journée"
    L = [f"### Session {sess} — {last:%d/%m %H:%M} UTC"]
    if today:
        o, hi, lo = today[0]["open"], max(k["high"] for k in today), min(k["low"] for k in today)
        cl = today[-1]["close"]
        L.append(f"- Depuis minuit : ouverture {_f(o)}, plus haut {_f(hi)}, plus bas {_f(lo)}, "
                 f"maintenant {_f(cl)} ({_pips(cl, o):+.0f} pips).")
    ah, al = lv.get("asia_high"), lv.get("asia_low")
    if ah is not None:
        width = _pips(ah, al)
        L.append(f"- Range asiatique {_f(al)} – {_f(ah)} ({width:.0f} pips"
                 + (f", {width / max(c.get('atr_pips', 1), 1):.1f} ATR" if c.get("atr_pips") else "") + ").")
        price = snap["price"]
        if lv.get("asia_done"):
            if price > ah:
                L.append(f"- Le prix est **au-dessus du range asiatique** : les acheteurs ont pris la main. "
                         f"Un retour sous {_f(ah)} affaiblirait la cassure.")
            elif price < al:
                L.append(f"- Le prix est **sous le range asiatique** : les vendeurs ont pris la main. "
                         f"Un retour au-dessus de {_f(al)} affaiblirait la cassure.")
            else:
                L.append("- Le prix est **encore dans le range asiatique** : pas de cassure pour l'instant. "
                         "Une cassure franche entre 7 h et 11 h UTC donnerait le sens de la journée.")
        else:
            L.append("- Le range asiatique se forme encore : la cassure se joue à l'ouverture de Londres (7 h UTC).")
    ph, pl = lv.get("prev_day_high"), lv.get("prev_day_low")
    if ph is not None and today:
        hi, lo = max(k["high"] for k in today), min(k["low"] for k in today)
        if hi > ph:
            L.append(f"- Le plus haut de la veille ({_f(ph)}) a été dépassé"
                     + (" puis repris : possible piège (balayage de liquidité)." if snap["price"] < ph else "."))
        if lo < pl:
            L.append(f"- Le plus bas de la veille ({_f(pl)}) a été cassé"
                     + (" puis repris : possible piège (balayage de liquidité)." if snap["price"] > pl else "."))
        if lo >= pl and hi <= ph:
            L.append(f"- Toujours entre le plus bas ({_f(pl)}) et le plus haut ({_f(ph)}) de la veille.")
    d, strength, why = bias(snap)
    L.append(f"- Biais actuel : {'haussier' if d == 1 else 'baissier' if d == -1 else 'neutre'}"
             + (f" ({', '.join(why)})." if why else "."))
    L.append("\n### Pour la suite")
    L += scenario(snap, d)
    L += [f"- {w}" for w in _warnings(snap, extra or {})]
    return "\n".join(L)


def journal_review(snap: dict, extra: dict | None = None) -> str:
    extra = extra or {}
    st = (snap or {}).get("journal_stats") or {}
    L = ["### Journal des signaux"]
    if st.get("closed"):
        L.append(f"- {st['closed']} signaux terminés : réussite {st.get('win_rate')} %, espérance "
                 f"{st.get('expectancy_r', 0):+.2f} R par trade, total {st.get('total_r', 0):+.1f} R, "
                 f"profit factor {st.get('profit_factor') or '—'}.")
        e = st.get("expectancy_r", 0)
        L.append("- " + ("Espérance positive : la méthode gagne sur ta période." if e > 0.05 else
                         "Espérance proche de zéro : le spread mange le gain, sois plus sélectif (score minimum plus haut)."
                         if e > -0.05 else "Espérance négative : ne trade pas en réel avec ces réglages."))
    else:
        L.append("- Pas encore assez de signaux terminés.")
    rows = [p for p in ((extra.get("edge") or {}).get("patterns") or []) if isinstance(p, dict)]
    if rows:
        good = [p for p in rows if p.get("trades", 0) >= 20 and p.get("expectancy_r", 0) >= 0.05]
        bad = [p for p in rows if p.get("trades", 0) >= 20 and p.get("expectancy_r", 0) < 0]
        if good:
            L.append("\n**Figures qui marchent sur ton historique**")
            L += [f"- {p['pattern']} : {p['trades']} trades, {p['expectancy_r']:+.2f} R" for p in good[:5]]
        if bad:
            L.append("\n**Figures qui perdent** (à désactiver dans Réglages → Figures)")
            L += [f"- {p['pattern']} : {p['trades']} trades, {p['expectancy_r']:+.2f} R" for p in bad[:6]]
    else:
        L.append("- Statistiques par figure pas encore calculées (onglet Robot → Recalculer).")
    bot = extra.get("robot") or {}
    if bot.get("recent"):
        L.append("\n**Derniers trades du robot**")
        L += [f"- {t}" for t in bot["recent"][:6]]
    L.append("\n### Conseils")
    L.append("- Garde le score minimum du robot à 7 ou plus tant que l'espérance n'est pas nettement positive.")
    L.append("- Lance un backtest de 50 000 bougies MT5 après chaque changement de réglage.")
    return "\n".join(L)


def news_answer(snap: dict, extra: dict | None = None) -> str:
    soon = upcoming_news(extra or {}, hours=24)
    if not (extra or {}).get("news") and not soon:
        return ("Je n'ai pas de calendrier chargé pour l'instant (onglet Robot → Annonces). Par prudence, "
                "évite d'ouvrir une position 30 min avant et après le NFP, le CPI et les décisions de la Fed.")
    if not soon:
        return "Aucune annonce majeure USD dans les 24 prochaines heures : le marché suit sa technique."
    L = ["### Annonces USD des prochaines 24 h"]
    L += [f"- **{n['when']}** — {n['title']}" for n in soon]
    L.append("\n- Pas de nouvelle position de 30 min avant à 30 min après chaque annonce (le robot le fait déjà).")
    L.append("- Si une position est ouverte avant l'annonce : stop à l'entrée ou prise de profit partielle.")
    L.append("- Après l'annonce, attends la clôture de 2–3 bougies M5 : la première réaction se retourne souvent.")
    return "\n".join(L)


INTENTS = [                       # ordre important : du plus précis au plus général
    ("journal", ("journal", "mes trades", "mes derniers", "historique", "statist", "réglage", "résultats")),
    ("annonces", ("annonce", "news", "nfp", "cpi", "fed", "calendrier")),
    ("session", ("session", "résumé", "resume", "aujourd'hui", "journée")),
    ("signal", ("signal", "radar", "pourquoi", "setup")),
    ("marche", ("marché", "marche", "analyse", "tendance", "or ")),
]
HANDLERS = {"journal": lambda s, e: journal_review(s, e), "annonces": lambda s, e: news_answer(s, e),
            "session": lambda s, e: session_summary(s, e), "signal": lambda s, e: explain_signal(s, e),
            "marche": lambda s, e: market_analysis(s, e)}


def answer(question: str, snap: dict, extra: dict | None = None, intent: str | None = None) -> str:
    if intent in HANDLERS:
        return HANDLERS[intent](snap, extra)
    q = (question or "").lower()
    for name, words in INTENTS:
        if any(w in q for w in words):
            return HANDLERS[name](snap, extra)
    return (market_analysis(snap, extra) + "\n\n*L'analyste intégré répond avec l'analyse du marché. "
            "Pour des questions libres, choisis Groq, Gemini ou Ollama (gratuits) dans « Cerveau de l'assistant ».*")


# --------------------------------------------------------------------------- validation d'un trade
def rule_review(sig: dict, checks: list[dict], snap: dict | None = None, extra: dict | None = None) -> dict:
    """Avis PLACER / IGNORER par règles. Confiance 0–100 ; le robot compare au seuil réglé."""
    d = int(sig.get("direction", 1))
    pts, pro, con = 50.0, [], []

    def add(v: float, txt: str):
        nonlocal pts
        pts += v
        (pro if v > 0 else con).append((abs(v), txt))

    add((float(sig.get("score", 7)) - 7) * 8, f"score {sig.get('score')}/10")
    mtf = sig.get("mtf") or {}
    if mtf.get("h4") == -d:
        add(-15, "tendance H4 contre le trade")
    elif mtf and all(v == d for v in mtf.values()):
        add(10, "M15, H1 et H4 alignés")
    mac = sig.get("macro", 0)
    if mac == -d:
        add(-15, "dollar / taux contre le trade")
    elif mac == d:
        add(10, "dollar / taux favorables")
    neg = [f for f in sig.get("factors", []) if f.get("points", 0) < 0 and not f["label"].startswith("Macro")]
    for f in neg[:2]:
        add(-6, f["label"].lower())
    if sig.get("retest"):
        add(5, "entrée de précision sur retest")
    risk = abs(sig.get("entry", 0) - sig.get("sl", 0)) or 1e-9
    if snap:
        zs = [z for z in snap.get("zones") or [] if (z["price"] - sig["entry"]) * d > 0]
        if zs:
            room = min(abs(z["price"] - sig["entry"]) for z in zs) / risk
            if room < 1.5:
                add(-25, f"zone opposée à {room:.1f} R seulement")
            elif room >= 2.5:
                add(5, f"place jusqu'à la zone opposée ({room:.1f} R)")
    soon = upcoming_news(extra or {}, hours=1)
    if soon:
        add(-20, f"annonce {soon[0]['title']} à {soon[0]['when']}")
    atr = sig.get("atr_pips") or 0
    if atr > 90:
        add(-10, f"volatilité extrême ({atr:.0f} pips)")
    for p in ((extra or {}).get("edge") or {}).get("patterns") or []:
        if isinstance(p, dict) and p.get("pattern") == sig.get("pattern") and p.get("trades", 0) >= 20:
            e = p.get("expectancy_r", 0)
            if e >= 0.1:
                add(10, f"figure rentable sur ton historique ({e:+.2f} R)")
            elif e < 0:
                add(-20, f"figure perdante sur ton historique ({e:+.2f} R)")
    ml = sig.get("ml")
    if isinstance(ml, dict) and ml.get("proba") is not None:
        add((ml["proba"] - 0.5) * 40, f"probabilité IA {ml['proba']:.0%}")
    for c in checks or []:
        if c.get("status") == "fail":
            add(-30, c.get("label", "contrôle échoué").lower())
    conf = int(max(0, min(100, round(pts))))
    dec = "PLACER" if conf >= 50 else "IGNORER"
    key = sorted(con if dec == "IGNORER" else pro, reverse=True)[:3] or sorted(pro + con, reverse=True)[:2]
    reason = ("Points forts : " if dec == "PLACER" else "Points bloquants : ") + ", ".join(t for _, t in key) + "."
    if dec == "PLACER" and con:
        reason += " Attention : " + ", ".join(t for _, t in sorted(con, reverse=True)[:2]) + "."
    return {"decision": dec, "confidence": conf, "reason": reason}
