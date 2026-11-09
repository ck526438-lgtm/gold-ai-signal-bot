"""Backtester M5 oriente signaux.

Pieges traite explicitement:
  - entree a l'OUVERTURE de la bougie suivante: le close de la bougie de signal
    sert deja a calculer le signal, le reutiliser comme prix d'entree serait du
    look-ahead ;
  - SL et TP testes intra-bougie, et si les deux sont touches sur la meme bougie
    on compte la PERTE (pas de tirage au sort) ;
  - ouverture en gap au-dela du SL/TP : execution au prix du gap ;
  - couts: un spread plein + deux slippages deduits a la sortie, ce qui laisse
    les niveaux SL/TP tels que le broker les verrait.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .config import get
from .signals.rules import evaluate


@dataclass
class Trade:
    entry_time: str
    exit_time: str
    side: str
    entry: float
    exit: float
    sl: float
    tp: float
    reason: str
    bars: int
    points: float
    r_multiple: float
    cost_points: float
    gross_points: float
    score: float
    atr: float
    hour_utc: int
    session: str


def _session_name(hour: int) -> str:
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 13:
        return "london"
    if 13 <= hour < 16:
        return "overlap"
    if 16 <= hour < 21:
        return "ny"
    return "off"


def engine_columns(scored: pd.DataFrame, engine: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(drapeau declenchement, sens -1/0/+1, score) selon le moteur retenu.

    `both` n'est pas une moyenne des deux: le signal n'existe que quand les deux
    moteurs sont d'accord, ce qui divise la frequence et donc le cout.
    """
    engine = (engine or "rules").lower()
    engine = {"consensus": "both"}.get(engine, engine)
    rs = scored["rules_score"].to_numpy(float)
    r_fire = scored["rules_fire"].to_numpy(bool)
    r_sign = scored["rules_side"].map({"BUY": 1, "SELL": -1}).fillna(0).to_numpy(int)
    if engine == "rules":
        return r_fire, r_sign, rs
    if engine not in ("ml", "both"):
        raise ValueError(f"signal.engine inconnu: {engine}")
    if "ml_fire" not in scored:
        raise RuntimeError("Colonnes ML absentes: lancer `python bot.py train --walk-forward` "
                           "ou backtest avec --engine rules.")
    p = scored["ml_prob_up"].to_numpy(float)
    m_sign = scored["ml_side"].map({"BUY": 1, "SELL": -1}).fillna(0).to_numpy(int)
    m_conf = np.round(np.where(np.isnan(p), np.nan, np.where(p >= 0.5, p, 1 - p)) * 100, 1)
    m_fire = scored["ml_fire"].to_numpy(bool)
    if engine == "ml":
        return m_fire, m_sign, m_conf
    agree = (r_sign == m_sign) & (m_sign != 0)
    return r_fire & m_fire & agree, m_sign, np.round((rs + m_conf) / 2, 1)


def permute_direction(scored: pd.DataFrame, seed: int | None = None,
                      side: str | None = None) -> pd.DataFrame:
    """Tire la direction au pile-ou-face sur les seules bougies qui declenchent.

    C'est le temoin qui repond a la seule question qui compte: est-ce que le
    moteur choisit bien le sens, ou est-ce que c'est la geometrie SL/TP et la
    frequence qui encaissent? `side="BUY"`/"SELL" remplace le tirage par une
    direction unique: la reponse a « est-ce que ce n'etait que la tendance du
    marche? ». Un reglage qui ne bat pas ce temoin est un generateur de couts.
    """
    if side is not None:
        draw = np.full(len(scored), side)
    else:
        draw = np.random.default_rng(seed).choice(["BUY", "SELL"], len(scored))
    out = scored.copy()
    for fire_col, side_col in (("rules_fire", "rules_side"), ("ml_fire", "ml_side")):
        if fire_col in out.columns and side_col in out.columns:
            mask = out[fire_col].to_numpy(bool)
            out.loc[mask, side_col] = draw[mask]
    return out


def run_backtest(df: pd.DataFrame, cfg: dict, start: str | None = None,
                 end: str | None = None, engine: str | None = None) -> tuple[pd.DataFrame, dict]:
    scored = evaluate(df, cfg)
    return run_backtest_scored(scored, cfg, start, end, engine)


