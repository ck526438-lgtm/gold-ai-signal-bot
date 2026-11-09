"""MT5 data provider.

`MetaTrader5` n'existe que sous Windows: l'import est fait dans connect() pour
que tout le reste du bot (features, regles, ML, backtest) tourne sur Kali.

Tout ce qui sort de cette classe est en UTC veritable, pas en heure serveur.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import get, resolve_path
from ..timeutil import resolve_offset

# Le serveur ne rend jamais de bougie plus recente que "maintenant" en heure
# broker: cette marge absorbe un decalage d'horloge ou une interpretation
# differente des bornes sans rien perdre.
_QUERY_MARGIN = timedelta(days=3)


class MT5NotAvailable(RuntimeError):
    pass


class MT5Provider:
    name = "mt5"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._mt5 = None
        self._symbol: str | None = None
        self._connected = False
        self.offset = {"hours": 0.0, "source": "unset", "delta": timedelta(0), "warning": None}

    def connect(self) -> dict:
        if self._connected:
            return self.terminal_info()
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:  # Kali / Linux
            raise MT5NotAvailable(
                "Le paquet MetaTrader5 est Windows-only. Sur Kali, lancer le bridge "
                "sur le PC Windows (python bot.py bridge) et mettre data.provider: bridge."
            ) from exc
        self._mt5 = mt5

        mt5_cfg = get(self.cfg, "mt5", {}) or {}
        kwargs = {}
        if mt5_cfg.get("terminal_path"):
            kwargs["path"] = mt5_cfg["terminal_path"]
        if mt5_cfg.get("login"):
            kwargs.update(
                login=int(mt5_cfg["login"]),
                password=str(mt5_cfg.get("password") or ""),
                server=mt5_cfg.get("server") or None,
            )
        if not mt5.initialize(**{k: v for k, v in kwargs.items() if v is not None}):
            code, msg = mt5.last_error()
            raise MT5NotAvailable(f"mt5.initialize a echoue ({code}): {msg}")
        self._connected = True
        self._symbol = self._resolve_symbol()

        tick = mt5.symbol_info_tick(self._symbol)
        cache_dir = resolve_path(get(self.cfg, "data.cache_dir", "data/cache"))
        self.offset = resolve_offset(
            tick.time if tick else None,
            Path(cache_dir),
            mt5_cfg.get("broker_utc_offset_hours"),
        )
        return self.terminal_info()

    @property
    def _off(self) -> timedelta:
        return self.offset["delta"]

    def terminal_info(self) -> dict:
        info, acc = self._mt5.terminal_info(), self._mt5.account_info()
        return {
            "connected": bool(info.connected) if info else False,
            "build": info.build if info else None,
            "company": info.company if info else None,
            "login": acc.login if acc else None,
            "server": acc.server if acc else None,
            "currency": acc.currency if acc else None,
            "balance": acc.balance if acc else None,
            "equity": acc.equity if acc else None,
            "leverage": acc.leverage if acc else None,
            "broker_utc_offset_hours": self.offset["hours"],
            "offset_source": self.offset["source"],
            "offset_warning": self.offset["warning"],
        }

    def _resolve_symbol(self) -> str:
        candidates = get(self.cfg, "symbol.candidates", ["XAUUSD"])
        known = []
        for cand in candidates:
            si = self._mt5.symbol_info(cand)
            if si is None:
                continue
            if si.visible and si.trade_mode != 0:
                self._mt5.symbol_select(cand, True)
                return cand
            known.append(cand)
        raise MT5NotAvailable(
            f"Aucun gold exploitable parmi {candidates} (trouves mais non trading: {known}). "
            "Ajuster symbol.candidates dans config.yaml."
        )

    @property
    def symbol(self) -> str:
        if self._symbol is None:
            self.connect()
        return self._symbol

    def _tf(self, timeframe: str) -> int:
        const = getattr(self._mt5, f"TIMEFRAME_{timeframe.upper()}", None)
        if const is None:
            raise ValueError(f"Timeframe inconnu: {timeframe}")
        return const

    @staticmethod
    def _utc(dt: datetime) -> pd.Timestamp:
        ts = pd.Timestamp(dt)
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

    def bars_range(self, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        start_utc, end_utc = self._utc(start), self._utc(end)
        broker_start = (start_utc + self._off).to_pydatetime().replace(tzinfo=None)
        broker_end = (end_utc + self._off + _QUERY_MARGIN).to_pydatetime().replace(tzinfo=None)
        rates = self._mt5.copy_rates_range(self.symbol, self._tf(timeframe), broker_start, broker_end)
        frame = self._to_frame(rates)
        if frame.empty:
            return frame
        mask = (frame["time"] >= start_utc) & (frame["time"] <= end_utc)
        return frame[mask].reset_index(drop=True)

    def bars_count(self, timeframe: str, count: int, start_pos: int = 0) -> pd.DataFrame:
        rates = self._mt5.copy_rates_from_pos(self.symbol, self._tf(timeframe), start_pos, count)
        return self._to_frame(rates)

    def _to_frame(self, rates) -> pd.DataFrame:
        empty = pd.DataFrame(columns=["time", "open", "high", "low", "close", "tick_volume", "spread_points"])
        if rates is None or len(rates) == 0:
            return empty
        frame = pd.DataFrame(np.asarray(rates)).rename(columns={"spread": "spread_points"})
        # epoch broker -> epoch UTC
        frame["time"] = pd.to_datetime(
            frame["time"].astype("int64") - int(self._off.total_seconds()), unit="s", utc=True
        )
        keep = ["time", "open", "high", "low", "close", "tick_volume", "spread_points"]
        return frame[[c for c in keep if c in frame.columns]]

    def tick(self) -> dict:
        t = self._mt5.symbol_info_tick(self.symbol)
        if t is None:
            return {}
        return {
            "symbol": self.symbol,
            "bid": float(t.bid),
            "ask": float(t.ask),
            "spread_points": round((t.ask - t.bid) / self.point(), 1),
            "time_broker": datetime.fromtimestamp(t.time, timezone.utc).isoformat(),
            "time_utc": (datetime.fromtimestamp(t.time, timezone.utc) - self._off).isoformat(),
        }

    def point(self) -> float:
        si = self._mt5.symbol_info(self.symbol)
        return float(si.point) if si else 0.01

    def shutdown(self) -> None:
        if self._mt5 is not None and self._connected:
            self._mt5.shutdown()
            self._connected = False
