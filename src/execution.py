"""Exécution des ordres MT5 : order_send, retcode, retry, contrôle du slippage, journal."""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from .config import resolve
from .risk_manager import RiskManager, TradePlan

RET_DONE = 10009
RET_PLACED = 10008
RET_DONE_PARTIAL = 10010
TRANSIENT_RETCODES = {10004, 10020, 10021, 10012, 10031, 10024}  # requote, price changed/off, timeout, connexion, trop de requêtes

TRADE_FIELDS = [
    "ticket", "symbol", "side", "volume", "open_time", "close_time", "duration_min",
    "requested_price", "entry_price", "exit_price", "sl", "tp", "pnl", "r_multiple", "reason",
]


@dataclass
class OpenTrade:
    ticket: int
    direction: int
    volume: float
    entry: float
    requested: float
    sl: float
    tp: float
    sl_distance: float
    risk_amount: float
    open_time: datetime
    reason: str
    extra: dict = field(default_factory=dict)


def append_trade_csv(path: str | Path, row: dict) -> None:
    p = resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TRADE_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in TRADE_FIELDS})


class Executor:
    def __init__(self, connector, cfg: dict, risk: RiskManager, notifier=None):
        self.conn = connector
        self.cfg = cfg
        self.ex = cfg["execution"]
        self.risk = risk
        self.notifier = notifier
        self.symbol = cfg["symbol"]
        self.trades_csv = cfg["logging"].get("trades_csv", "logs/trades.csv")
        self.open_trades: dict[int, OpenTrade] = {}

    @property
    def mt5(self):
        return self.conn.mt5

    # ------------------------------------------------------------------ helpers
    def _filling_mode(self, info) -> int:
        fm = getattr(info, "filling_mode", 0) or 0
        if fm & 1:
            return getattr(self.mt5, "ORDER_FILLING_FOK", 0)
        if fm & 2:
            return getattr(self.mt5, "ORDER_FILLING_IOC", 1)
        return getattr(self.mt5, "ORDER_FILLING_RETURN", 2)

    def _send_with_retry(self, request: dict, label: str):
        retries = int(self.ex.get("max_retries", 3))
        delay = float(self.ex.get("retry_delay_s", 0.5))
        result = None
        for attempt in range(1, retries + 1):
            result = self.mt5.order_send(request)
            code = getattr(result, "retcode", None)
            if code in (RET_DONE, RET_PLACED, RET_DONE_PARTIAL):
                return result
            comment = getattr(result, "comment", "") if result else str(self.mt5.last_error())
            if code in TRANSIENT_RETCODES and attempt < retries:
                logger.warning(f"{label}: retcode {code} ({comment}) — nouvel essai {attempt}/{retries}")
                time.sleep(delay * attempt)
                tick = self.conn.tick()
                if tick is not None and "price" in request and request.get("action") == getattr(self.mt5, "TRADE_ACTION_DEAL", 1):
                    is_buy = request["type"] == getattr(self.mt5, "ORDER_TYPE_BUY", 0)
                    request["price"] = tick.ask if is_buy else tick.bid
                continue
            logger.error(f"{label}: échec retcode={code} ({comment})")
            if self.notifier:
                self.notifier.alert_error(f"{label} : retcode {code} — {comment}")
            return None
        return None

    # ------------------------------------------------------------------ entrée
    def open_position(self, plan: TradePlan, expected_price: float) -> Optional[OpenTrade]:
        info, tick = self.conn.symbol_info(), self.conn.tick()
        if info is None or tick is None:
            logger.error("symbol_info/tick indisponible — ordre annulé")
            return None
        is_buy = plan.direction > 0
        price = tick.ask if is_buy else tick.bid
        est_slip_pts = abs(price - expected_price) / info.point
        max_slip = float(self.ex["max_slippage_points"])
        if est_slip_pts > max_slip:
            logger.warning(f"Ordre refusé : slippage estimé {est_slip_pts:.0f} pts > {max_slip:.0f}")
            return None
        # Recalage SL/TP sur le prix réel d'exécution (même distance ATR)
        d = plan.direction
        sl = round(price - d * plan.sl_distance, info.digits)
        tp = round(price + d * plan.sl_distance * self.risk.rr, info.digits)
        request = {
            "action": getattr(self.mt5, "TRADE_ACTION_DEAL", 1),
            "symbol": self.symbol,
            "volume": float(plan.volume),
            "type": getattr(self.mt5, "ORDER_TYPE_BUY", 0) if is_buy else getattr(self.mt5, "ORDER_TYPE_SELL", 1),
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": int(self.ex.get("deviation_points", 20)),
            "magic": int(self.ex["magic"]),
            "comment": str(self.ex.get("comment", "xauusd_bot"))[:31],
            "type_time": getattr(self.mt5, "ORDER_TIME_GTC", 0),
            "type_filling": self._filling_mode(info),
        }
        result = self._send_with_retry(request, "Ouverture")
        if result is None:
            return None
        fill = float(getattr(result, "price", 0) or price)
        ticket = int(getattr(result, "order", 0) or getattr(result, "deal", 0))
        positions = self.conn.positions()
        for p in positions:  # le ticket de position peut différer du ticket d'ordre
            if getattr(p, "identifier", None) == ticket or getattr(p, "ticket", None) == ticket:
                ticket = int(p.ticket)
                break
        real_slip = abs(fill - request["price"]) / info.point
        logger.info(
            f"ORDRE {'BUY' if is_buy else 'SELL'} ticket={ticket} vol={plan.volume} "
            f"demandé={request['price']} obtenu={fill} slippage={real_slip:.0f}pts SL={sl} TP={tp} | {plan.reason}"
        )
        trade = OpenTrade(
            ticket=ticket, direction=d, volume=plan.volume, entry=fill, requested=request["price"],
            sl=sl, tp=tp, sl_distance=plan.sl_distance, risk_amount=plan.risk_amount,
            open_time=datetime.now(timezone.utc), reason=plan.reason,
        )
        self.open_trades[ticket] = trade
        if self.notifier:
            self.notifier.notify_entry(
                symbol=self.symbol, side="BUY" if is_buy else "SELL", price=fill, sl=sl, tp=tp,
                volume=plan.volume, reason=plan.reason, confidence=plan.confidence,
            )
        return trade

    # ------------------------------------------------------------------ suivi
    def _closed_pnl(self, ticket: int) -> tuple[float, Optional[float], Optional[datetime]]:
        deals = self.mt5.history_deals_get(position=ticket) or []
        pnl, exit_price, close_time = 0.0, None, None
        for dl in deals:
            pnl += float(getattr(dl, "profit", 0) + getattr(dl, "commission", 0) + getattr(dl, "swap", 0) + getattr(dl, "fee", 0))
            if getattr(dl, "entry", 0) in (1, 3):  # DEAL_ENTRY_OUT / OUT_BY
                exit_price = float(dl.price)
                close_time = datetime.fromtimestamp(int(dl.time), tz=timezone.utc)
        return pnl, exit_price, close_time

    def sync_positions(self) -> list[dict]:
        """Détecte les positions clôturées (SL/TP/manuel) → journal, risk manager, notification."""
        live = {int(p.ticket) for p in self.conn.positions()}
        closed = []
        for ticket in list(self.open_trades):
            if ticket in live:
                continue
            t = self.open_trades.pop(ticket)
            pnl, exit_price, close_time = self._closed_pnl(ticket)
            close_time = close_time or datetime.now(timezone.utc)
            duration = (close_time - t.open_time).total_seconds() / 60.0
            r_mult = pnl / t.risk_amount if t.risk_amount else 0.0
            row = {
                "ticket": ticket, "symbol": self.symbol, "side": "BUY" if t.direction > 0 else "SELL",
                "volume": t.volume, "open_time": t.open_time.isoformat(), "close_time": close_time.isoformat(),
                "duration_min": round(duration, 1), "requested_price": t.requested, "entry_price": t.entry,
                "exit_price": exit_price, "sl": t.sl, "tp": t.tp, "pnl": round(pnl, 2),
                "r_multiple": round(r_mult, 2), "reason": t.reason,
            }
            append_trade_csv(self.trades_csv, row)
            logger.info(f"CLÔTURE ticket={ticket} PnL={pnl:.2f}$ ({r_mult:+.2f}R) durée={duration:.0f} min")
            acc = self.conn.account_info()
            self.risk.register_trade_result(pnl, getattr(acc, "equity", None))
            if self.notifier:
                self.notifier.notify_exit(
                    symbol=self.symbol, side=row["side"], pnl=pnl, r_multiple=r_mult, duration_min=duration,
                )
            closed.append(row)
        # adopter les positions du bot inconnues (redémarrage)
        for p in self.conn.positions():
            if int(p.ticket) not in self.open_trades:
                d = 1 if p.type == getattr(self.mt5, "POSITION_TYPE_BUY", 0) else -1
                dist = abs(p.price_open - p.sl) if p.sl else 0.0
                info = self.conn.symbol_info()
                cs = getattr(info, "trade_contract_size", 100.0) if info else 100.0
                self.open_trades[int(p.ticket)] = OpenTrade(
                    ticket=int(p.ticket), direction=d, volume=p.volume, entry=p.price_open,
                    requested=p.price_open, sl=p.sl, tp=p.tp, sl_distance=dist,
                    risk_amount=dist * cs * p.volume, open_time=datetime.fromtimestamp(int(p.time), tz=timezone.utc),
                    reason="position reprise au démarrage",
                )
        return closed

    def modify_sl(self, ticket: int, new_sl: float) -> bool:
        t = self.open_trades.get(ticket)
        if t is None:
            return False
        info = self.conn.symbol_info()
        new_sl = round(new_sl, getattr(info, "digits", 2))
        req = {
            "action": getattr(self.mt5, "TRADE_ACTION_SLTP", 6),
            "symbol": self.symbol, "position": ticket, "sl": new_sl, "tp": t.tp,
            "magic": int(self.ex["magic"]),
        }
        res = self._send_with_retry(req, f"Trailing SL #{ticket}")
        if res is not None:
            logger.info(f"Trailing : SL #{ticket} {t.sl} -> {new_sl}")
            t.sl = new_sl
            return True
        return False

    def manage_trailing(self, atr_value: float) -> None:
        tick = self.conn.tick()
        if tick is None:
            return
        for ticket, t in list(self.open_trades.items()):
            price = tick.bid if t.direction > 0 else tick.ask
            new_sl = self.risk.trailing_sl(t.direction, t.entry, t.sl, t.sl_distance, price, atr_value)
            if new_sl is not None:
                self.modify_sl(ticket, new_sl)

    def close_all(self) -> int:
        """Ferme toutes les positions du bot (uniquement sur /stop confirm close)."""
        n = 0
        for p in self.conn.positions():
            tick = self.conn.tick()
            is_buy = p.type == getattr(self.mt5, "POSITION_TYPE_BUY", 0)
            req = {
                "action": getattr(self.mt5, "TRADE_ACTION_DEAL", 1),
                "symbol": self.symbol, "volume": float(p.volume), "position": int(p.ticket),
                "type": getattr(self.mt5, "ORDER_TYPE_SELL", 1) if is_buy else getattr(self.mt5, "ORDER_TYPE_BUY", 0),
                "price": tick.bid if is_buy else tick.ask,
                "deviation": int(self.ex.get("deviation_points", 20)),
                "magic": int(self.ex["magic"]), "comment": "close_all",
                "type_filling": self._filling_mode(self.conn.symbol_info()),
            }
            if self._send_with_retry(req, f"Fermeture #{p.ticket}") is not None:
                n += 1
        self.sync_positions()
        return n