def run_backtest_scored(scored: pd.DataFrame, cfg: dict, start: str | None = None,
                        end: str | None = None, engine: str | None = None) -> tuple[pd.DataFrame, dict]:
    """Meme simulation mais sur un frame deja feature: permet de tester
    40 reglages sans recalculer les indicateurs a chaque fois."""
    engine = (engine or str(get(cfg, "signal.engine", "rules"))).lower()
    if start:
        scored = scored.loc[scored["time"] >= pd.Timestamp(start, tz="UTC")]
    if end:
        scored = scored.loc[scored["time"] < pd.Timestamp(end, tz="UTC")]
    scored = scored.reset_index(drop=True)

    from .risk import round_turn_cost

    cost = round_turn_cost(cfg)
    sl_mult = float(get(cfg, "risk.sl_atr", 1.5))
    tp_mult = float(get(cfg, "risk.tp_atr", 2.5))
    max_hold = int(get(cfg, "backtest.max_hold_bars", 72))
    exit_on_flip = bool(get(cfg, "backtest.exit_on_opposite_state", True))

    fire, sign, scores = engine_columns(scored, engine)
    o = scored["open"].to_numpy(float)
    hi = scored["high"].to_numpy(float)
    lo = scored["low"].to_numpy(float)
    cl = scored["close"].to_numpy(float)
    atr = scored["atr"].to_numpy(float)
    state_bull = sign > 0
    times = scored["time"].to_numpy()

    trades: list[Trade] = []
    n = len(scored)
    i = 0
    while i < n - 1:
        if not fire[i] or sign[i] == 0 or not np.isfinite(atr[i]) or atr[i] <= 0:
            i += 1
            continue
        side = int(sign[i])
        entry = o[i + 1]
        if not np.isfinite(entry):
            i += 1
            continue
        risk, reward = sl_mult * atr[i], tp_mult * atr[i]
        sl, tp = entry - side * risk, entry + side * reward

        exit_idx, exit_price, reason = None, None, None
        end = min(n, i + 1 + max_hold)
        for j in range(i + 1, end):
            if side > 0:
                hit_sl, hit_tp = lo[j] <= sl, hi[j] >= tp
            else:
                hit_sl, hit_tp = hi[j] >= sl, lo[j] <= tp
            if hit_sl:
                # Les deux dans la meme bougie -> on encaisse le SL.
                exit_idx, exit_price, reason = j, (o[j] if (side > 0 and o[j] < sl) or (side < 0 and o[j] > sl) else sl), "sl"
                break
            if hit_tp:
                exit_idx, exit_price, reason = j, (o[j] if (side > 0 and o[j] > tp) or (side < 0 and o[j] < tp) else tp), "tp"
                break
            if exit_on_flip and fire[j] and state_bull[j] != (side > 0):
                exit_idx, exit_price, reason = j, o[j], "flip"
                break
        if exit_idx is None:
            j = min(n - 1, i + max_hold)
            exit_idx, exit_price, reason = j, cl[j], "time"

        gross = (exit_price - entry) * side
        net = gross - cost
        trades.append(Trade(
            entry_time=pd.Timestamp(times[i + 1]).isoformat(),
            exit_time=pd.Timestamp(times[exit_idx]).isoformat(),
            side="BUY" if side > 0 else "SELL",
            entry=round(float(entry), 3), exit=round(float(exit_price), 3),
            sl=round(float(sl), 3), tp=round(float(tp), 3), reason=reason,
            bars=int(exit_idx - i), points=round(float(net), 3),
            r_multiple=round(float(net) / risk, 3), cost_points=round(float(cost), 3),
            gross_points=round(float(gross), 3), score=float(scores[i]),
            atr=round(float(atr[i]), 3),
            hour_utc=int(pd.Timestamp(times[i]).hour),
            session=_session_name(int(pd.Timestamp(times[i]).hour)),
        ))
        i = exit_idx + 1

    log = pd.DataFrame([asdict(t) for t in trades])
    bar_days = int(pd.to_datetime(scored["time"], utc=True).dt.date.nunique())
    return log, summarize(log, cfg, int(fire.sum()), bar_days)


def _ratio(returns: np.ndarray) -> float:
    """Moyenne / ecart-type: le rendement par unite de risque, sans sqrt(n)."""
    sd = returns.std(ddof=0)
    return 0.0 if sd == 0 else float(returns.mean() / sd)


def _group_stats(log: pd.DataFrame, key: str, min_trades: int) -> dict:
    g = log.groupby(key, observed=True).agg(
        n=("points", "size"), win=("r_multiple", lambda s: (s > 0).mean()),
        avg_r=("r_multiple", "mean"), pts=("points", "sum"),
    )
    g = g[g["n"] >= min_trades]
    return {str(k): {"n": int(v["n"]), "win_pct": round(float(v["win"]) * 100, 1),
                     "avg_r": round(float(v["avg_r"]), 3), "points": round(float(v["pts"]), 1)}
            for k, v in g.iterrows()}


