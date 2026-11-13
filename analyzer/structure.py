"""Structure de marché : points pivots (swings) et zones de support / résistance."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SWING_K = 3  # un pivot = plus haut/bas de 3 bougies de chaque côté


@dataclass
class Zone:
    price: float
    touches: int
    kind: str  # "support" | "resistance" (relatif au prix actuel)

    @property
    def strong(self) -> bool:
        return self.touches >= 2


def swing_points(h: np.ndarray, l: np.ndarray, k: int = SWING_K):
    """Indices des pivots hauts et bas. Un pivot en j n'est CONFIRMÉ qu'en j+k."""
    n = len(h)
    sh, sl = [], []
    for j in range(k, n - k):
        win_h = h[j - k:j + k + 1]
        win_l = l[j - k:j + k + 1]
        if h[j] == win_h.max() and np.argmax(win_h) == k:
            sh.append(j)
        if l[j] == win_l.min() and np.argmin(win_l) == k:
            sl.append(j)
    return np.array(sh, dtype=int), np.array(sl, dtype=int)


def zones_at(i: int, h, l, swings_hi, swings_lo, close: float, atr: float,
             lookback: int, merge_atr: float, k: int = SWING_K) -> list[Zone]:
    """Zones S/R connues à la clôture de la bougie i (aucune donnée future)."""
    lo_bound, hi_bound = i - lookback, i - k
    a, b = np.searchsorted(swings_hi, [lo_bound, hi_bound + 1])
    c, d = np.searchsorted(swings_lo, [lo_bound, hi_bound + 1])
    levels = h[swings_hi[a:b]].tolist() + l[swings_lo[c:d]].tolist()
    if not levels:
        return []
    levels.sort()
    tol = merge_atr * atr
    clusters: list[list[float]] = [[levels[0]]]
    for p in levels[1:]:
        if p - np.mean(clusters[-1]) <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [
        Zone(float(np.mean(c)), len(c), "support" if np.mean(c) < close else "resistance")
        for c in clusters
    ]


def nearest(zones: list[Zone], price: float, kind: str) -> Zone | None:
    cands = [z for z in zones if z.kind == kind]
    if not cands:
        return None
    return min(cands, key=lambda z: abs(z.price - price))
