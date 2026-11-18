"""Monitoring Telegram : alertes sortantes + commandes entrantes.

* API HTTP Telegram via `requests` (aucune librairie lourde).
* Token et chat_id lus depuis .env (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID).
* File d'attente interne : 1 message / `min_interval_s` (3 s par défaut).
* 3 tentatives avec backoff exponentiel (1 s, 2 s, 4 s), respect de `retry_after` (HTTP 429).
* dry_run=true : les messages sont loggés au lieu d'être envoyés.
* Le token n'apparaît JAMAIS dans les logs (masquage + messages d'erreur nettoyés).
* Seul le chat_id configuré peut piloter le bot. /stop exige « /stop confirm ».
"""
from __future__ import annotations

import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd
import requests
from loguru import logger

from .config import resolve
from .logging_setup import redact, register_secret
from .state import StateStore

API = "https://api.telegram.org/bot{token}/{method}"
VERBOSITY = {"minimal": 0, "normal": 1, "debug": 2}


class TelegramNotifier:
    def __init__(
        self,
        cfg: dict,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
        session: Optional[requests.Session] = None,
        status_provider: Optional[Callable[[], str]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        m = cfg.get("monitoring", {})
        self.cfg = cfg
        self.enabled = bool(m.get("enabled", True))
        self.dry_run = bool(m.get("dry_run", True))
        self.verbosity = VERBOSITY.get(str(m.get("verbosity", "normal")), 1)
        self.min_interval = float(m.get("min_interval_s", 3))
        self.poll_seconds = float(m.get("poll_seconds", 30))
        self.confirm_timeout = float(m.get("stop_confirm_timeout_s", 120))
        self.token = token if token is not None else os.environ.get("TELEGRAM_TOKEN", "")
        self.chat_id = str(chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", ""))
        register_secret(self.token)
        if not self.dry_run and (not self.token or not self.chat_id):
            logger.warning("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID absents de .env → passage en dry_run")
            self.dry_run = True
        self.session = session or requests.Session()
        self.state = StateStore(m.get("pause_file", "state.json"))
        self.status_provider = status_provider
        self.trades_csv = cfg.get("logging", {}).get("trades_csv", "logs/trades.csv")
        self._sleep = sleep
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_sent = 0.0
        self._offset: Optional[int] = None
        self._stop_pending_until: Optional[float] = None
        self.sent_log: list[str] = []  # historique (utile pour dry_run et tests)

    # ================================================================ envoi
    def _url(self, method: str) -> str:
        return API.format(token=self.token, method=method)

    def _safe(self, text: str) -> str:
        return redact(str(text), [self.token])

    def _request(self, method: str, payload: dict, timeout: float = 15) -> Optional[dict]:
        delay = 1.0
        for attempt in range(1, 4):
            try:
                r = self.session.post(self._url(method), json=payload, timeout=timeout)
                if r.status_code == 429:
                    retry = float(r.json().get("parameters", {}).get("retry_after", delay))
                    logger.warning(f"Telegram 429 — attente {retry:.0f}s")
                    self._sleep(retry)
                    continue
                if r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                data = r.json()
                if not data.get("ok", False):
                    logger.error(f"Telegram {method} refusé : {self._safe(data.get('description', ''))}")
                    return None
                return data
            except (requests.RequestException, ValueError) as exc:
                logger.warning(f"Telegram {method} échec {attempt}/3 : {self._safe(type(exc).__name__)}: {self._safe(exc)}")
                if attempt < 3:
                    self._sleep(delay)
                    delay *= 2
        logger.error(f"Telegram {method} : abandon après 3 tentatives")
        return None

    def _deliver(self, text: str) -> bool:
        wait = self.min_interval - (time.monotonic() - self._last_sent)
        if wait > 0:
            self._sleep(wait)
        self._last_sent = time.monotonic()
        self.sent_log.append(text)
        if self.dry_run:
            logger.info(f"[TELEGRAM dry_run] {text}")
            return True
        return self._request("sendMessage", {"chat_id": self.chat_id, "text": text}) is not None

    def send(self, text: str, level: int = 0) -> None:
        """Met le message en file d'attente (non bloquant). level : 0 minimal, 1 normal, 2 debug."""
        if not self.enabled or level > self.verbosity:
            return
        if self._threads:
            self._queue.put(text)
        else:
            self._deliver(text)

    def flush(self, timeout: float = 30) -> None:
        end = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < end:
            time.sleep(0.1)

    # ================================================================ événements
    def notify_entry(self, symbol, side, price, sl, tp, volume, reason, confidence=None):
        conf = f" | conf {confidence:.0%}" if confidence else ""
        self.send(f"✅ ENTRÉE {side} {symbol} @ {price:.2f}\nSL {sl:.2f} | TP {tp:.2f} | {volume} lot\n{reason}{conf}", 1)

    def notify_exit(self, symbol, side, pnl, r_multiple, duration_min):
        icon = "🎯" if pnl >= 0 else "🔻"
        self.send(f"{icon} SORTIE {side} {symbol} : {r_multiple:+.2f}R ({pnl:+.2f}$) en {duration_min:.0f} min", 1)

    def alert_killswitch(self, reason, equity=None, daily_dd=None):
        eq = f"\nEquity {equity:.2f}$" if equity is not None else ""
        dd = f" | DD jour {daily_dd:.2f}%" if daily_dd is not None else ""
        self.send(f"⚠️ KILL-SWITCH : {reason}{eq}{dd}\nTrading suspendu jusqu'à demain (ou /resume).", 0)

    def alert_disconnect(self, msg):
        self.send(f"🔌 {msg}", 0)

    def alert_error(self, msg):
        self.send(f"🔌 Erreur d'exécution : {msg}", 0)

    # ================================================================ rapports
    def _load_trades(self) -> pd.DataFrame:
        p = resolve(self.trades_csv)
        if not p.exists():
            return pd.DataFrame()
        df = pd.read_csv(p)
        if df.empty:
            return df
        df["close_time"] = pd.to_datetime(df["close_time"], utc=True, format="ISO8601")
        return df

    def build_report(self, since: datetime, title: str, equity=None, drawdown=None) -> str:
        df = self._load_trades()
        if not df.empty:
            df = df[df["close_time"] >= pd.Timestamp(since)]
        n = len(df)
        wins = int((df["pnl"] > 0).sum()) if n else 0
        pnl = float(df["pnl"].sum()) if n else 0.0
        wr = wins / n * 100 if n else 0.0
        lines = [f"📊 {title}", f"Trades : {n} | Winrate : {wr:.0f}% | PnL : {pnl:+.2f}$"]
        if drawdown is not None:
            lines.append(f"Drawdown jour : {drawdown:.2f}%")
        if equity is not None:
            lines.append(f"Equity : {equity:.2f}$")
        return "\n".join(lines)

    def daily_report(self, equity=None, drawdown=None) -> None:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.send(self.build_report(start, f"Rapport du {start:%d/%m/%Y}", equity, drawdown), 0)

    def weekly_report_text(self, equity=None) -> str:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.build_report(start, f"Semaine depuis le {start:%d/%m}", equity)

    # ================================================================ commandes
    def handle_command(self, text: str, chat_id: str) -> Optional[str]:
        if str(chat_id) != self.chat_id:
            logger.warning(f"Commande ignorée d'un chat non autorisé ({chat_id})")
            return None
        parts = text.strip().split()
        if not parts:
            return None
        cmd = parts[0].split("@")[0].lower()
        args = [a.lower() for a in parts[1:]]
        if cmd == "/status":
            return self.status_provider() if self.status_provider else "Statut indisponible"
        if cmd == "/pause":
            self.state.update(paused=True)
            return "⏸️ Bot en pause (aucune nouvelle entrée). /resume pour reprendre."
        if cmd == "/resume":
            self.state.update(paused=False, resume_requested=True)
            return "▶️ Trading réactivé."
        if cmd == "/report":
            return self.weekly_report_text()
        if cmd == "/stop":
            now = time.monotonic()
            if args[:1] == ["confirm"]:
                if self._stop_pending_until is None or now > self._stop_pending_until:
                    return "Envoie d'abord /stop, puis /stop confirm dans les 2 minutes."
                close = args[1:2] == ["close"]
                self.state.update(stop_requested=True, close_positions_on_stop=close)
                self._stop_pending_until = None
                return ("🛑 Arrêt demandé. Les positions ouvertes seront FERMÉES." if close
                        else "🛑 Arrêt propre demandé. Les positions ouvertes restent en place (SL/TP actifs).")
            self._stop_pending_until = now + self.confirm_timeout
            return ("Confirmer l'arrêt ?\n• /stop confirm → arrête le bot, garde les positions\n"
                    "• /stop confirm close → arrête ET ferme les positions")
        if cmd in ("/start", "/help"):
            return "Commandes : /status /pause /resume /report /stop"
        return "Commande inconnue. /help"

    def poll_once(self) -> int:
        if self.dry_run:
            return 0
        payload = {"timeout": 0}
        if self._offset is not None:
            payload["offset"] = self._offset
        data = self._request("getUpdates", payload)
        if not data:
            return 0
        n = 0
        for upd in data.get("result", []):
            self._offset = int(upd["update_id"]) + 1
            msg = upd.get("message") or {}
            text = msg.get("text")
            chat = str(msg.get("chat", {}).get("id", ""))
            if not text:
                continue
            reply = self.handle_command(text, chat)
            if reply:
                self.send(reply, 0)
            n += 1
        return n

    # ================================================================ threads
    def _sender_loop(self):
        while not self._stop.is_set() or not self._queue.empty():
            try:
                text = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._deliver(text)
            except Exception as exc:  # ne jamais tuer le thread
                logger.error(f"Envoi Telegram : {self._safe(exc)}")

    def _poll_loop(self):
        # ignorer les anciennes commandes à l'arrivée
        if not self.dry_run:
            data = self._request("getUpdates", {"timeout": 0, "offset": -1})
            if data and data.get("result"):
                self._offset = int(data["result"][-1]["update_id"]) + 1
        while not self._stop.wait(self.poll_seconds):
            try:
                self.poll_once()
            except Exception as exc:
                logger.error(f"Polling Telegram : {self._safe(exc)}")

    def start(self):
        if not self.enabled or self._threads:
            return
        self._stop.clear()
        for target in (self._sender_loop, self._poll_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        mode = "dry_run" if self.dry_run else "actif"
        logger.info(f"Notifier Telegram démarré ({mode})")

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        self._threads = []


class NullNotifier:
    """Remplaçant silencieux quand le monitoring est désactivé."""

    def __getattr__(self, name):
        return lambda *a, **k: None
