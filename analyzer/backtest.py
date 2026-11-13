"""Simulation des trades (backtest et suivi en direct des signaux).

Règles : spread payé à l'entrée, 50 % fermé à TP1 puis stop au point d'entrée,
reste à TP2, sortie forcée après `max_hold_bars`. Si SL et TP sont touchés dans
la même bougie, on compte le SL (hypothèse prudente).

Gestion du robot (`Management`, mêmes règles qu'en direct) :
* stop suiveur — dès que le gain atteint `trail_start_r` R, le stop suit le meilleur
  prix à `trail_distance_r` R de distance et ne recule jamais. Le nouveau stop ne
  s'applique qu'à partir de la bougie suivante (on ignore l'ordre haut/bas dans la bougie) ;
* sortie sur retournement — clôture au cours de clôture si une bougie fait apparaître
  un signal opposé de score ≥ `reversal_score` (données connues à la clôture uniquement).
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from .strategy import Analyzer, Signal


@dataclass
class Management:
    trailing: bool = False
    trail_start_r: float = 1.0
    trail_distance_r: float = 0.7
    reversal_exit: bool = False
    reversal_score: float = 6.0
    reversal_momentum: bool = False
    reversal_structure: bool = False
    entry_mode: str = "market"
    pullback_r: float = 0.3
    pullback_bars: int = 3
    target_r: float = 0.0              # encaisser vite : objectif unique à X R (0 = TP1/TP2)

    @classmethod
    def from_cfg(cls, cfg) -> "Management":
        t = cfg.trading
        return cls(t.trailing, t.trail_start_r, t.trail_distance_r, t.reversal_exit, t.reversal_score,
                   t.reversal_momentum, t.reversal_structure, t.entry_mode, t.pullback_r, t.pullback_bars,
                   t.quick_target_r)

    @property
    def active(self) -> bool:
        return self.trailing or self.reversal_exit or self.entry_mode == "pullback" or self.target_r > 0


def reversal_at(an: Analyzer, j: int, direction: int, min_score: float) -> Signal | None:
    """Signal opposé solide sur la bougie j (None sinon)."""
    c = an.candidate(j)
    if c is None or c.direction != -direction or c.score < min_score:
        return None
    if c.blocked and c.blocked.startswith("Figures achat et vente"):   # signal ambigu
        return None
    return c


def reversal_info(an: Analyzer, j: int) -> dict:
    """Tout ce qui permet de détecter un retournement, connu à la clôture de la bougie j.

    * `candidate` : meilleure figure de la bougie (même rejetée) ;
    * `momentum[d]` : perte d'élan CONTRE une position de sens d (clôture qui repasse de
      l'autre côté de l'EMA 20 avec une vraie bougie, RSI de l'autre côté de 50) ;
    * `swing_low` / `swing_high` : dernier creux / sommet CONFIRMÉ (3 bougies de chaque côté).
    """
    from .structure import SWING_K

    cache = an.__dict__.setdefault("_rev_cache", {})
    if j in cache:
        return cache[j]
    o, c, ef, rsi, atr = an.o, an.c, an.ef, an.rsi, an.atr
    cand = an.candidate(j)
    mom = {1: False, -1: False}
    if j >= 1:
        body = abs(c[j] - o[j])
        mom[1] = bool(c[j] < ef[j] and c[j - 1] >= ef[j - 1] and rsi[j] < 50 and c[j] < o[j] and body >= 0.3 * atr[j])
        mom[-1] = bool(c[j] > ef[j] and c[j - 1] <= ef[j - 1] and rsi[j] > 50 and c[j] > o[j] and body >= 0.3 * atr[j])

    def last_swing(arr, prices):
        k = int(np.searchsorted(arr, j - SWING_K, side="right"))
        if k == 0:
            return None
        s = int(arr[k - 1])
        return {"price": float(prices[s]), "time": an.idx[s].isoformat()}

    cache[j] = {"time": (an.idx[j] + pd.Timedelta(minutes=5)).isoformat(), "close": float(c[j]),
                "candidate": None if cand is None else {"direction": cand.direction, "score": cand.score,
                                                        "pattern": cand.pattern, "blocked": cand.blocked,
                                                        "key": cand.key},
                "momentum": mom, "swing_low": last_swing(an.sw_lo, an.l), "swing_high": last_swing(an.sw_hi, an.h)}
    return cache[j]


def reversal_reason(info: dict, d: int, mg: "Management", opened: pd.Timestamp) -> str | None:
    """Raison de couper une position de sens d ouverte à `opened` (début de bougie), ou None."""
    if not mg.reversal_exit or info is None:
        return None
    cand = info.get("candidate")
    if cand and int(cand["direction"]) == -d and cand["score"] >= mg.reversal_score \
            and not (cand.get("blocked") or "").startswith("Figures achat et vente"):
        return f"{cand['pattern']} {cand['score']}/10 en sens inverse"
    if mg.reversal_momentum and (info.get("momentum") or {}).get(d, (info.get("momentum") or {}).get(str(d))):
        return ("perte d'élan : clôture sous l'EMA 20, RSI sous 50" if d == 1
                else "perte d'élan : clôture au-dessus de l'EMA 20, RSI au-dessus de 50")
    if mg.reversal_structure:
        sw = info.get("swing_low" if d == 1 else "swing_high")
        if sw and pd.Timestamp(sw["time"]) >= opened and (info["close"] - sw["price"]) * d < 0:
            return (f"cassure du dernier creux {sw['price']:.2f}" if d == 1
                    else f"cassure du dernier sommet {sw['price']:.2f}")
    return None


@dataclass
class Outcome:
    status: str          # "en cours" | "TP2" | "SL" | "BE" | "suiveur" | "retournement" | "temps"
                         # | "non rempli" / "annulé" (entrée sur repli jamais déclenchée)
    result_r: float      # résultat en R (réalisé + latent si en cours)
    tp1_hit: bool
    exit_index: int | None
    exit_price: float | None


def quick_target(entry: float, sl: float, direction: int, target_r: float) -> float:
    """Objectif « encaisser vite » : X fois le risque initial."""
    return round(entry + direction * target_r * abs(entry - sl), 5)


def simulate(an: Analyzer, sig: Signal, spread_pips: float, max_bars: int,
             mg: Management | None = None) -> Outcome:
    if mg is not None and mg.target_r > 0:
        tp = quick_target(sig.entry, sig.sl, sig.direction, mg.target_r)
        out = _simulate(an, replace(sig, tp1=tp, tp2=tp), spread_pips, max_bars, mg)
        if out.status == "TP2":
            out.status = "objectif"
        return out
    return _simulate(an, sig, spread_pips, max_bars, mg)


def _simulate(an: Analyzer, sig: Signal, spread_pips: float, max_bars: int,
              mg: Management | None = None) -> Outcome:
    h, l, c, n = an.h, an.l, an.c, len(an.c)
    d = sig.direction
    spread = spread_pips * an.cfg.pip
    entry, start = sig.entry + d * spread, sig.index + 1
    if mg is not None and mg.entry_mode == "pullback":
        # entrée sur repli : le prix d'achat (ask) doit revenir au niveau avant TP1
        level = sig.entry - d * mg.pullback_r * abs(sig.entry - sig.sl)
        filled = None
        for j in range(sig.index + 1, min(n, sig.index + 1 + mg.pullback_bars)):
            hit = (l[j] + spread <= level) if d == 1 else (h[j] >= level)
            tp_touch = (h[j] >= sig.tp1) if d == 1 else (l[j] <= sig.tp1)
            if tp_touch:          # ordre inconnu dans la bougie : on suppose le gagnant manqué
                return Outcome("annulé", 0.0, False, j, None)
            if hit:
                filled = j
                break
        if filled is None:
            j = min(n - 1, sig.index + mg.pullback_bars)
            if sig.index + mg.pullback_bars >= n:
                return Outcome("en cours", 0.0, False, None, None)
            return Outcome("non rempli", 0.0, False, j, None)
        entry, start = level, filled
    risk = max(abs(entry - sig.sl), 1e-9)
    sl, half, r = sig.sl, False, 0.0
    trailed = False
    peak = entry
    last = min(n, start + max_bars)
    for j in range(start, last):
        if (l[j] <= sl) if d == 1 else (h[j] >= sl):
            r += (0.5 if half else 1.0) * (sl - entry) * d / risk
            status = "suiveur" if trailed and (sl - entry) * d > 0 else ("BE" if half else "SL")
            return Outcome(status, round(r, 3), half, j, sl)
        if not half and ((h[j] >= sig.tp1) if d == 1 else (l[j] <= sig.tp1)):
            r += 0.5 * (sig.tp1 - entry) * d / risk
            half = True
            sl = max(sl, entry) if d == 1 else min(sl, entry)
        if half and ((h[j] >= sig.tp2) if d == 1 else (l[j] <= sig.tp2)):
            r += 0.5 * (sig.tp2 - entry) * d / risk
            return Outcome("TP2", round(r, 3), True, j, sig.tp2)
        if mg is None:
            continue
        if mg.trailing and (j > start or start == sig.index + 1):
            # bougie d'entrée sur repli : son plus haut a pu avoir lieu AVANT l'entrée → ignoré
            peak = max(peak, h[j]) if d == 1 else min(peak, l[j])
            if (peak - entry) * d >= mg.trail_start_r * risk:
                new_sl = peak - d * mg.trail_distance_r * risk
                if (new_sl - sl) * d > 0:
                    sl, trailed = new_sl, True
        if mg.reversal_exit and reversal_reason(reversal_info(an, j), d, mg, an.idx[start]) is not None:
            r += (0.5 if half else 1.0) * (c[j] - entry) * d / risk
            return Outcome("retournement", round(r, 3), half, j, float(c[j]))
    j = last - 1
    rest = (0.5 if half else 1.0) * (c[j] - entry) * d / risk if j >= start else 0.0
    if last - start >= max_bars:
        return Outcome("temps", round(r + rest, 3), half, j, float(c[j]))
    return Outcome("en cours", round(r + rest, 3), half, None, None)


def _trades(an: Analyzer, cfg, mg: Management | None) -> list[dict]:
    from .config import risk_factor

    s = cfg.strategy
    rows, busy_until = [], -1
    for sig in an.scan():
        if sig.index <= busy_until:
            continue
        out = simulate(an, sig, s.spread_pips, s.max_hold_bars, mg)
        if out.status == "en cours":
            break
        busy_until = out.exit_index
        if out.status in ("non rempli", "annulé"):     # entrée sur repli jamais déclenchée
            continue
        w = risk_factor(sig.score, cfg)
        rows.append({
            "time": sig.close_time.isoformat(), "side": sig.side, "pattern": sig.pattern,
            "score": sig.score, "quality": sig.quality, "entry": sig.entry, "sl": sig.sl,
            "tp1": sig.tp1, "tp2": sig.tp2, "exit": out.status, "risk": w,
            "result_r": round(out.result_r * w, 3), "hour": sig.close_time.hour,
        })
    return rows


def run_backtest(df: pd.DataFrame, cfg, compare: bool = True) -> dict:
    """Backtest complet : un seul trade à la fois, délai entre signaux respecté.

    Les trades sont gérés comme le robot (stop suiveur, retournement, risque réduit des
    scores faibles). `management` compare ces règles à la gestion classique sur les mêmes
    données ; les résultats en R sont pondérés par le risque réellement engagé.
    """
    an = Analyzer(df, cfg)
    mg = Management.from_cfg(cfg)
    rows = _trades(an, cfg, mg if mg.active else None)
    trades = pd.DataFrame(rows)
    out = {
        "period": {"start": df.index[0].isoformat(), "end": df.index[-1].isoformat(), "bars": len(df)},
        "stats": summarize(trades),
        "equity": _equity(trades),
        "by_pattern": _group(trades, "pattern"),
        "by_quality": _group(trades, "quality"),
        "by_hour": _group(trades, "hour"),
        "trades": rows[::-1],
    }
    if compare:
        classic = summarize(pd.DataFrame(_trades(an, cfg, None))) if mg.active else out["stats"]
        out["management"] = {"rules": vars(mg) | {"active": mg.active}, "robot": out["stats"],
                             "classic": classic}
    return out


def summarize(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"trades": 0}
    r = t["result_r"]
    eq = r.cumsum()
    wins, losses = r[r > 0], r[r <= 0]
    streak = max_streak = 0
    for v in r:
        streak = streak + 1 if v < 0 else 0
        max_streak = max(max_streak, streak)
    return {
        "trades": int(len(r)),
        "win_rate": round(100 * len(wins) / len(r), 1),
        "total_r": round(float(r.sum()), 2),
        "expectancy_r": round(float(r.mean()), 3),
        "profit_factor": round(float(wins.sum() / abs(losses.sum())), 2) if losses.sum() else None,
        "max_drawdown_r": round(float((eq.cummax() - eq).max()), 2),
        "avg_win_r": round(float(wins.mean()), 2) if len(wins) else 0,
        "avg_loss_r": round(float(losses.mean()), 2) if len(losses) else 0,
        "max_losing_streak": int(max_streak),
        "exits": {k: int(v) for k, v in t["exit"].value_counts().items()},
    }


def _equity(t: pd.DataFrame) -> list[dict]:
    if t.empty:
        return []
    eq = t["result_r"].cumsum().round(3)
    return [{"time": tm, "value": float(v)} for tm, v in zip(t["time"], eq)]


def _group(t: pd.DataFrame, col: str) -> list[dict]:
    if t.empty:
        return []
    g = t.groupby(col)["result_r"]
    out = pd.DataFrame({
        "trades": g.size(), "win_rate": g.apply(lambda x: 100 * (x > 0).mean()),
        "expectancy_r": g.mean(), "total_r": g.sum(),
    }).round(2).reset_index().rename(columns={col: "key"})
    out["key"] = out["key"].astype(str)
    return out.sort_values("total_r", ascending=False).to_dict("records")
