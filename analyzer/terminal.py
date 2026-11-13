"""Notifications dans le terminal : bandeau coloré + bip à chaque signal,
et une ligne de suivi du marché à chaque clôture M5."""
from __future__ import annotations

import os
import sys
from datetime import datetime

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, RED, YELLOW, CYAN, GREY = "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[90m"
BG_GREEN, BG_RED = "\033[42m\033[30m", "\033[41m\033[97m"


def _enable_ansi() -> bool:
    if os.getenv("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name == "nt":  # active les couleurs dans l'invite de commandes Windows 10+
        os.system("")
    return True


class TerminalNotifier:
    def __init__(self, symbol: str = "XAUUSD", bell: bool = True, color: bool | None = None):
        self.symbol = symbol
        self.bell = bell
        self.color = _enable_ansi() if color is None else color

    def _c(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text

    # ---------------------------------------------------------------- #
    def banner(self, url: str, source: str) -> None:
        line = "─" * 62
        print(self._c(CYAN, line))
        from .paths import VERSION
        print(self._c(BOLD, f"  Gold AI Signal Bot {VERSION} — espace d'analyse"))
        print(f"  Interface : {self._c(BOLD, url)}")
        print(f"  Données   : {source}")
        print(self._c(GREY, "  Les signaux s'afficheront ici et dans le navigateur. Ctrl+C pour arrêter."))
        print(self._c(CYAN, line), flush=True)

    def market(self, snap: dict) -> None:
        if not snap.get("ready"):
            return
        ctx = snap["context"]
        bias = {1: self._c(GREEN, "H1 ▲"), -1: self._c(RED, "H1 ▼"), 0: self._c(GREY, "H1 ↔")}[ctx["h1_bias"]]
        t = datetime.fromisoformat(snap["last_close"]).strftime("%H:%M")
        radar = snap.get("radar")
        if radar:
            col = GREEN if radar["direction"] == 1 else RED
            state = self._c(BOLD + col, "SIGNAL") if not radar["blocked"] else self._c(GREY, radar["blocked"])
            setup = f"{self._c(col, radar['side'])} {radar['pattern']} {radar['score']}/10 — {state}"
        else:
            setup = self._c(GREY, "aucune figure")
        sess = "" if ctx["in_session"] else self._c(GREY, " · hors session")
        price = self._c(BOLD, "%.2f" % snap["price"])
        print(f"{self._c(DIM, t)}  {self.symbol} {price}  {bias}  "
              f"RSI {ctx['rsi']:.0f}  ADX {ctx['adx']:.0f}  ATR {ctx['atr_pips']:.1f}p{sess}  │  {setup}",
              flush=True)

    def signal(self, s: dict) -> None:
        buy = s["direction"] == 1
        head = f"  {'▲ ACHAT' if buy else '▼ VENTE'} {self.symbol}  ·  qualité {s['quality']}  ·  score {s['score']}/10  "
        t = datetime.fromisoformat(s["close_time"]).strftime("%d/%m %H:%M UTC")
        if abs(s["tp2"] - s["tp1"]) < 1e-5:
            tps = f"TP {self._c(GREEN, '%.2f' % s['tp1'])} (1:{s['rr1']})"
        else:
            tps = (f"TP1 {self._c(GREEN, '%.2f' % s['tp1'])} (1:{s['rr1']})   "
                   f"TP2 {self._c(GREEN, '%.2f' % s['tp2'])} (1:{s['rr2']})")
        out = [
            "",
            self._c(BG_GREEN if buy else BG_RED, head),
            f"  {s['pattern']}  —  bougie clôturée {t}",
            f"  Entrée {self._c(BOLD, '%.2f' % s['entry'])}   "
            f"Stop {self._c(RED, '%.2f' % s['sl'])} ({s['risk_pips']} pips)   " + tps,
        ]
        for f in s["factors"]:
            mark = self._c(GREEN, "✓") if f["points"] > 0 else self._c(RED, "✗") if f["points"] < 0 else self._c(GREY, "·")
            pts = self._c(GREY, "%+g" % f["points"])
            out.append(f"    {mark} {f['label']}  {pts}")
        out.append("")
        print("\n".join(out), flush=True)
        if self.bell:
            sys.stdout.write("\a")
            sys.stdout.flush()

    def bot(self, entry: dict) -> None:
        """Activité du robot : ordres, refus, break-even, erreurs."""
        col = {"ok": GREEN, "warn": YELLOW, "err": RED}.get(entry["level"], CYAN)
        t = datetime.fromisoformat(entry["time"]).strftime("%H:%M:%S")
        print(f"{self._c(DIM, t)}  {self._c(BOLD + col, 'ROBOT')}  {self._c(col, entry['message'])}", flush=True)
        if entry["level"] == "ok" and self.bell:
            sys.stdout.write("\a")
            sys.stdout.flush()

    def error(self, message: str) -> None:
        print(self._c(YELLOW, f"⚠ {message}"), file=sys.stderr, flush=True)
