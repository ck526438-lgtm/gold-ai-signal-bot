"""Assistant de premier démarrage et diagnostic.

- `detect()` : trouve MetaTrader 5, le compte, le nom de l'or (XAUUSD, GOLD…) chez le courtier
  et le décalage horaire du serveur, pour pré-remplir la configuration.
- `diagnostics()` : liste de vérifications avec, pour chaque problème, comment le corriger.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from .mt5link import LINK, find_terminals
from .paths import VERSION, user_dir

UTC = timezone.utc


GOLD_BASES = ("XAUUSD", "GOLD")


def pick_symbol(names: list[str], base: str = "XAUUSD") -> str | None:
    """Choisit le symbole de l'or du courtier : exact, sinon le plus court qui commence par la base.

    Sans XAUUSD chez le courtier, essaie GOLD (GOLD, GOLDm, GOLD.a…)."""
    if not names:
        return None
    if base.upper() in GOLD_BASES:
        for b in (base.upper(),) + tuple(x for x in GOLD_BASES if x != base.upper()):
            hit = _pick(names, b)
            if hit:
                return hit
        return None
    return _pick(names, base)


def _pick(names: list[str], base: str) -> str | None:
    if base in names:
        return base
    starts = sorted((n for n in names if n.upper().startswith(base)), key=len)
    if starts:
        return starts[0]
    contains = sorted((n for n in names if base in n.upper()), key=len)
    return contains[0] if contains else None


def offset_from_tick(tick_time: float, now: float | None = None) -> int | None:
    """Décalage (h) entre l'heure serveur MT5 et UTC, si la cotation est récente."""
    now = time.time() if now is None else now
    diff = tick_time - now
    hours = round(diff / 3600)
    if abs(diff - hours * 3600) > 180 or abs(hours) > 14:
        return None  # cotation ancienne (marché fermé) : détection impossible
    return int(hours)


def detect(link=LINK, symbol_hint: str = "XAUUSD") -> dict:
    out = {
        "mt5_module": link.available, "terminals": [], "connected": False, "account": None,
        "algo_trading": None, "symbols": [], "symbol": None, "offset": None,
        "offset_reason": "", "error": None, "terminal_path": "",
    }
    if os.name == "nt" or link._module is not None:
        out["terminals"] = find_terminals()
    if not out["mt5_module"]:
        out["error"] = ("MetaTrader 5 n'est pas disponible sur ce système. Sous Linux, installe le pont "
                        "MT5 (./install.sh --with-mt5) ; sinon continue avec Yahoo Finance.")
        return out
    try:
        with link.lock:
            mt5 = link.ensure()
            out["connected"] = True
            out["terminal_path"] = link.path_in_use
            a, t = mt5.account_info(), mt5.terminal_info()
            if a is not None:
                out["account"] = {"login": a.login, "server": a.server, "name": a.name,
                                  "currency": a.currency, "balance": a.balance,
                                  "type": "DÉMO" if a.trade_mode == 0 else "RÉEL"}
            out["algo_trading"] = bool(t and t.trade_allowed)
            groups = ("*XAU*", "*GOLD*") if symbol_hint.upper().startswith(GOLD_BASES) else (f"*{symbol_hint[:6]}*",)
            names = set()
            for g in groups:
                names |= {s.name for s in (mt5.symbols_get(group=g) or ())}
            # l'or contre le dollar seulement (pas XAUEUR, XAUAUD…)
            out["symbols"] = sorted(n for n in names if "XAU" not in n.upper() or "XAUUSD" in n.upper())
            out["symbol"] = pick_symbol(out["symbols"], "XAUUSD" if symbol_hint.upper().startswith(GOLD_BASES) else symbol_hint[:6])
            if out["symbol"]:
                mt5.symbol_select(out["symbol"], True)
                tick = mt5.symbol_info_tick(out["symbol"])
                if tick is not None and tick.time:
                    out["offset"] = offset_from_tick(tick.time)
            if out["offset"] is None:
                out["offset_reason"] = ("Marché fermé ou cotation ancienne : décalage non détectable "
                                        "maintenant (souvent 2 en hiver, 3 en été).")
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    return out


def _money(v: float) -> str:
    return f"{v:,.2f}".replace(",", "\u202f").replace(".", ",")


def _check(cid, label, status, detail="", fix=""):
    return {"id": cid, "label": label, "status": status, "detail": detail, "fix": fix}


