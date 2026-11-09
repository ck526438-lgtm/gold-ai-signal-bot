"""Notifications Telegram + rendu console.

Tout en ASCII: les emojis et les accents passent mal selon le terminal Windows,
et un message casse n'est pas livre.
"""
from __future__ import annotations

import json

from .config import get

ARROW = {"BUY": ">>> ACHAT", "SELL": "<<< VENTE", "FLAT": "--- PLAT"}


def format_signal(sig: dict, engine_label: str) -> str:
    bars = ""
    if sig.get("reasons"):
        parts = sorted(sig["reasons"].items(), key=lambda kv: -abs(kv[1]))[:4]
        bars = " | ".join(f"{k}:{v:+.2f}" for k, v in parts)
    prob = ""
    if sig.get("probabilities"):
        prob = f"  p(montee)={sig['probabilities'].get('up', 0):.2f}"
    lines = [
        f"[{engine_label}] {ARROW.get(sig['side'], sig['side'])} {sig['symbol']}",
        f"bougie {sig['time'][:16].replace('T',' ')} UTC | score {sig['score']}{prob}",
        f"entree {sig['entry']}  SL {sig['sl']}  TP {sig['tp']}  RR {sig['rr']}  ATR {sig['atr']}",
    ]
    if bars:
        lines.append(bars)
    if sig.get("block_reason"):
        lines.append(f"BLOQUE: {sig['block_reason']}")
    return "\n".join(lines)


def send_telegram(cfg: dict, text: str) -> dict:
    token = str(get(cfg, "notify.telegram.bot_token", "") or "")
    chat = str(get(cfg, "notify.telegram.chat_id", "") or "")
    if not bool(get(cfg, "notify.telegram.enabled", False)):
        return {"sent": False, "reason": "telegram desactive"}
    if not token or not chat:
        return {"sent": False, "reason": "bot_token ou chat_id vides (.env)"}
    import requests

    resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"},
                         timeout=20)
    payload = resp.json() if resp.content else {}
    return {"sent": bool(payload.get("ok")), "status": resp.status_code,
            "error": payload.get("description")}


def emit(cfg: dict, text: str, quiet: bool = False) -> dict:
    if not quiet:
        print(text, flush=True)
    return send_telegram(cfg, text)


def format_heartbeat(bar_time, prices: dict, stats: dict) -> str:
    return (f"heartbeat {str(bar_time)[:16].replace('T',' ')} UTC | "
             + " | ".join(f"{k}:{v}" for k, v in prices.items())
             + f" | paper {json.dumps(stats, default=str)}")
