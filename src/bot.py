"""Boucle de trading live (compte DÉMO uniquement). Utilisée par main.py et l'appli Windows."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from .config import load_config
from .data_feed import DataFeed
from .env import load_env
from .execution import Executor
from .filters import EntryFilters
from .logging_setup import register_secret, setup_logging
from .mt5_connector import MT5Connector
from .risk_manager import RiskManager
from .state import StateStore
from .strategy import build_strategy
from .telegram_notifier import NullNotifier, TelegramNotifier


class TradingBot:
    def __init__(self, cfg: dict, mt5_module=None, notifier=None, stop_event: Optional[threading.Event] = None):
        if cfg["mode"] != "demo":
            raise RuntimeError(
                f"mode='{cfg['mode']}' : la boucle live ne tourne qu'en mode 'demo'. "
                "Pour un backtest : python backtest.py"
            )
        self.cfg = cfg
        self.stop_event = stop_event or threading.Event()
        mon = cfg.get("monitoring", {})
        if notifier is None:
            notifier = TelegramNotifier(cfg, status_provider=self.status_text) if mon.get("enabled", True) else NullNotifier()
        self.notifier = notifier
        self.state = StateStore(mon.get("pause_file", "state.json"))
        self.conn = MT5Connector(cfg, mt5_module, notifier)
        self.feed = DataFeed(self.conn, cfg)
        self.strategy = build_strategy(cfg)
        self.filters = EntryFilters(cfg)
        self.risk = RiskManager(cfg, notifier)
        self.executor = Executor(self.conn, cfg, self.risk, notifier)
        self.report_hour = int(mon.get("daily_report_hour_utc", 22))
        self.last_report_day = None
        self.last_bar_time = None

    # ------------------------------------------------------------------ statut
    def status_text(self) -> str:
        try:
            acc = self.conn.account_info()
            pos = self.conn.positions()
            floating = sum(float(p.profit) for p in pos)
            lines = [f"📟 STATUT — {self.strategy.name} ({self.strategy.timeframe})"]
            st = self.state.read()
            lines.append("⏸️ EN PAUSE" if st.get("paused") else ("⚠️ KILL-SWITCH : " + self.risk.lock_reason if self.risk.locked else "▶️ Actif"))
            lines.append(f"Positions : {len(pos)} | PnL flottant : {floating:+.2f}$")
            for p in pos:
                side = "BUY" if p.type == 0 else "SELL"
                lines.append(f"  #{p.ticket} {side} {p.volume} @ {p.price_open} SL {p.sl} TP {p.tp} ({p.profit:+.2f}$)")
            if acc is not None:
                lines.append(f"Equity : {acc.equity:.2f}$ | DD jour : {self.risk.daily_drawdown_pct(acc.equity):.2f}%")
            return "\n".join(lines)
        except Exception as exc:
            return f"Statut indisponible : {exc}"

    # ------------------------------------------------------------------ boucle
    def start(self) -> None:
        if not self.conn.connect():
            raise RuntimeError("Connexion MT5 impossible. Vérifie que le terminal est ouvert et connecté.")
        self.conn.assert_demo_account()
        if self.strategy.name == "ml_xgboost":
            self.strategy.check_ready(is_demo_account=True)
        self.state.update(stop_requested=False, close_positions_on_stop=False)
        if hasattr(self.notifier, "start"):
            self.notifier.start()
        acc = self.conn.account_info()
        self.notifier.send(
            f"🤖 Bot démarré — {self.cfg['symbol']} {self.strategy.name} {self.strategy.timeframe} "
            f"| equity {acc.equity:.2f}$ (DÉMO)", 0,
        )
        self.executor.sync_positions()

    def step(self) -> bool:
        """Une itération. Renvoie False pour arrêter la boucle."""
        st = self.state.read()
        if st.get("stop_requested") or self.stop_event.is_set():
            if st.get("close_positions_on_stop"):
                n = self.executor.close_all()
                logger.warning(f"/stop confirm close : {n} position(s) fermée(s)")
            return False
        if st.get("resume_requested"):
            self.risk.manual_unlock()
            self.state.update(resume_requested=False)

        if not self.conn.ensure_connected():
            return True

        self.executor.sync_positions()
        acc = self.conn.account_info()
        if acc is None:
            return True
        now = datetime.now(timezone.utc)
        self.risk.update_equity(float(acc.equity), now.date())

        if now.hour >= self.report_hour and self.last_report_day != now.date():
            self.last_report_day = now.date()
            self.notifier.daily_report(equity=float(acc.equity), drawdown=self.risk.daily_drawdown_pct(float(acc.equity)))
            logger.info(f"Résumé quotidien : equity={acc.equity:.2f} DD jour={self.risk.daily_drawdown_pct(acc.equity):.2f}%")

        bars = self.feed.get_closed_bars(self.strategy.timeframe, max(self.strategy.min_bars() + 50, 400))
        if bars.empty:
            return True
        if self.executor.open_trades:
            from . import indicators as ind

            self.executor.manage_trailing(float(ind.atr(bars, self.risk_atr_period()).iloc[-1]))

        if bars.index[-1] == self.last_bar_time:
            return True  # une seule décision par bougie clôturée
        self.last_bar_time = bars.index[-1]

        if st.get("paused"):
            logger.debug("En pause — aucune entrée")
            return True
        ok, why = self.risk.can_trade()
        if not ok:
            logger.debug(f"Kill-switch actif ({why})")
            return True

        signal = self.strategy.generate_signal(bars)
        if signal is None:
            return True
        spread = self.conn.spread_points()
        allowed, reason = self.filters.check(now_ts(), spread, signal.atr, len(self.conn.positions()))
        logger.info(f"SIGNAL {signal.side} {signal.time} {signal.reason} → {'OK' if allowed else 'refusé: ' + reason}")
        if not allowed:
            return True
        info, tick = self.conn.symbol_info(), self.conn.tick()
        entry = tick.ask if signal.direction > 0 else tick.bid
        plan = self.risk.plan_trade(
            signal, float(acc.equity), entry, float(info.trade_contract_size), float(info.volume_min),
            float(info.volume_step), float(info.volume_max), float(self.cfg["backtest"]["commission_per_lot"]), int(info.digits),
        )
        if plan is not None:
            self.executor.open_position(plan, expected_price=entry)
        return True

    def risk_atr_period(self) -> int:
        return int(self.cfg["risk"].get("atr_period", 14))

    def run(self) -> None:
        self.start()
        poll = float(self.cfg["loop"]["poll_seconds"])
        try:
            while self.step():
                if self.stop_event.wait(poll):
                    break  # arrêt depuis l'appli : positions conservées
        except KeyboardInterrupt:
            logger.info("Interruption clavier")
        finally:
            self.notifier.send("🛑 Bot arrêté (positions conservées sauf /stop confirm close).", 0)
            if hasattr(self.notifier, "flush"):
                self.notifier.flush(10)
            if hasattr(self.notifier, "stop"):
                self.notifier.stop()
            self.conn.shutdown()
            self.state.update(stop_requested=False)
            logger.info("Bot arrêté proprement")


def now_ts():
    import pandas as pd

    return pd.Timestamp.now(tz="UTC")


def run_bot(
    config_path: Optional[str] = None, stop_event: Optional[threading.Event] = None, log_sink=None,
    strategy_name: Optional[str] = None,
) -> None:
    load_env()
    cfg = load_config(config_path)
    if strategy_name:
        cfg["strategy"]["name"] = strategy_name
    setup_logging(cfg, log_sink)
    import os

    register_secret(os.environ.get("TELEGRAM_TOKEN"), os.environ.get("MT5_PASSWORD"))
    TradingBot(cfg, stop_event=stop_event).run()
