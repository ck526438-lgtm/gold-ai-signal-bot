"""Courtiers : MetaTrader 5 (réel ou démo) et simulateur (paper trading).

Les deux exposent la même interface, utilisée par le robot (executor.py).
"""
from __future__ import annotations

import itertools
import math
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

RETCODES = {
    10004: "Requote : le prix a changé",
    10006: "Ordre rejeté par le courtier",
    10007: "Ordre annulé",
    10013: "Requête invalide",
    10014: "Volume invalide",
    10015: "Prix invalide",
    10016: "Stop ou TP invalide (trop proche du prix ?)",
    10017: "Trading désactivé pour ce compte",
    10018: "Marché fermé",
    10019: "Fonds insuffisants",
    10020: "Prix modifiés",
    10021: "Pas de cotation pour traiter la requête",
    10024: "Trop de requêtes",
    10026: "Trading automatique désactivé par le serveur",
    10027: "Trading algorithmique désactivé dans le terminal (bouton « Algo Trading »)",
    10030: "Mode de remplissage non supporté",
    10031: "Pas de connexion au serveur de trading",
    10033: "Limite d'ordres atteinte",
    10034: "Limite de volume atteinte",
}
DONE = (10008, 10009, 10010)


@dataclass
class AccountInfo:
    login: int
    server: str
    name: str
    currency: str
    balance: float
    equity: float
    margin_free: float
    leverage: int
    is_demo: bool
    trade_allowed: bool          # compte ET terminal autorisent le trading auto
    trade_allowed_reason: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = "DÉMO" if self.is_demo else "RÉEL"
        return d


@dataclass
class SymbolSpec:
    name: str
    digits: int
    point: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int = 0
    tradable: bool = True


@dataclass
class Tick:
    bid: float
    ask: float


@dataclass
class Position:
    ticket: int
    side: int                    # +1 achat, -1 vente
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float
    time_open: datetime          # UTC
    comment: str = ""
    magic: int = 0
    price_current: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(time_open=self.time_open.isoformat(), side_label="ACHAT" if self.side == 1 else "VENTE")
        return d


@dataclass
class OrderResult:
    ok: bool
    message: str = ""
    ticket: int | None = None
    price: float | None = None
    retcode: int | None = None


def floor_step(volume: float, step: float) -> float:
    n = math.floor(volume / step + 1e-9)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, decimals)


