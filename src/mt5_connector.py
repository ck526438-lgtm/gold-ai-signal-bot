"""Connexion MetaTrader 5 : initialisation, vérification compte démo, reconnexion.

Le module `MetaTrader5` n'existe que sous Windows. Il est importé paresseusement
et peut être injecté (mock) pour les tests.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from loguru import logger

TIMEFRAMES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}
ACCOUNT_TRADE_MODE_DEMO = 0


def import_mt5():
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:  # pragma: no cover - dépend de l'OS
        raise RuntimeError(
            "Le paquet MetaTrader5 est introuvable. Il ne fonctionne que sous Windows : "
            "pip install MetaTrader5"
        ) from exc
    return mt5


class MT5Connector:
    def __init__(self, cfg: dict, mt5_module: Any = None, notifier=None):
        self.cfg = cfg
        self.mt5_cfg = cfg["mt5"]
        self.symbol = cfg["symbol"]
        self._mt5 = mt5_module
        self.notifier = notifier
        self.connected = False
        self._disconnect_notified = False

    @property
    def mt5(self):
        if self._mt5 is None:
            self._mt5 = import_mt5()
        return self._mt5

    def timeframe(self, name: str) -> int:
        const = getattr(self.mt5, f"TIMEFRAME_{name}", None)
        if const is None:
            raise ValueError(f"Timeframe inconnu : {name}")
        return const

    # ------------------------------------------------------------------ connexion
    def connect(self) -> bool:
        kwargs: dict[str, Any] = {"timeout": int(self.mt5_cfg.get("timeout_ms", 10000))}
        if self.mt5_cfg.get("path"):
            kwargs["path"] = self.mt5_cfg["path"]
        login = int(os.environ.get("MT5_LOGIN") or self.mt5_cfg.get("login") or 0)
        password = os.environ.get("MT5_PASSWORD", "")
        server = self.mt5_cfg.get("server", "")
        if login and password:
            kwargs.update(login=login, password=password, server=server)
        if not self.mt5.initialize(**kwargs):
            logger.error(f"MT5 initialize() a échoué : {self.mt5.last_error()}")
            self.connected = False
            return False
        acc = self.mt5.account_info()
        if acc is not None and login and int(acc.login) != login:
            logger.error(
                f"Le terminal MT5 est connecté au compte {acc.login}, pas au compte {login} attendu. "
                "Connecte-toi au bon compte dans MT5 (Fichier → Se connecter à un compte)."
            )
            self.connected = False
            return False
        if acc is not None and server and acc.server != server:
            logger.warning(f"Serveur MT5 « {acc.server} » différent de config.yaml « {server} »")
        if not self.mt5.symbol_select(self.symbol, True):
            logger.error(f"Impossible de sélectionner {self.symbol} dans le Market Watch")
            self.connected = False
            return False
        self.connected = True
        self._disconnect_notified = False
        acc = self.mt5.account_info()
        if acc is not None:
            logger.info(f"Connecté à MT5 : compte {acc.login} sur {acc.server} ({acc.currency})")
        return True

    def assert_demo_account(self) -> None:
        """Garde-fou : refuse de trader si le compte MT5 n'est pas un compte DÉMO."""
        acc = self.mt5.account_info()
        if acc is None:
            raise RuntimeError("account_info() indisponible — connexion MT5 incomplète.")
        demo_const = getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", ACCOUNT_TRADE_MODE_DEMO)
        if acc.trade_mode != demo_const:
            raise RuntimeError(
                "Le compte MT5 connecté n'est PAS un compte démo. Le bot refuse de trader."
            )

    def is_alive(self) -> bool:
        try:
            info = self.mt5.terminal_info()
            return bool(info is not None and getattr(info, "connected", True))
        except Exception:
            return False

    def ensure_connected(self) -> bool:
        if self.connected and self.is_alive():
            return True
        self.connected = False
        if not self._disconnect_notified:
            logger.warning("Connexion MT5 perdue — tentative de reconnexion")
            if self.notifier:
                self.notifier.alert_disconnect("Connexion MT5 perdue, reconnexion en cours")
            self._disconnect_notified = True
        try:
            self.mt5.shutdown()
        except Exception:
            pass
        time.sleep(float(self.mt5_cfg.get("reconnect_delay_s", 10)))
        ok = self.connect()
        if ok and self.notifier:
            self.notifier.send("🔌 MT5 reconnecté")
        return ok

    def shutdown(self) -> None:
        try:
            self.mt5.shutdown()
        finally:
            self.connected = False

    # ------------------------------------------------------------------ infos
    def account_info(self):
        return self.mt5.account_info()

    def symbol_info(self):
        return self.mt5.symbol_info(self.symbol)

    def tick(self):
        return self.mt5.symbol_info_tick(self.symbol)

    def spread_points(self) -> Optional[float]:
        tick, info = self.tick(), self.symbol_info()
        if tick is None or info is None:
            return None
        return (tick.ask - tick.bid) / info.point

    def positions(self):
        res = self.mt5.positions_get(symbol=self.symbol)
        magic = int(self.cfg["execution"]["magic"])
        return [p for p in (res or []) if getattr(p, "magic", magic) == magic]
