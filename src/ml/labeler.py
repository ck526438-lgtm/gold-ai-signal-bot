"""Labels par la méthode Triple Barrier (López de Prado) + poids d'unicité.

Pour chaque bougie i (entrée au close de i) :
  barrière haute = close_i + k·ATR_i, barrière basse = close_i − k·ATR_i,
  barrière temporelle = i + horizon.
  label +1 si la haute est touchée en premier, −1 si la basse, 0 si timeout
  ou si les deux sont touchées dans la même bougie (ambigu).

Les labels regardent le futur PAR DÉFINITION : ils ne servent qu'à l'entraînement,
jamais comme features. `t1` (fin de la fenêtre de chaque label) sert à la purge.

Poids d'unicité : c_t = nb de labels actifs à la bougie t ;
  w_i = moyenne de 1/c_t sur [i+1, t1_i]. w_i = 1 si aucun chevauchement, < 1 sinon.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators as ind


def triple_barrier_labels(
    df: pd.DataFrame, atr_mult: float = 1.5, horizon: int = 15, atr_period: int = 14
) -> pd.DataFrame:
    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    atr = ind.atr(df, atr_period).to_numpy(float)
    n = len(df)
    label = np.full(n, np.nan)
    t1 = np.full(n, -1, dtype=np.int64)

    for i in range(n - horizon):  # les `horizon` dernières bougies n'ont pas de futur complet
        a = atr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        up, dn = close[i] + atr_mult * a, close[i] - atr_mult * a
        hi = high[i + 1: i + horizon + 1]
        lo = low[i + 1: i + horizon + 1]
        hit_up = np.flatnonzero(hi >= up)
        hit_dn = np.flatnonzero(lo <= dn)
        fu = hit_up[0] if hit_up.size else horizon
        fd = hit_dn[0] if hit_dn.size else horizon
        if fu == horizon and fd == horizon:
            label[i], t1[i] = 0, i + horizon
        elif fu < fd:
            label[i], t1[i] = 1, i + 1 + fu
        elif fd < fu:
            label[i], t1[i] = -1, i + 1 + fd
        else:  # touchées dans la même bougie → ambigu
            label[i], t1[i] = 0, i + 1 + fu

    out = pd.DataFrame({"label": label, "t1": t1}, index=df.index)
    out["sample_weight"] = uniqueness_weights(t1, n)
    return out


def uniqueness_weights(t1: np.ndarray, n: int) -> np.ndarray:
    """Poids d'unicité moyenne (vectorisé via sommes préfixes)."""
    valid = t1 >= 0
    starts = np.arange(n)[valid] + 1
    ends = t1[valid]
    conc = np.zeros(n + 2)
    np.add.at(conc, starts, 1.0)
    np.add.at(conc, ends + 1, -1.0)
    c = np.cumsum(conc)[: n + 1]
    inv = np.where(c > 0, 1.0 / np.maximum(c, 1.0), 0.0)
    pref = np.concatenate([[0.0], np.cumsum(inv)])
    w = np.full(n, np.nan)
    lengths = ends - starts + 1
    w[valid] = (pref[ends + 1] - pref[starts]) / np.maximum(lengths, 1)
    return w
