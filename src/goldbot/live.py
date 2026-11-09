"""Boucle live: une analyse par cloture de bougie M5.

A faire tourner sur la machine qui a les donnees (Windows natif, ou Kali via le
bridge). Le bot ne prend aucune position tant que trading.auto_execute reste
false: il journalise et notifie, et le meme journal sert de test paper.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from .config import get
from .features import compute_features
from .journal import (close_paper, connect, fill_paper, get_state, list_open, open_paper,
                      paper_summary, put_state, record_signal, last_fire_time)
from .notify import emit, format_signal
from .risk import position_size
from .signals import rules as rules_engine

TAIL_BARS = 2500
ENGINES = {"rules": ["rules"], "ml": ["ml"], "both": ["rules", "ml", "consensus"]}


def seconds_to_next_close(tf_minutes: int, delay: float = 4.0) -> float:
    now = datetime.now(timezone.utc)
    minutes = now.minute
    nxt = (minutes // tf_minutes + 1) * tf_minutes
    target = now.replace(second=0, microsecond=0) + timedelta(minutes=nxt - minutes, seconds=delay)
    if nxt >= 60:
        target = now.replace(minute=nxt - 60, second=0, microsecond=0) + timedelta(hours=1, seconds=delay)
    return max(1.0, (target - now).total_seconds())


def score_frame(df: pd.DataFrame, cfg: dict, want_ml: bool) -> pd.DataFrame:
    scored = rules_engine.rule_frame(compute_features(df, cfg), cfg)
    if want_ml:
        from .signals.ml import add_ml_columns

        try:
            scored = add_ml_columns(scored, cfg, walk_forward_mode=False)
        except RuntimeError as exc:
            emit(cfg, f"ML indisponible ({exc}) -> moteur regles seul.", quiet=True)
            scored["ml_side"], scored["ml_conf"], scored["ml_prob_up"] = "FLAT", 50.0, float("nan")
            scored["ml_fire"] = False
    return scored


def _engine_verdict(scored: pd.DataFrame, cfg: dict, engine: str, symbol: str) -> dict:
    row = scored.iloc[-1]
    if engine == "rules":
        sig = rules_engine.rules_signal(row, symbol, cfg).as_dict()
        sig["consensus"] = False
        return sig
    if engine == "consensus":
        a = _engine_verdict(scored, cfg, "rules", symbol)
        b = _engine_verdict(scored, cfg, "ml", symbol)
        agree = a["side"] == b["side"] != "FLAT"
        out = dict(a if agree else b)
        out["side"] = a["side"] if agree else "FLAT"
        out["engine"] = "consensus"
        out["consensus"] = True
        out["score"] = round((a["score"] + b["score"]) / 2, 1)
        out["probabilities"] = b.get("probabilities")
        out["block_reason"] = "" if agree else "accord des 2 moteurs requis"
        out["fires"] = agree and bool(row.get("rules_fire")) and bool(row.get("ml_fire"))
        return out
    thr = float(get(cfg, "signal.ml.prob_threshold", 0.58))
    from .risk import bracket

    sl, tp, rr = bracket(float(row["close"]), row["ml_side"], float(row["atr"]), cfg)
    return {
        "time": row["time"].isoformat(), "symbol": symbol, "engine": "ml",
        "side": row["ml_side"], "score": float(row["ml_conf"]), "entry": float(row["close"]),
        "sl": sl, "tp": tp, "rr": rr, "atr": float(row["atr"]),
        "probabilities": {"up": round(float(row["ml_prob_up"]), 4)} if pd.notna(row["ml_prob_up"]) else None,
        "reasons": {}, "block_reason": "" if row["allowed"] else str(row["block_reason"]),
        "consensus": False,
    }


def _fires(scored: pd.DataFrame, engine: str) -> bool:
    col = {"rules": "rules_fire", "ml": "ml_fire", "consensus": None}[engine]
    if col is None:
        row = scored.iloc[-1]
        return bool(row.get("rules_fire")) and bool(row.get("ml_fire")) and row["rules_side"] == row["ml_side"]
    return bool(scored.iloc[-1][col])


def manage_paper(db, cfg: dict, scored: pd.DataFrame) -> list[dict]:
    """Remplit et clot les trades paper en rejouant la regle du backtest.

    Meme convention des deux cotes: remplissage a l'ouverture de la bougie
    suivante, SL prioritaire sur le TP dans la meme bougie, sortie sur
    retournement du signal, plafond en bougies depuis le signal, cout unique.
    """
    from .backtest import engine_columns
    from .data.bars import TIMEFRAME_MINUTES
    from .risk import round_turn_cost

    bar = scored.iloc[-1]
    cost = round_turn_cost(cfg)
    max_hold = int(get(cfg, "backtest.max_hold_bars", 72))
    tf_minutes = TIMEFRAME_MINUTES.get(str(get(cfg, "symbol.timeframe", "M5")).upper(), 5)
    exit_on_flip = bool(get(cfg, "backtest.exit_on_opposite_state", True))
    bar_time = pd.Timestamp(bar["time"])
    closed = []

    for trade in list_open(db):
        side = 1 if trade["side"] == "BUY" else -1
        if trade["status"] == "pending":
            if bar_time <= pd.Timestamp(trade["signal_time"]):
                continue
            entry = float(bar["open"])
            sl, tp = entry - side * float(trade["sl_dist"]), entry + side * float(trade["tp_dist"])
            fill_paper(db, trade["id"], bar_time.isoformat(), entry, sl, tp)
            trade = {**trade, "entry": entry, "sl": sl, "tp": tp}
        sl, tp = float(trade["sl"]), float(trade["tp"])
        o, ts = float(bar["open"]), bar_time.isoformat()

        if (bar["low"] <= sl) if side > 0 else (bar["high"] >= sl):
            px = o if (side > 0 and o < sl) or (side < 0 and o > sl) else sl
            closed.append(close_paper(db, trade["id"], ts, float(px), "sl", cost))
        elif (bar["high"] >= tp) if side > 0 else (bar["low"] <= tp):
            px = o if (side > 0 and o > tp) or (side < 0 and o < tp) else tp
            closed.append(close_paper(db, trade["id"], ts, float(px), "tp", cost))
        else:
            flip = False
            if exit_on_flip:
                fire, sign, _ = engine_columns(scored, trade["engine"])
                flip = bool(fire[-1]) and int(sign[-1]) != 0 and (int(sign[-1]) > 0) != (side > 0)
            held = (bar_time - pd.Timestamp(trade["signal_time"] or trade["entry_time"])).total_seconds() / 60
            if flip:
                closed.append(close_paper(db, trade["id"], ts, o, "flip", cost))
            elif held >= max_hold * tf_minutes:
                closed.append(close_paper(db, trade["id"], ts, float(bar["close"]), "time", cost))
    return closed


def process_once(cfg: dict, provider, db, verbose: bool = True) -> dict:
    from .data.bars import closed_only
    from .data.history import refresh

    tf = str(get(cfg, "symbol.timeframe", "M5"))
    df = closed_only(refresh(cfg, provider, tf, quiet=not verbose), tf)
    tail = df.tail(TAIL_BARS).reset_index(drop=True)
    bar_time = tail.iloc[-1]["time"]

    # Le week-end et la pause de 21h ne produisent aucune bougie: sans ce garde
    # on recalculerait 2500 features + une predicition ML pour rien toutes les
    # 5 minutes pendant des heures. Les positions ouvertes font exception, il
    # faut bien les evaluer sur la bougie disponible.
    if bar_time.isoformat() == get_state(db, "last_bar") and not list_open(db):
        return {"bar": str(bar_time), "skipped": True, "signals": [],
                "paper": paper_summary(db), "closed_now": []}

    scored = score_frame(tail, cfg, want_ml=True)
    symbol = provider.symbol

    closed = manage_paper(db, cfg, scored)
    for trade in closed:
        emit(cfg, format_signal({**trade, "score": 0, "reasons": {}}, f"PAPER CLOTURE {trade['reason']}"), verbose)

    engine_mode = str(get(cfg, "signal.engine", "both"))
    cooldown = int(get(cfg, "signal.cooldown_bars", 6)) * 5
    results = []
    # Le papier est une simulation: son volume se dimensionne sur le solde
    # fictif configure, pas sur le solde reel du compte (un compte demo a 1 $
    # donnerait des volumes absurdes). En execution reelle, c'est le reel.
    balance = get(cfg, "risk.paper_balance", 1000)
    if bool(get(cfg, "trading.auto_execute", False)):
        balance = provider.terminal_info().get("balance") or balance
    for engine in ENGINES.get(engine_mode, ["rules"]):
        sig = _engine_verdict(scored, cfg, engine, symbol)
        fired = _fires(scored, engine)
        bar_time = pd.Timestamp(sig["time"])
        last = last_fire_time(db, engine)
        if fired and last is not None and (bar_time - last).total_seconds() < cooldown:
            fired, sig["block_reason"] = False, "cooldown"
        sig["fires"] = fired
        sig["sl"] = None if sig["sl"] is None else round(sig["sl"], 2)
        sig["tp"] = None if sig["tp"] is None else round(sig["tp"], 2)
        record_signal(db, sig, fired)
        results.append(sig)
        if fired:
            text = format_signal(sig, engine.upper())
            sizing = position_size(balance, abs(sig["entry"] - sig["sl"]), cfg)
            text += f"\npaper volume {sizing['volume']} lot (risque {sizing['risk_usd']} $)"
            open_paper(db, engine, sig, sizing["volume"])
            emit(cfg, text, verbose)
            if engine != "consensus":
                from .mt5_trader import place

                place(cfg, sig["symbol"], sig["side"], sizing["volume"], sig["sl"], sig["tp"])
    put_state(db, "last_bar", scored.iloc[-1]["time"].isoformat())
    return {"bar": str(scored.iloc[-1]["time"]), "signals": results,
            "paper": paper_summary(db), "closed_now": closed}


def cmd_live(cfg: dict, args) -> int:
    import traceback

    from .data.bars import TIMEFRAME_MINUTES

    db = connect(cfg)
    provider = _provider(cfg, db)
    tf = str(get(cfg, "symbol.timeframe", "M5"))
    tf_minutes = TIMEFRAME_MINUTES.get(tf.upper(), 5)
    cycles, failures = 0, 0
    try:
        while True:
            if not args.once:
                time.sleep(seconds_to_next_close(tf_minutes, args.delay))
            try:
                result = process_once(cfg, provider, db, verbose=True)
                failures = 0
            except Exception:
                # Un cycle qui plante ne doit pas tuer une campagne de plusieurs
                # jours: on journalise et on retente la bougie suivante.
                failures += 1
                print(f"[{datetime.now(timezone.utc)}] cycle echoue ({failures}):", flush=True)
                print(traceback.format_exc(), flush=True)
                if failures >= 12:
                    print("12 cycles consecutifs en echec: le terminal MT5 est probablement ferme.",
                          flush=True)
                    return 1
                if args.once:
                    raise
                continue
            cycles += 1
            print(json.dumps({"bar": result["bar"], "paper": result["paper"]}, indent=2), flush=True)
            if args.heartbeat and cycles % args.heartbeat == 0 and result["signals"]:
                emit(cfg, format_heartbeat(datetime.fromisoformat(result["signals"][0]["time"]),
                                           {"spread_pts": round(float(result["signals"][0]["atr"]), 2)},
                                           result["paper"]), verbose=False)
            if args.once:
                break
    except KeyboardInterrupt:
        print("\narret")
    finally:
        provider.shutdown()
        db.close()
    return 0
    return 0


def cmd_paper(cfg: dict, args) -> int:
    db = connect(cfg)
    print(json.dumps(paper_summary(db), indent=2, default=str))
    return 0


def _provider(cfg: dict, db):
    from .data import make_provider

    provider = make_provider(cfg)
    info = provider.connect()
    put_state(db, "provider", {k: str(v) for k, v in info.items()})
    return provider
