"""Moteur de décision : combine chandeliers + tendance + zones + momentum en un score /10.

`Analyzer.inspect(i)` renvoie toujours le meilleur setup de la bougie i (même rejeté,
avec la raison) — c'est ce qu'affiche le « radar ». `Analyzer.evaluate(i)` ne renvoie
que les setups validés.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from . import patterns as pat
from .structure import Zone, nearest, zones_at


@dataclass
class Factor:
    label: str
    points: float

    @property
    def tone(self) -> str:
        return "pos" if self.points > 0 else "neg" if self.points < 0 else "neutral"


@dataclass
class Signal:
    time: pd.Timestamp             # heure d'OUVERTURE de la bougie signal (UTC)
    index: int
    direction: int                 # +1 ACHAT, -1 VENTE
    entry: float
    sl: float
    tp1: float
    tp2: float
    score: float
    pattern: str
    factors: list[Factor] = field(default_factory=list)
    risk_pips: float = 0.0
    blocked: str | None = None     # raison du rejet, None si valide
    atr_pips: float = 0.0
    mtf: dict = field(default_factory=dict)   # tendances {"m15", "h1", "h4"} : +1 / 0 / −1

    @property
    def side(self) -> str:
        return "ACHAT" if self.direction == 1 else "VENTE"

    @property
    def quality(self) -> str:
        if self.score >= 8:
            return "A+"
        if self.score >= 7:
            return "A"
        if self.score >= 6:
            return "B"
        return "C"

    @property
    def close_time(self) -> pd.Timestamp:
        return self.time + pd.Timedelta(minutes=5)

    @property
    def key(self) -> str:
        return f"{self.time:%Y%m%d%H%M}{'L' if self.direction == 1 else 'S'}"

    def rr(self, target: float) -> float:
        return round(abs(target - self.entry) / max(abs(self.entry - self.sl), 1e-9), 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(time=self.time.isoformat(), close_time=self.close_time.isoformat(),
                 side=self.side, quality=self.quality, key=self.key,
                 rr1=self.rr(self.tp1), rr2=self.rr(self.tp2),
                 factors=[{"label": f.label, "points": f.points, "tone": f.tone} for f in self.factors])
        return d


def in_session(ts: pd.Timestamp, sessions: list) -> bool:
    hour = ts.hour + ts.minute / 60
    return any(a <= hour < b for a, b in sessions)


class Analyzer:
    """Pré-calcule les indicateurs une fois, puis évalue n'importe quelle bougie i."""

    def __init__(self, df: pd.DataFrame, cfg):
        from .indicators import add_indicators
        from .structure import swing_points

        self.cfg = cfg
        self.s = cfg.strategy
        self.df = add_indicators(df, self.s)
        a = self.df
        self.o, self.h, self.l, self.c = (a[x].to_numpy() for x in ("open", "high", "low", "close"))
        self.atr = a["atr"].to_numpy()
        self.rsi = a["rsi"].to_numpy()
        self.adx = a["adx"].to_numpy()
        self.ef, self.em, self.es = (a[x].to_numpy() for x in ("ema_fast", "ema_mid", "ema_slow"))
        self.bias = a["htf_bias"].to_numpy()
        self.bias15 = a["m15_bias"].to_numpy()
        self.bias4 = a["h4_bias"].to_numpy()
        self.idx = a.index
        self.sw_hi, self.sw_lo = swing_points(self.h, self.l)

    @property
    def warmup(self) -> int:
        return max(self.s.ema_slow, 60)

    def zones(self, i: int) -> list[Zone]:
        return zones_at(i, self.h, self.l, self.sw_hi, self.sw_lo, self.c[i], self.atr[i],
                        self.s.swing_lookback, self.s.zone_atr_mult)

    # ------------------------------------------------------------------ #
    def evaluate(self, i: int) -> Signal | None:
        sig = self.inspect(i)
        return sig if sig is not None and sig.blocked is None else None

    def scan(self, start: int | None = None) -> list[Signal]:
        """Tous les signaux valides de `start` à la fin, avec le délai entre signaux."""
        out: list[Signal] = []
        last = {1: -10**9, -1: -10**9}
        for i in range(max(self.warmup, start or 0), len(self.c)):
            sig = self.evaluate(i)
            if sig and i - last[sig.direction] >= self.s.cooldown_bars:
                last[sig.direction] = i
                out.append(sig)
        return out

    def candidate(self, i: int) -> Signal | None:
        """inspect(i) mis en cache (utilisé pour détecter les retournements)."""
        cache = self.__dict__.setdefault("_cand_cache", {})
        if i not in cache:
            cache[i] = self.inspect(i)
        return cache[i]

    def inspect(self, i: int) -> Signal | None:
        s, pip = self.s, self.cfg.pip
        if i < self.warmup:
            return None
        atr = self.atr[i]
        ts = self.idx[i]

        found = [p for p in pat.detect(self.o, self.h, self.l, self.c, i, atr)
                 if p.name not in s.disabled_patterns]
        if not found:
            return None
        best = max(found, key=lambda p: p.strength)
        d = best.direction
        conflict = len({p.direction for p in found}) > 1
        same = [p for p in found if p.direction == d]
        names = " + ".join(sorted({p.name for p in same}))

        F: list[Factor] = []
        strength = min(3, best.strength + (1 if len(same) > 1 else 0))
        F.append(Factor(f"Figure : {names}", strength))

        # 1) Tendance de fond H1
        if self.bias[i] == d:
            F.append(Factor("Dans le sens de la tendance H1", 2))
        elif self.bias[i] == -d:
            F.append(Factor("Contre la tendance H1", -2))
        else:
            F.append(Factor("Tendance H1 neutre", 0))

        # 2) Moyennes mobiles M5
        c = self.c[i]
        aligned = (d == 1 and c > self.em[i] and self.ef[i] > self.em[i]) or \
                  (d == -1 and c < self.em[i] and self.ef[i] < self.em[i])
        F.append(Factor("EMA 20/50 M5 alignées" if aligned else "EMA 20/50 M5 non alignées",
                        1 if aligned else 0))
        good200 = (d == 1 and c > self.es[i]) or (d == -1 and c < self.es[i])
        F.append(Factor("Prix du bon côté de l'EMA 200" if good200 else "Prix du mauvais côté de l'EMA 200",
                        0.5 if good200 else 0))

        # 3) Réaction sur une zone clé ou sur les EMA
        zones = self.zones(i)
        own_kind = "support" if d == 1 else "resistance"
        z = nearest(zones, best.extreme, own_kind)
        if z is not None and abs(best.extreme - z.price) <= 0.5 * atr:
            F.append(Factor(f"Rejet {'du support' if d == 1 else 'de la résistance'} {z.price:.2f} ({z.touches} touche"
                            f"{'s' if z.touches > 1 else ''})", 2 if z.strong else 1))
        else:
            lo, hi = sorted((self.ef[i], self.em[i]))
            touched = (d == 1 and self.l[i] <= hi + 0.2 * atr and c > lo) or \
                      (d == -1 and self.h[i] >= lo - 0.2 * atr and c < hi)
            F.append(Factor("Rebond sur la zone EMA 20/50" if touched else "Aucune zone clé touchée",
                            1 if touched else 0))

        # 4) Momentum RSI
        r, r1 = self.rsi[i], self.rsi[i - 1]
        if d == 1:
            if r >= 75:
                F.append(Factor(f"RSI suracheté ({r:.0f})", -1))
            else:
                ok = r > r1 and r < 70
                txt = "remonte" if ok else "remonte mais déjà élevé" if r > r1 else "ne confirme pas"
                F.append(Factor(f"RSI {txt} ({r:.0f})", 1 if ok else 0))
        else:
            if r <= 25:
                F.append(Factor(f"RSI survendu ({r:.0f})", -1))
            else:
                ok = r < r1 and r > 30
                txt = "redescend" if ok else "redescend mais déjà bas" if r < r1 else "ne confirme pas"
                F.append(Factor(f"RSI {txt} ({r:.0f})", 1 if ok else 0))

        # 5) Force du marché
        if self.adx[i] >= s.adx_min:
            F.append(Factor(f"Marché directionnel (ADX {self.adx[i]:.0f})", 1))
        else:
            F.append(Factor(f"Marché sans direction (ADX {self.adx[i]:.0f})", -1))

        session_ok = in_session(ts + pd.Timedelta(minutes=5), s.sessions_utc)
        F.append(Factor("Session Londres / New York" if session_ok else "Hors session principale",
                        0.5 if session_ok else 0))

        score = round(float(np.clip(sum(f.points for f in F), 0, 10)), 1)

        # ---- Gestion du risque ----
        entry = c
        sl = best.extreme - d * s.sl_atr_buffer * atr
        risk = abs(entry - sl)
        if risk < s.min_sl_pips * pip:
            risk = s.min_sl_pips * pip
            sl = entry - d * risk
        tp1 = entry + d * s.rr_tp1 * risk
        tp2 = entry + d * s.rr_tp2 * risk

        blocked = None
        opp = nearest([z for z in zones if (z.price - entry) * d > 0], entry,
                      "resistance" if d == 1 else "support")
        if opp is not None:
            room = abs(opp.price - entry)
            if room < s.min_room_r * risk:
                blocked = f"Zone opposée trop proche ({opp.price:.2f}, {room / risk:.1f} R)"
            elif room < s.rr_tp2 * risk:
                tp2 = opp.price - d * 0.1 * atr
                if abs(tp2 - entry) < abs(tp1 - entry):
                    tp1 = tp2

        if risk > s.max_sl_pips * pip:
            blocked = f"Stop trop large ({risk / pip:.1f} pips > {s.max_sl_pips})"
        if score < s.min_score:
            blocked = f"Score insuffisant ({score} < {s.min_score})"
        if conflict:
            blocked = "Figures achat et vente contradictoires"
        if s.use_sessions and not session_ok:
            blocked = "Hors des heures de session configurées"

        mtf = {"m15": int(self.bias15[i]), "h1": int(self.bias[i]), "h4": int(self.bias4[i])}
        return Signal(ts, i, d, round(entry, 5), round(sl, 5), round(tp1, 5), round(tp2, 5),
                      score, best.name, F, round(risk / pip, 1), blocked, round(atr / pip, 1), mtf)
