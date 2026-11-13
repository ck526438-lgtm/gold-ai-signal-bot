"""Faux module MetaTrader5 pour tester le robot sans terminal (reproduit l'API utilisée)."""
from __future__ import annotations

import time
from types import SimpleNamespace as NS


class FakeMT5:
    TIMEFRAME_M5 = 5
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2

    def __init__(self, demo=True, balance=1000.0, bid=1080, spread=0.08,
                 filling_mode=2, algo=True, stops_level=0, reject=None,
                 symbols=("XAUUSD", "XAUUSDx", "XAUEUR"), server_offset=2, fail_init=False):
        self.account = NS(login=123456, server="Broker-Demo", name="Test", currency="USD",
                          balance=balance, equity=balance, margin_free=balance, leverage=100,
                          trade_mode=0 if demo else 2, trade_allowed=True, trade_expert=True)
        self.terminal = NS(trade_allowed=algo, connected=True,
                           path="C:\\Program Files\\Broker MetaTrader 5")
        self.symbols = list(symbols)
        self.server_offset = server_offset
        self.fail_init = fail_init
        self.init_calls: list[dict] = []
        self.bid, self.spread = bid, spread
        self.filling_mode, self.stops_level = filling_mode, stops_level
        self.reject = reject          # retcode à renvoyer pour order_send
        self.positions: dict[int, NS] = {}
        self.deals: list[NS] = []
        self.requests: list[dict] = []
        self._ticket = 5000
        self.initialized = False

    # --- connexion
    def initialize(self, **kw):
        self.init_calls.append(kw)
        if self.fail_init:
            return False
        self.initialized = True
        return True

    def shutdown(self):
        self.initialized = False

    def last_error(self):
        return (0, "ok")

    def terminal_info(self):
        return self.terminal if self.initialized else None

    def account_info(self):
        return self.account

    # --- marché
    def symbol_select(self, s, v):
        return True

    def symbols_get(self, group="*"):
        key = group.strip("*").upper()
        return tuple(NS(name=n) for n in self.symbols if key in n.upper())

    def symbol_info(self, s):
        if s not in self.symbols:
            return None
        return NS(name=s, digits=5, point=0.01, volume_min=0.01, volume_max=100.0, volume_step=0.01,
                  trade_stops_level=self.stops_level, filling_mode=self.filling_mode, trade_mode=4)

    def symbol_info_tick(self, s):
        return NS(bid=self.bid, ask=round(self.bid + self.spread, 2),
                  time=int(time.time()) + self.server_offset * 3600)

    def order_calc_profit(self, kind, s, vol, p1, p2):
        return (p2 - p1) * (1 if kind == 0 else -1) * vol * 100

    # --- ordres
    def order_check(self, req):
        return NS(retcode=0, comment="Done")

    def order_send(self, req):
        self.requests.append(req)
        if self.reject:
            return NS(retcode=self.reject, comment="rejected", order=0, deal=0, price=0)
        if req["action"] == self.TRADE_ACTION_SLTP:
            p = self.positions[req["position"]]
            p.sl, p.tp = req["sl"], req["tp"]
            return NS(retcode=10009, comment="Done", order=0, deal=0, price=0)
        if "position" in req:  # clôture
            p = self.positions.pop(req["position"])
            profit = (req["price"] - p.price_open) * (1 if p.type == 0 else -1) * p.volume * 100
            self.deals.append(NS(magic=p.magic, time=int(time.time()), profit=profit, commission=0, swap=0,
                                 fee=0, position_id=p.ticket))
            return NS(retcode=10009, comment="Done", order=p.ticket, deal=1, price=req["price"])
        self._ticket += 1
        self.positions[self._ticket] = NS(ticket=self._ticket, type=req["type"], volume=req["volume"],
                                          price_open=req["price"], sl=req["sl"], tp=req["tp"], profit=0.0,
                                          swap=0.0, time=int(time.time()), magic=req["magic"],
                                          comment=req["comment"], price_current=req["price"])
        return NS(retcode=10009, comment="Done", order=self._ticket, deal=1, price=req["price"])

    def positions_get(self, symbol=None):
        return tuple(self.positions.values())

    def history_deals_get(self, *args, position=None):
        if position is not None:
            return tuple(d for d in self.deals if d.position_id == position)
        return tuple(self.deals)

    # --- outils de test
    def hit(self, ticket, price):
        """Simule l'atteinte du SL/TP d'une position par le courtier."""
        p = self.positions.pop(ticket)
        profit = (price - p.price_open) * (1 if p.type == 0 else -1) * p.volume * 100
        self.deals.append(NS(magic=p.magic, time=int(time.time()), profit=profit, commission=0, swap=0,
                             fee=0, position_id=ticket))