def diagnostics(service, executor, link=LINK) -> dict:
    cfg = service.cfg
    checks = []
    snap = service.snapshot()
    checks.append(_check("version", "Version", "ok", f"Gold AI Signal Bot {VERSION}"))

    # données
    if service.error:
        checks.append(_check("data", "Données", "err", service.error,
                             "Ouvre MetaTrader 5 puis clique sur Analyser, ou change la source dans Réglages → Données."))
    elif snap.get("ready"):
        last = datetime.fromisoformat(snap["last_close"])
        age_min = (datetime.now(UTC) - last).total_seconds() / 60
        weekend = datetime.now(UTC).weekday() >= 5
        if snap["source"] == "demo":
            checks.append(_check("data", "Données", "warn", "Mode Démo : prix simulés",
                                 "Réglages → Données → Source : Auto ou MetaTrader 5."))
        elif age_min > 15 and not weekend:
            checks.append(_check("data", "Données", "warn",
                                 f"{snap['source_label']} — dernière bougie il y a {age_min:.0f} min",
                                 "Si le marché est ouvert : vérifie la connexion de MT5 et le décalage horaire."))
        else:
            checks.append(_check("data", "Données", "ok",
                                 f"{snap['source_label']} — dernière bougie {last:%d/%m %H:%M} UTC"))
    else:
        checks.append(_check("data", "Données", "err", snap.get("error") or "Aucune donnée chargée",
                             "Ouvre MetaTrader 5 puis clique sur Analyser, ou passe en source Yahoo / Démo."))

    # MetaTrader 5
    needs_mt5 = cfg.source in ("auto", "mt5") or cfg.trading.mode == "mt5"
    if not link.available:
        checks.append(_check("mt5", "MetaTrader 5", "warn" if not needs_mt5 or os.name != "nt" else "err",
                             "MetaTrader 5 indisponible sur ce système",
                             "Windows : inclus dans l'installateur. Linux : ./install.sh --with-mt5 (pont Wine)."))
    else:
        try:
            with link.lock:
                mt5 = link.ensure()
                a, t = mt5.account_info(), mt5.terminal_info()
                s = mt5.symbol_info(cfg.symbol)
            where = f"pont Wine {link.bridge}" if link.bridge else (link.path_in_use or "terminal par défaut")
            checks.append(_check("mt5", "Terminal MetaTrader 5", "ok", f"Connecté ({where})"))
            if a is not None:
                kind = "DÉMO" if a.trade_mode == 0 else "RÉEL"
                checks.append(_check("account", "Compte", "ok" if a.trade_mode == 0 else "warn",
                                     f"{kind} {a.login} · {a.server} · {_money(a.balance)} {a.currency}",
                                     "" if a.trade_mode == 0 else "Compte réel : teste d'abord le robot sur un compte démo."))
            checks.append(_check("symbol", f"Symbole {cfg.symbol}", "ok" if s else "err",
                                 "Trouvé chez le courtier" if s else "Introuvable chez ce courtier",
                                 "" if s else "Relance l'assistant (Réglages → Assistant) pour détecter le bon nom."))
            algo = bool(t and t.trade_allowed)
            checks.append(_check("algo", "Bouton « Algo Trading »", "ok" if algo else
                                 ("err" if executor.enabled else "warn"),
                                 "Activé" if algo else "Désactivé dans MetaTrader 5",
                                 "" if algo else "Nécessaire pour le robot : clique sur « Algo Trading » dans MT5 (Ctrl+E)."))
        except Exception as e:  # noqa: BLE001
            checks.append(_check("mt5", "Terminal MetaTrader 5", "err" if needs_mt5 else "warn", str(e),
                                 "Ouvre MetaTrader 5 et connecte-toi à ton compte, puis clique sur « Vérifier »."))

    # robot
    if executor.enabled:
        checks.append(_check("robot", "Robot", "warn" if executor.paused else "ok",
                             executor.paused or f"Actif · {executor.broker.name} · risque {cfg.trading.risk_percent} %"))
    else:
        checks.append(_check("robot", "Robot", "skip", "Désactivé (normal tant que tu ne l'actives pas)"))
    if executor.last_error:
        checks.append(_check("robot_err", "Dernière erreur du robot", "warn", executor.last_error))

    # fichiers
    d = user_dir()
    try:
        probe = d / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        checks.append(_check("files", "Dossier des données", "ok", str(d.resolve())))
    except OSError as e:
        checks.append(_check("files", "Dossier des données", "err", f"{d} non accessible en écriture : {e}",
                             "Vérifie les droits du dossier ou réinstalle l'application."))

    worst = "err" if any(c["status"] == "err" for c in checks) else \
            "warn" if any(c["status"] == "warn" for c in checks) else "ok"
    return {"status": worst, "checks": checks, "time": datetime.now(UTC).isoformat()}