def summarize(log: pd.DataFrame, cfg: dict, n_signals: int = 0, bar_days: int = 0) -> dict:
    """Baremes d'un journal de trades.

    `bar_days` = nombre de jours ou des bougies existent vraiment. Sans lui, la
    frequence journaliere serait calculee sur les seuls jours a trade et
    gonflerait pour un reglage rare (ex. 70 trades sur 59 jours actifs).
    """
    if log.empty:
        return {"trades": 0, "message": "aucun trade: baisser min_score ou verifier les portes"}
    min_trades = int(get(cfg, "backtest.min_group_trades", 40))
    r = log["r_multiple"].to_numpy(float)
    net = log["points"].to_numpy(float)
    gross = log["gross_points"].to_numpy(float)
    equity = np.cumsum(net)
    dd = equity - np.maximum.accumulate(equity)
    days = pd.to_datetime(log["entry_time"], utc=True).dt.date
    daily = log.groupby(days)["points"].sum()
    # t de Student sur la moyenne des R: c'est la seule facon de savoir si
    # l'esperance mesuree sort du bruit ou la reproduce 1000 fois.
    t_stat = _ratio(r) * np.sqrt(len(r))
    denom = int(bar_days) or int(len(days.unique()))

    return {
        "period": {"from": log["entry_time"].iloc[0][:10], "to": log["exit_time"].iloc[-1][:10],
                   "trading_days": denom},
        "trades": int(len(log)),
        "signals_in_window": int(n_signals),
        "trades_per_day": round(float(len(log) / max(1, denom)), 2),
        "win_rate_pct": round(float((r > 0).mean() * 100), 2),
        "avg_r": round(float(r.mean()), 4),
        "median_r": round(float(np.median(r)), 4),
        "t_stat_avg_r": round(float(t_stat), 2),
        "edge_significatif": bool(abs(t_stat) >= 1.96),
        "expectancy_net_points": round(float(net.mean()), 3),
        "expectancy_gross_points": round(float(gross.mean()), 3),
        "cost_drag_pct": round(float((1 - net.mean() / gross.mean()) * 100), 1) if gross.mean() > 0 else None,
        "profit_factor": round(float(net[net > 0].sum() / abs(net[net < 0].sum())), 3) if (net < 0).any() else None,
        "total_net_points": round(float(net.sum()), 1),
        "max_drawdown_points": round(float(-dd.min()), 1),
        "sharpe_per_trade": round(_ratio(r), 3),
        "sharpe_daily": round(_ratio(daily.to_numpy(float)), 3),
        "avg_hold_bars": round(float(log["bars"].mean()), 1),
        "best_trade_points": round(float(net.max()), 2),
        "worst_trade_points": round(float(net.min()), 2),
        "exit_reasons": log["reason"].value_counts().to_dict(),
        "side_split": log["side"].value_counts().to_dict(),
        "worst_day_points": round(float(daily.min()), 1),
        "best_day_points": round(float(daily.max()), 1),
        "by_session": _group_stats(log.assign(session=log["session"]), "session", min_trades),
        "by_side": _group_stats(log, "side", min_trades),
        "by_hour": _group_stats(log, "hour_utc", min_trades),
        "by_atr": _group_stats(log.assign(atr=pd.cut(log["atr"], [0, 2, 4, 8, 100],
                                                      labels=["atr<2", "2-4", "4-8", "atr>8"])),
                               "atr", min_trades),
    }


def cmd_backtest(cfg: dict, args) -> int:
    from .data.bars import closed_only
    from .data.history import load_cache
    from .signals import ml as ml_engine

    tf = str(get(cfg, "symbol.timeframe", "M5"))
    df = closed_only(load_cache(cfg, args.symbol, tf), tf)
    scored = evaluate(df, cfg)
    engine = getattr(args, "engine", None) or str(get(cfg, "signal.engine", "rules"))
    if engine in ("ml", "both"):
        scored = ml_engine.add_ml_columns(scored, cfg)
    log, stats = run_backtest_scored(scored, cfg, start=args.start, end=args.end, engine=engine)
    print(json.dumps(stats, indent=2, default=str))
    if args.export and not log.empty:
        log.to_csv(args.export, index=False)
        print("journal des trades:", args.export)
    return 0
