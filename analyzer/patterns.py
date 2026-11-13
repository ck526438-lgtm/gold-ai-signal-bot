"""Détection des figures de chandeliers japonais sur la dernière bougie clôturée.

Chaque détecteur reçoit les bougies jusqu'à l'index i (inclus) et renvoie
une liste de Pattern. La « force » va de 1 (faible) à 3 (forte).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


PATTERN_NAMES = [
    "Avalement haussier", "Avalement baissier",
    "Pin bar haussière (marteau)", "Pin bar baissière (étoile filante)",
    "Étoile du matin", "Étoile du soir",
    "Trois soldats blancs", "Trois corbeaux noirs",
    "Pince basse (tweezer bottom)", "Pince haute (tweezer top)",
    "Cassure d'inside bar haussière", "Cassure d'inside bar baissière",
]


@dataclass
class Pattern:
    name: str
    direction: int      # +1 achat, -1 vente
    strength: int       # 1..3
    extreme: float      # plus bas (achat) / plus haut (vente) de la figure, pour le SL


def _c(o, h, l, c):
    body = abs(c - o)
    rng = max(h - l, 1e-12)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return body, rng, upper, lower


def detect(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, i: int, atr: float) -> list[Pattern]:
    if i < 3 or atr <= 0:
        return []
    res: list[Pattern] = []
    b0, r0, u0, lo0 = _c(o[i], h[i], l[i], c[i])
    b1, r1, u1, lo1 = _c(o[i - 1], h[i - 1], l[i - 1], c[i - 1])
    b2, r2, _, _ = _c(o[i - 2], h[i - 2], l[i - 2], c[i - 2])
    bull0, bear0 = c[i] > o[i], c[i] < o[i]
    bull1, bear1 = c[i - 1] > o[i - 1], c[i - 1] < o[i - 1]
    meaningful = r0 >= 0.6 * atr          # on ignore les micro-bougies

    # --- Avalement (engulfing) ---
    if meaningful and bull0 and bear1 and c[i] >= o[i - 1] and o[i] <= c[i - 1] and b0 > b1 * 1.1:
        res.append(Pattern("Avalement haussier", 1, 3 if b0 > 1.0 * atr else 2, min(l[i], l[i - 1])))
    if meaningful and bear0 and bull1 and c[i] <= o[i - 1] and o[i] >= c[i - 1] and b0 > b1 * 1.1:
        res.append(Pattern("Avalement baissier", -1, 3 if b0 > 1.0 * atr else 2, max(h[i], h[i - 1])))

    # --- Pin bar : marteau / étoile filante ---
    if meaningful and lo0 >= 2.0 * b0 and lo0 >= 0.6 * r0 and u0 <= 0.25 * r0:
        res.append(Pattern("Pin bar haussière (marteau)", 1, 3 if lo0 > 1.2 * atr else 2, l[i]))
    if meaningful and u0 >= 2.0 * b0 and u0 >= 0.6 * r0 and lo0 <= 0.25 * r0:
        res.append(Pattern("Pin bar baissière (étoile filante)", -1, 3 if u0 > 1.2 * atr else 2, h[i]))

    # --- Étoile du matin / du soir ---
    small_mid = b1 <= 0.35 * max(r1, 1e-12) or b1 <= 0.3 * atr
    if bull0 and c[i - 2] < o[i - 2] and b2 >= 0.6 * atr and small_mid and c[i] > (o[i - 2] + c[i - 2]) / 2:
        res.append(Pattern("Étoile du matin", 1, 3, min(l[i], l[i - 1], l[i - 2])))
    if bear0 and c[i - 2] > o[i - 2] and b2 >= 0.6 * atr and small_mid and c[i] < (o[i - 2] + c[i - 2]) / 2:
        res.append(Pattern("Étoile du soir", -1, 3, max(h[i], h[i - 1], h[i - 2])))

    # --- Trois soldats blancs / trois corbeaux noirs ---
    last3_bull = all(c[k] > o[k] and abs(c[k] - o[k]) >= 0.5 * atr for k in (i - 2, i - 1, i))
    last3_bear = all(c[k] < o[k] and abs(c[k] - o[k]) >= 0.5 * atr for k in (i - 2, i - 1, i))
    if last3_bull and c[i] > c[i - 1] > c[i - 2]:
        res.append(Pattern("Trois soldats blancs", 1, 2, l[i - 2]))
    if last3_bear and c[i] < c[i - 1] < c[i - 2]:
        res.append(Pattern("Trois corbeaux noirs", -1, 2, h[i - 2]))

    # --- Pinces (tweezer) ---
    tol = 0.1 * atr
    if bear1 and bull0 and abs(l[i] - l[i - 1]) <= tol and meaningful:
        res.append(Pattern("Pince basse (tweezer bottom)", 1, 2, min(l[i], l[i - 1])))
    if bull1 and bear0 and abs(h[i] - h[i - 1]) <= tol and meaningful:
        res.append(Pattern("Pince haute (tweezer top)", -1, 2, max(h[i], h[i - 1])))

    # --- Cassure d'inside bar ---
    # i-2 = bougie mère, i-1 = inside bar, i = cassure en clôture
    if h[i - 1] <= h[i - 2] and l[i - 1] >= l[i - 2] and r2 >= 0.8 * atr:
        if c[i] > h[i - 2] and bull0:
            res.append(Pattern("Cassure d'inside bar haussière", 1, 2, l[i - 1]))
        elif c[i] < l[i - 2] and bear0:
            res.append(Pattern("Cassure d'inside bar baissière", -1, 2, h[i - 1]))

    return res
