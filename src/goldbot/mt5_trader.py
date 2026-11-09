"""Execution d'ordres MT5.

Verrou: rien ne part au marche si trading.auto_execute n'est pas explicitement
a true. Un bot de signal n'est pas un robot de trade, et la validation du
backtest ci-dessus ne tient pas encore la charge de l'argent reel.
"""
from __future__ import annotations

from .config import get


def _mt5():
    import MetaTrader5 as mt5

    return mt5


def place(cfg: dict, symbol: str, side: str, volume: float, sl: float, tp: float) -> dict:
    if not bool(get(cfg, "trading.auto_execute", False)):
        return {"sent": False, "reason": "trading.auto_execute=false (normal: rester en paper)"}
    if volume <= 0:
        return {"sent": False, "reason": "volume 0: SL trop large pour risk_pct"}
    mt5 = _mt5()
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {"sent": False, "reason": f"pas de tick pour {symbol}"}
    buy = side == "BUY"
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
        "price": tick.ask if buy else tick.bid,
        "sl": float(sl),
        "tp": float(tp),
        "deviation": int(get(cfg, "trading.deviation_points", 30)),
        "magic": int(get(cfg, "trading.magic", 20261109)),
        "comment": str(get(cfg, "trading.order_comment", "GOLDAI"))[:31],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None:
        return {"sent": False, "reason": f"order_send a retourne None: {mt5.last_error()}"}
    return {"sent": result.retcode == mt5.TRADE_RETCODE_DONE, "retcode": result.retcode,
            "comment": result.comment, "deal": result.order, "price": float(result.price)}


def open_positions(cfg: dict, symbol: str) -> list[dict]:
    mt5 = _mt5()
    positions = mt5.positions_get(symbol=symbol) or []
    return [{"ticket": p.ticket, "side": "BUY" if p.type == 0 else "SELL",
             "volume": p.volume, "price": p.price_open, "sl": p.sl, "tp": p.tp} for p in positions]
