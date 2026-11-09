"""Grille de reglage avec separateur in-sample / out-of-sample.

Le backtest brut ne prouve rien: choisir les heures gagnantes sur les memes
donnees qui servent a evaluer, c'est de la courbe. Ici les heures retenues par
le mode `learn_from_is` sont apprises sur la premiere partie uniquement, puis
payees sur la seconde.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime

import numpy as np
import pandas as pd

from .backtest import run_backtest_scored
from .config import get
from .features import compute_features
from .signals.rules import rule_frame

WEIGHT_VARIANTS = {
    "base": None,
    "no_meanrev": {"mean_reversion": 0},
    "trend_heavy": {"trend": 40, "momentum": 20, "mean_reversion": 0, "volatility": 20,
                    "price_action": 12, "session": 8},
    "session_inverted": {"session": -10},
}

SESSION_MODES = ["all", "drop_overlap", "drop_quiet_toxic", "learn_from_is"]


def _apply_session_mode(scored: pd.DataFrame, cfg: dict, mode: str, is_hours: set | None) -> pd.DataFrame:
    out = scored.copy()
    hour = out["time"].dt.hour
    blocked = pd.Series(False, index=out.index)
    if mode == "drop_overlap":
        blocked = out["session_overlap"].eq(1)
    elif mode == "drop_quiet_toxic":
        # Heures les plus negatives du reglage de depart, hors period de news.
        blocked = hour.isin([10, 11, 13, 14, 15, 19])
    elif mode == "learn_from_is" and is_hours is not None:
        blocked = ~hour.isin(is_hours)
    if mode != "all":
        out["rules_fire"] = out["rules_fire"] & ~blocked
        out["block_reason"] = out["block_reason"].mask(blocked & out["rules_state"], "session_filter")
    return out


def _hours_from_is(scored: pd.DataFrame, cfg: dict, end: str, min_trades: int) -> set:
    log, _ = run_backtest_scored(scored, cfg, end=end)
    if log.empty:
        return set()
    log = log.assign(hour=pd.to_datetime(log["entry_time"], utc=True).dt.hour)
    stats = log.groupby("hour")["r_multiple"].agg(["size", "mean"])
    stats = stats[(stats["size"] >= min_trades) & (stats["mean"] > 0)]
    return set(stats.index.tolist())


def cmd_compare(cfg: dict, args) -> int:
    """Confronte les trois moteurs sur les memes bougies, couts inclus.

    Les predictions ML viennent du walk-forward: la comparaison est legitime,
    aucune bougie n'est notee par un modele qui l'a vue a l'entrainement.
    """
    from .backtest import run_backtest_scored
    from .data.bars import closed_only
    from .data.history import load_cache
    from .signals import ml as ml_engine

    tf = str(get(cfg, "symbol.timeframe", "M5"))
    df = closed_only(load_cache(cfg, args.symbol, tf), tf)
    scored = rule_frame(compute_features(df, cfg), cfg)
    scored = ml_engine.add_ml_columns(scored, cfg, walk_forward_mode=True)

    rows = []
    for engine in ("rules", "ml", "both"):
        log, st = run_backtest_scored(scored, cfg, start=args.start, engine=engine)
        if st.get("trades", 0) == 0:
            rows.append({"engine": engine, "trades": 0})
            continue
        rows.append({
            "engine": engine, "trades": st["trades"], "per_day": st["trades_per_day"],
            "win%": st["win_rate_pct"], "avg_R": st["avg_r"], "t_stat": st["t_stat_avg_r"],
            "significatif": st["edge_significatif"], "exp_net": st["expectancy_net_points"],
            "exp_brut": st["expectancy_gross_points"], "PF": st["profit_factor"],
            "total_pts": st["total_net_points"], "maxDD": st["max_drawdown_points"],
            "sharpe_trade": st["sharpe_per_trade"],
        })
        if args.export and engine == args.export_engine:
            log.to_csv(f"data/trades_{engine}.csv", index=False)
    table = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(table.to_string(index=False))
    print("\nLecture: t_stat >= 1.96 = edge reellement distinct du bruit. "
          "exp_brut = ce que rend la direction seule, exp_net = apres spread + slippage.")

    if not args.no_control:
        _direction_control(scored, cfg, args)
    return 0


def _direction_control(scored: pd.DataFrame, cfg: dict, args) -> None:
    """Compare chaque moteur a la meme frequence de trade mais au sens tire au hasard.

    La sortie sur retournement est coupee des deux cotes: avec des sens
    aleatoires elle se declencherait deux fois plus souvent, ce qui changerait
    le nombre de trades et rendrait les esperances incompareables.
    """
    from .backtest import permute_direction, run_backtest_scored

    ctrl = copy.deepcopy(cfg)
    ctrl["backtest"]["exit_on_opposite_state"] = False
    print("\n--- Temoin de direction (meme timing, sens tire au hasard, sortie sur retournement coupee) ---")
    rows = []
    for engine in ("rules", "ml", "both"):
        _, real = run_backtest_scored(scored, ctrl, start=args.start, engine=engine)
        if real.get("trades", 0) == 0:
            rows.append({"engine": engine, "n_reel": 0})
            continue
        seeds = [run_backtest_scored(permute_direction(scored, seed=k), ctrl,
                                     start=args.start, engine=engine)[1]
                 for k in range(args.seeds)]
        _, always_b = run_backtest_scored(permute_direction(scored, side="BUY"), ctrl,
                                          start=args.start, engine=engine)
        _, always_s = run_backtest_scored(permute_direction(scored, side="SELL"), ctrl,
                                          start=args.start, engine=engine)
        nets = [s["expectancy_net_points"] for s in seeds]
        rows.append({
            "engine": engine, "n_reel": real["trades"], "net_reel": real["expectancy_net_points"],
            "t_reel": real["t_stat_avg_r"], "n_hasard": int(np.mean([s["trades"] for s in seeds])),
            "net_hasard": round(float(np.mean(nets)), 3), "sd_hasard": round(float(np.std(nets)), 3),
            "apport_sens": round(real["expectancy_net_points"] - float(np.mean(nets)), 3),
            "net_tjs_buy": always_b["expectancy_net_points"], "net_tjs_sell": always_s["expectancy_net_points"],
        })
    print(pd.DataFrame(rows).to_string(index=False))
    print("\nLecture: `apport_sens` = ce que rapporte le CHOIX du sens, a timing et a nombre de trades"
          " comparables. S'il est proche de 0, le moteur ne pred rien: il paie du spread sur des"
          " signaux au hasard. Comparer aussi a `net_tjs_buy`/`net_tjs_sell`: si une direction fixe"
          " fait aussi bien, ce n'etait que la tendance du marche.\n"
          "Attention `both`: au hasard les deux moteurs se mettent d'accord une fois sur deux, son"
          " nombre de trades change done, et son `apport_sens` est sur-evalue.")


def cmd_tune(cfg: dict, args) -> int:
    from .data.bars import closed_only
    from .data.history import load_cache

    tf = str(get(cfg, "symbol.timeframe", "M5"))
    df = closed_only(load_cache(cfg, args.symbol, tf), tf)
    base = compute_features(df, cfg)
    split = args.split or str(pd.Timestamp(base["time"].iloc[int(len(base) * 0.6)]).date())
    min_tr = int(args.min_trades)

    rows = []
    for score in args.scores:
        for weights_name, weights in WEIGHT_VARIANTS.items():
            cfg_v = copy.deepcopy(cfg)
            cfg_v.setdefault("signal", {}).setdefault("rules", {})["weights"] = weights or get(cfg, "signal.rules.weights")
            cfg_v["signal"]["min_score"] = score
            cfg_v["signal"]["min_agreeing_components"] = args.agreeing

            scored = rule_frame(base, cfg_v)
            is_hours = _hours_from_is(scored, cfg_v, split, min_tr) if "learn_from_is" in SESSION_MODES else set()
            for mode in SESSION_MODES:
                if mode == "learn_from_is" and not is_hours:
                    continue
                variant = _apply_session_mode(scored, cfg_v, mode, is_hours)
                _, is_stats = run_backtest_scored(variant, cfg_v, end=split)
                _, oos_stats = run_backtest_scored(variant, cfg_v, start=split)
                if oos_stats.get("trades", 0) < min_tr or is_stats.get("trades", 0) < min_tr:
                    continue
                rows.append({
                    "min_score": score, "weights": weights_name, "session_mode": mode,
                    "is_trades": is_stats["trades"], "is_exp": is_stats["expectancy_net_points"],
                    "is_wr": is_stats["win_rate_pct"],
                    "oos_trades": oos_stats["trades"], "oos_exp": oos_stats["expectancy_net_points"],
                    "oos_wr": oos_stats["win_rate_pct"], "oos_pf": oos_stats["profit_factor"],
                    "oos_sharpe": oos_stats["sharpe_per_trade"],
                    "oos_dd": oos_stats["max_drawdown_points"],
                    "oos_trades_per_day": oos_stats["trades_per_day"],
                    "oos_total_pts": oos_stats["total_net_points"],
                    "decay": round(float(oos_stats["expectancy_net_points"] - is_stats["expectancy_net_points"]), 3),
                })
    table = pd.DataFrame(rows).sort_values("oos_exp", ascending=False)
    pd.set_option("display.width", 220)
    print(f"split in-sample < {split} <= out-of-sample | {len(table)} reglages testes")
    print(table.head(args.top).to_string(index=False))
    print("\nLes 5 pires:")
    print(table.tail(5).to_string(index=False))
    best = table.iloc[0]
    print("\nMeilleur reglage (verifier le decay: proche de 0 = pas de curve fitting):")
    print(json.dumps(json.loads(best.to_json()), indent=2, default=str))
    print(f"\nHeures retenues par learn_from_is: {sorted(is_hours)}")
    return 0
