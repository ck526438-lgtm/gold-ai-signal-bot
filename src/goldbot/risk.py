"""SL/TP et dimensionnement, en ATR.

Sur l'or M5, un SL fixe en points est une erreur: l'ATR varie du simple au
triple selon la session. Les distances suivent donc la volatilite de la bougie
de signal, et le volume est calcule sur le risque en dollars.
"""
from __future__ import annotations

from math import floor

from .config import get


def round_turn_cost(cfg: dict, spread_points: float | None = None) -> float:
    """Cout aller-retour en prix (USD sur XAUUSD). Source unique: backtest et
    paper doivent payer la meme facture, sinon les deux bilans sont incomparables.

    Les commissions par lot ne rentrent pas dans cette formule (unites
    monetaires, pas des points de prix): les reperter dans slippage_points si
    ton broker en facture.
    """
    point = float(get(cfg, "broker.point", 0.01))
    sp = float(get(cfg, "backtest.spread_points", 33)) if spread_points is None else float(spread_points)
    slip = float(get(cfg, "backtest.slippage_points", 5))
    return sp * point + 2 * slip * point


def bracket(entry: float, side: str, atr: float, cfg: dict) -> tuple[float, float, float]:
    sl_mult = float(get(cfg, "risk.sl_atr", 1.5))
    tp_mult = float(get(cfg, "risk.tp_atr", 2.5))
    if side == "FLAT" or atr is None or atr <= 0:
        return entry, entry, 0.0
    direction = 1.0 if side == "BUY" else -1.0
    sl = entry - direction * sl_mult * atr
    tp = entry + direction * tp_mult * atr
    return round(sl, 3), round(tp, 3), round(tp_mult / sl_mult, 2)


def position_size(balance: float, sl_distance: float, cfg: dict) -> dict:
    """Volume en lots pour risquer risk_pct% de la balance."""
    contract = float(get(cfg, "broker.contract_size", 100))
    lot_min = float(get(cfg, "broker.lot_min", 0.01))
    lot_step = float(get(cfg, "broker.lot_step", 0.01))
    risk_pct = float(get(cfg, "risk.risk_pct", 0.5))
    max_volume = float(get(cfg, "broker.lot_max", 100))

    risk_usd = max(0.0, float(balance)) * risk_pct / 100.0
    per_lot = max(1e-9, sl_distance * contract)
    raw = risk_usd / per_lot
    lots = floor(raw / lot_step) * lot_step if raw >= lot_min else 0.0
    lots = min(round(lots, 2), max_volume)
    return {
        "risk_usd": round(risk_usd, 2),
        "sl_distance": round(sl_distance, 3),
        "volume": lots,
        "actual_risk_usd": round(lots * per_lot, 2),
        "note": "volume 0 = SL trop large pour le risque autorise" if lots == 0 else "",
    }