# ====================================================================== MT5
class MT5Broker:
    name = "MetaTrader 5"

    def __init__(self, link, utc_offset_hours: int = 0):
        self.link = link
        self.offset = timedelta(hours=utc_offset_hours)

    # -------------------------------------------------------------- lecture
    def account(self) -> AccountInfo:
        with self.link.lock:
            mt5 = self.link.ensure()
            a, t = mt5.account_info(), mt5.terminal_info()
            if a is None:
                raise RuntimeError(f"Compte MT5 indisponible : {mt5.last_error()}")
        reason = ""
        if t is not None and not t.trade_allowed:
            reason = "Bouton « Algo Trading » désactivé dans MetaTrader 5"
        elif not a.trade_allowed:
            reason = "Trading désactivé sur ce compte"
        elif not getattr(a, "trade_expert", True):
            reason = "Trading automatique interdit sur ce compte"
        return AccountInfo(a.login, a.server, a.name, a.currency, a.balance, a.equity,
                           a.margin_free, a.leverage, a.trade_mode == 0, not reason, reason)

    def symbol(self, symbol: str) -> SymbolSpec:
        with self.link.lock:
            mt5 = self.link.ensure()
            mt5.symbol_select(symbol, True)
            s = mt5.symbol_info(symbol)
            if s is None:
                raise RuntimeError(f"Symbole {symbol} introuvable dans MT5")
        return SymbolSpec(s.name, s.digits, s.point, s.volume_min, s.volume_max, s.volume_step,
                          s.trade_stops_level, getattr(s, "trade_mode", 4) != 0)

    def tick(self, symbol: str) -> Tick:
        with self.link.lock:
            mt5 = self.link.ensure()
            t = mt5.symbol_info_tick(symbol)
            if t is None:
                raise RuntimeError(f"Pas de cotation pour {symbol}")
        return Tick(t.bid, t.ask)

    def loss_per_lot(self, symbol: str, side: int, price: float, sl: float) -> float:
        with self.link.lock:
            mt5 = self.link.ensure()
            kind = mt5.ORDER_TYPE_BUY if side == 1 else mt5.ORDER_TYPE_SELL
            p = mt5.order_calc_profit(kind, symbol, 1.0, price, sl)
        if p is None:
            raise RuntimeError("Impossible de calculer la valeur du pip (order_calc_profit)")
        return abs(p)

    def positions(self, symbol: str, magic: int) -> list[Position]:
        with self.link.lock:
            mt5 = self.link.ensure()
            raw = mt5.positions_get(symbol=symbol) or ()
        out = []
        for p in raw:
            if p.magic != magic:
                continue
            out.append(Position(p.ticket, 1 if p.type == 0 else -1, p.volume, p.price_open, p.sl, p.tp,
                                p.profit + getattr(p, "swap", 0.0),
                                datetime.fromtimestamp(p.time, UTC) - self.offset,
                                p.comment, p.magic, getattr(p, "price_current", 0.0)))
        return out

    def realized_since(self, since: datetime, magic: int) -> float:
        with self.link.lock:
            mt5 = self.link.ensure()
            now = datetime.now(UTC)
            deals = mt5.history_deals_get(since - timedelta(days=2), now + timedelta(days=2)) or ()
        total = 0.0
        for d in deals:
            if d.magic != magic:
                continue
            if datetime.fromtimestamp(d.time, UTC) - self.offset < since:
                continue
            total += d.profit + getattr(d, "commission", 0.0) + getattr(d, "swap", 0.0) + getattr(d, "fee", 0.0)
        return total

    def closed_profit(self, ticket: int) -> float | None:
        with self.link.lock:
            mt5 = self.link.ensure()
            deals = mt5.history_deals_get(position=ticket) or ()
        if not deals:
            return None
        return sum(d.profit + getattr(d, "commission", 0.0) + getattr(d, "swap", 0.0) for d in deals)

    # -------------------------------------------------------------- ordres
    def _filling(self, mt5, symbol: str) -> int:
        info = mt5.symbol_info(symbol)
        mode = getattr(info, "filling_mode", 0) if info else 0
        if mode & 1:
            return mt5.ORDER_FILLING_FOK
        if mode & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _send(self, mt5, request: dict) -> OrderResult:
        check = mt5.order_check(request)
        if check is None:
            return OrderResult(False, f"Vérification impossible : {mt5.last_error()}")
        if check.retcode not in (0, *DONE):
            return OrderResult(False, RETCODES.get(check.retcode, check.comment or "Ordre refusé"),
                               retcode=check.retcode)
        res = mt5.order_send(request)
        if res is None:
            return OrderResult(False, f"Envoi impossible : {mt5.last_error()}")
        if res.retcode not in DONE:
            return OrderResult(False, RETCODES.get(res.retcode, res.comment or "Ordre refusé"),
                               retcode=res.retcode)
        return OrderResult(True, "OK", res.order or getattr(res, "deal", None), res.price, res.retcode)

    def open(self, symbol: str, side: int, volume: float, sl: float, tp: float,
             comment: str, magic: int, deviation: int) -> OrderResult:
        with self.link.lock:
            mt5 = self.link.ensure()
            t = mt5.symbol_info_tick(symbol)
            req = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
                "type": mt5.ORDER_TYPE_BUY if side == 1 else mt5.ORDER_TYPE_SELL,
                "price": t.ask if side == 1 else t.bid, "sl": float(sl), "tp": float(tp),
                "deviation": int(deviation), "magic": int(magic), "comment": comment[:31],
                "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(mt5, symbol),
            }
            return self._send(mt5, req)

    def modify(self, pos: Position, symbol: str, sl: float, tp: float) -> OrderResult:
        with self.link.lock:
            mt5 = self.link.ensure()
            req = {"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "symbol": symbol,
                   "sl": float(sl), "tp": float(tp), "magic": pos.magic}
            return self._send(mt5, req)

    def close(self, pos: Position, symbol: str, magic: int, deviation: int,
              comment: str = "fermeture") -> OrderResult:
        with self.link.lock:
            mt5 = self.link.ensure()
            t = mt5.symbol_info_tick(symbol)
            req = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(pos.volume),
                "type": mt5.ORDER_TYPE_SELL if pos.side == 1 else mt5.ORDER_TYPE_BUY,
                "position": pos.ticket, "price": t.bid if pos.side == 1 else t.ask,
                "deviation": int(deviation), "magic": int(magic), "comment": comment[:31],
                "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(mt5, symbol),
            }
            return self._send(mt5, req)


# ====================================================================== simulateur
@dataclass
class _PaperPos:
    pos: Position
    closed: bool = False
    exit_price: float | None = None


