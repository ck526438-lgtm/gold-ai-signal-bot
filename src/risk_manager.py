"""Gestion du risque : taille de position, SL/TP sur ATR, trailing, kill-switch.

Interdits par construction : martingale, grille sans stop, risque > 2 % par trade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Optional

from loguru import logger

from .strategy import Signal


@dataclass
class TradePlan:
    direction: int
    volume: float
    entry: float
    sl: float
    tp: float
    sl_distance: float
    risk_amount: float
    reason: str
    confidence: Optional[float] = None


def floor_step(x: float, step: float) -> float:
    return math.floor(x / step + 1e-9) * step


class RiskManager:
    def __init__(self, cfg: dict, notifier=None, verbose: bool = True):
        r = cfg["risk"]
        self.verbose = verbose
        self.risk_pct = float(r["risk_per_trade_pct"])
        self.max_risk_pct = min(float(r.get("max_risk_per_trade_pct", 2.0)), 2.0)
        if self.risk_pct > self.max_risk_pct:
            raise ValueError("Risque par trade supérieur au plafond autorisé (2 %).")
        self.sl_mult = float(r["sl_atr_mult"])
        self.rr = float(r["rr_ratio"])
        tr = r.get("trailing", {}) or {}
        self.trailing_enabled = bool(tr.get("enabled", False))
        self.trailing_activate_r = float(tr.get("activate_at_r", 1.0))
        self.trailing_atr_mult = float(tr.get("distance_atr_mult", self.sl_mult))
        ks = r["killswitch"]
        self.max_daily_dd = float(ks["max_daily_drawdown_pct"])
        self.max_consec = int(ks["max_consecutive_losses"])
        self.notifier = notifier

        self.day: Optional[date] = None
        self.day_start_equity: Optional[float] = None
        self.day_peak_equity: Optional[float] = None
        self.consecutive_losses = 0
        self.locked = False
        self.lock_reason = ""
        self.last_equity: Optional[float] = None

    # ------------------------------------------------------------------ état jour
    def new_day_if_needed(self, today: date, equity: float) -> None:
        if self.day != today:
            if self.locked and self.verbose:
                logger.info(f"Nouveau jour {today} : kill-switch réarmé")
            self.day = today
            self.day_start_equity = equity
            self.day_peak_equity = equity
            self.consecutive_losses = 0
            self.locked = False
            self.lock_reason = ""

    def daily_drawdown_pct(self, equity: float) -> float:
        ref = self.day_start_equity or equity
        return max(0.0, (ref - equity) / ref * 100.0) if ref else 0.0

    def update_equity(self, equity: float, today: date) -> None:
        self.new_day_if_needed(today, equity)
        self.last_equity = equity
        self.day_peak_equity = max(self.day_peak_equity or equity, equity)
        dd = self.daily_drawdown_pct(equity)
        if not self.locked and dd >= self.max_daily_dd:
            self._trigger(f"drawdown journalier {dd:.2f}% >= {self.max_daily_dd}%", equity)

    def register_trade_result(self, pnl: float, equity: Optional[float] = None) -> None:
        if pnl < 0:
            self.consecutive_losses += 1
        elif pnl > 0:
            self.consecutive_losses = 0
        if not self.locked and self.consecutive_losses >= self.max_consec:
            self._trigger(f"{self.consecutive_losses} pertes consécutives", equity or self.last_equity)

    def _trigger(self, reason: str, equity: Optional[float]) -> None:
        self.locked = True
        self.lock_reason = reason
        if self.verbose:
            logger.warning(f"KILL-SWITCH déclenché : {reason}")
        if self.notifier:
            self.notifier.alert_killswitch(
                reason,
                equity=equity,
                daily_dd=self.daily_drawdown_pct(equity) if equity else None,
            )

    def manual_unlock(self) -> None:
        self.locked = False
        self.lock_reason = ""
        self.consecutive_losses = 0

    def can_trade(self) -> tuple[bool, str]:
        return (not self.locked, self.lock_reason or "ok")

    # ------------------------------------------------------------------ sizing
    def compute_volume(
        self,
        equity: float,
        sl_distance: float,
        contract_size: float,
        volume_min: float,
        volume_step: float,
        volume_max: float,
        commission_per_lot: float = 0.0,
    ) -> float:
        """Lots tels que perte au SL (+ commission) <= risque. 0 si impossible sans dépasser."""
        if sl_distance <= 0 or equity <= 0:
            return 0.0
        risk_amount = equity * self.risk_pct / 100.0
        loss_per_lot = sl_distance * contract_size + commission_per_lot
        vol = floor_step(risk_amount / loss_per_lot, volume_step)
        vol = min(vol, volume_max)
        if vol < volume_min:
            # Le lot minimum ferait dépasser le risque : on n'augmente JAMAIS la taille.
            return 0.0
        # Garde-fou absolu : jamais plus de 2 % du capital
        if vol * loss_per_lot > equity * self.max_risk_pct / 100.0:
            return 0.0
        return round(vol, 8)

    def plan_trade(
        self,
        signal: Signal,
        equity: float,
        entry_price: float,
        contract_size: float,
        volume_min: float,
        volume_step: float,
        volume_max: float,
        commission_per_lot: float = 0.0,
        digits: int = 2,
    ) -> Optional[TradePlan]:
        sl_dist = self.sl_mult * signal.atr  # toujours basé sur l'ATR, jamais en points fixes
        if not math.isfinite(sl_dist) or sl_dist <= 0:
            return None
        vol = self.compute_volume(
            equity, sl_dist, contract_size, volume_min, volume_step, volume_max, commission_per_lot
        )
        if vol <= 0:
            logger.info("Trade ignoré : taille minimale > risque autorisé")
            return None
        d = signal.direction
        sl = round(entry_price - d * sl_dist, digits)
        tp = round(entry_price + d * sl_dist * self.rr, digits)
        return TradePlan(
            direction=d, volume=vol, entry=entry_price, sl=sl, tp=tp, sl_distance=sl_dist,
            risk_amount=vol * (sl_dist * contract_size + commission_per_lot),
            reason=signal.reason, confidence=signal.confidence,
        )

    # ------------------------------------------------------------------ trailing
    def trailing_sl(
        self, direction: int, entry: float, current_sl: float, initial_sl_distance: float,
        price: float, atr_value: float,
    ) -> Optional[float]:
        """Nouveau SL si le trailing doit bouger (jamais desserré), sinon None."""
        if not self.trailing_enabled or initial_sl_distance <= 0:
            return None
        r_mult = (price - entry) * direction / initial_sl_distance
        if r_mult < self.trailing_activate_r:
            return None
        dist = self.trailing_atr_mult * atr_value
        candidate = price - direction * dist
        if direction > 0 and candidate > current_sl:
            return candidate
        if direction < 0 and candidate < current_sl:
            return candidate
        return None