class PaperBroker:
    """Courtier simulé : aucun argent réel, exécution au dernier prix connu.

    Les prix sont fournis à chaque bougie par `update_market` ; SL/TP sont vérifiés sur
    le plus haut / plus bas de la bougie (stop en premier, hypothèse prudente).
    """
    name = "Simulateur"
    def __init__(self, balance: float = 10_000.0, spread_pips: float = 3.3, pip: float = 0.1):
        # or (XAUUSD) : 1 lot = 100 onces, cotation à 0,01 ; devises : 100 000 unités, 0,00001
        self.gold = pip >= 0.01
        self.CONTRACT = 100 if self.gold else 100_000
        self.digits = 2 if self.gold else 5
        self.start_balance = balance
        self.balance = balance
        self.spread = spread_pips * pip
        self.last = None
        self._pos: dict[int, _PaperPos] = {}
        self._ticket = itertools.count(1000)
        self._realized: list[tuple[datetime, float, int]] = []
        self._lock = threading.RLock()

    def update_market(self, close: float, high: float | None = None, low: float | None = None) -> None:
        with self._lock:
            self.last = close
            hi, lo = high if high is not None else close, low if low is not None else close
            for pp in self._pos.values():
                if pp.closed:
                    continue
                p = pp.pos
                hit_sl = (lo <= p.sl) if p.side == 1 else (hi >= p.sl)
                hit_tp = p.tp and ((hi >= p.tp) if p.side == 1 else (lo <= p.tp))
                if hit_sl:
                    self._settle(pp, p.sl)
                elif hit_tp:
                    self._settle(pp, p.tp)
                else:
                    p.price_current = close
                    p.profit = round((close - p.price_open) * p.side * p.volume * self.CONTRACT, 2)

    def _settle(self, pp: _PaperPos, price: float) -> None:
        p = pp.pos
        pnl = round((price - p.price_open) * p.side * p.volume * self.CONTRACT, 2)
        pp.closed, pp.exit_price = True, price
        self.balance += pnl
        self._realized.append((datetime.now(UTC), pnl, p.ticket))

    def _require_price(self) -> float:
        if self.last is None:
            raise RuntimeError("Simulateur : aucun prix reçu pour l'instant")
        return self.last

    def account(self) -> AccountInfo:
        with self._lock:
            floating = sum(pp.pos.profit for pp in self._pos.values() if not pp.closed)
            return AccountInfo(0, "Simulateur", "Paper trading", "USD", round(self.balance, 2),
                               round(self.balance + floating, 2), round(self.balance, 2), 100, True, True)

    def symbol(self, symbol: str) -> SymbolSpec:
        return SymbolSpec(symbol, self.digits, 10 ** -self.digits, 0.01, 50.0, 0.01, 0)

    def tick(self, symbol: str) -> Tick:
        bid = self._require_price()
        return Tick(bid, round(bid + self.spread, self.digits))

    def loss_per_lot(self, symbol: str, side: int, price: float, sl: float) -> float:
        return abs(price - sl) * self.CONTRACT

    def positions(self, symbol: str, magic: int) -> list[Position]:
        with self._lock:
            return [pp.pos for pp in self._pos.values() if not pp.closed and pp.pos.magic == magic]

    def realized_since(self, since: datetime, magic: int) -> float:
        with self._lock:
            return sum(pnl for t, pnl, _ in self._realized if t >= since)

    def closed_profit(self, ticket: int) -> float | None:
        with self._lock:
            vals = [pnl for _, pnl, tk in self._realized if tk == ticket]
            return sum(vals) if vals else None

    def open(self, symbol, side, volume, sl, tp, comment, magic, deviation) -> OrderResult:
        with self._lock:
            t = self.tick(symbol)
            price = t.ask if side == 1 else t.bid
            if (side == 1 and not sl < price < tp) or (side == -1 and not tp < price < sl):
                return OrderResult(False, RETCODES[10016], retcode=10016)
            ticket = next(self._ticket)
            self._pos[ticket] = _PaperPos(Position(ticket, side, volume, price, sl, tp, 0.0,
                                                   datetime.now(UTC), comment, magic, price))
            return OrderResult(True, "OK", ticket, price, 10009)

    def modify(self, pos: Position, symbol: str, sl: float, tp: float) -> OrderResult:
        with self._lock:
            pp = self._pos.get(pos.ticket)
            if pp is None or pp.closed:
                return OrderResult(False, "Position introuvable")
            pp.pos.sl, pp.pos.tp = sl, tp
            return OrderResult(True, "OK", pos.ticket)

    def close(self, pos: Position, symbol: str, magic: int, deviation: int, comment: str = "") -> OrderResult:
        with self._lock:
            pp = self._pos.get(pos.ticket)
            if pp is None or pp.closed:
                return OrderResult(False, "Position introuvable")
            t = self.tick(symbol)
            price = t.bid if pos.side == 1 else t.ask
            self._settle(pp, price)
            return OrderResult(True, "OK", pos.ticket, price, 10009)

