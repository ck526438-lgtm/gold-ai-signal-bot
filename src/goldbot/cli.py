"""Command line entry: python bot.py <command>."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .backtest import cmd_backtest
from .config import get, load_config
from .data import make_provider
from .data.bars import sanity_report
from .data.history import fetch_history, load_cache, save_cache

def cmd_fetch(cfg: dict, args) -> int:
    provider = make_provider(cfg)
    info = provider.connect()
    print("connection:", json.dumps(info, indent=2, default=str))
    tf = args.timeframe or str(get(cfg, "symbol.timeframe", "M5"))
    df = fetch_history(cfg, provider, tf, args.days, chunk_days=args.chunk_days)
    path = save_cache(cfg, provider.symbol, tf, df)
    print("cache:", path)
    print("rapport:", json.dumps(sanity_report(df, tf), indent=2))
    provider.shutdown()
    return 0


def cmd_doctor(cfg: dict, args) -> int:
    tf = str(get(cfg, "symbol.timeframe", "M5"))
    report = {"python": sys.version.split()[0], "now_utc": datetime.now(timezone.utc).isoformat(),
              "provider_mode": get(cfg, "data.provider"), "timeframe": tf}
    try:
        provider = make_provider(cfg)
        report["connect"] = provider.connect()
        report["tick"] = provider.tick()
        report["resolved_symbol"] = provider.symbol
        provider.shutdown()
    except Exception as exc:
        report["connect_error"] = f"{type(exc).__name__}: {exc}"
    cached = load_cache(cfg, report.get("resolved_symbol") or "XAUUSD", tf)
    report["cache"] = sanity_report(cached, tf) if len(cached) else {"bars": 0}
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_check(cfg: dict, args) -> int:
    """Valide l'integrite des donnees et l'absence de look-ahead dans les features."""
    from .features import FEATURE_COLS, compute_features
    from .data.history import load_cache

    tf = str(get(cfg, "symbol.timeframe", "M5"))
    df = load_cache(cfg, args.symbol, tf)
    if df.empty:
        print("Cache vide: lancer `python bot.py fetch` d'abord.")
        return 1
    f = compute_features(df, cfg)

    cut = max(300, len(df) - args.tail)
    sub = compute_features(df.iloc[:cut].reset_index(drop=True), cfg).iloc[: cut - 1]
    ref = f.iloc[: cut - 1]
    leaks = []
    for col in FEATURE_COLS:
        if col not in sub.columns:
            continue
        a, b = sub[col].to_numpy(dtype="float64"), ref[col].to_numpy(dtype="float64")
        if not np.allclose(a, b, equal_nan=True, rtol=1e-9, atol=1e-9):
            leaks.append(col)

    horizon = int(get(cfg, "signal.ml.horizon_bars", 12))
    usable = f.iloc[:-horizon]
    report = {
        "bars": int(len(f)),
        "look_ahead_leaks": leaks,
        "features_missing": [c for c in FEATURE_COLS if c not in f.columns],
        "nan_share_by_feature": {
            c: round(float(f[c].isna().mean()), 4) for c in FEATURE_COLS if c in f.columns and f[c].isna().any()
        },
        "label_distribution": {
            "up": round(float((f["fwd_ret"] > 0).mean()), 4),
            "flat_or_nan": round(float(f["fwd_ret"].isna().mean()), 6),
        },
        "spread_points_stats": {
            "median": round(float(f["spread_points"].median()), 1),
            "p95": round(float(f["spread_points"].quantile(0.95)), 1),
            "max": round(float(f["spread_points"].max()), 1),
        },
        "bars_in_break_window": int(((f["time"].dt.hour == 22)).sum()),
        "min_to_break_zero_bars": int((f["min_to_break"] <= 5).sum()),
    }
    print(json.dumps(report, indent=2))
    return 0 if not leaks else 2


def _bars_for_signals(cfg: dict, provider=None):
    """Cache a jour puis bougies fermees uniquement."""
    from .data.history import refresh
    from .data.bars import closed_only

    tf = str(get(cfg, "symbol.timeframe", "M5"))
    if provider is None:
        provider = make_provider(cfg)
        provider.connect()
    df = refresh(cfg, provider, tf)
    df = closed_only(df, tf)
    return provider, tf, df


def _signals_per_day(scored) -> float:
    """Nombre de signaux par journee de bourse (23:00 -> 21:00 UTC)."""
    day = (scored["time"] + pd.Timedelta(hours=1)).dt.date
    return round(float(scored.groupby(day)["rules_fire"].sum().mean()), 2)


def cmd_signal(cfg: dict, args) -> int:
    from .data.history import load_cache
    from .signals import evaluate, rules_signal

    tf = str(get(cfg, "symbol.timeframe", "M5"))
    provider = None
    if args.live:
        provider, tf, df = _bars_for_signals(cfg)
    else:
        df = load_cache(cfg, args.symbol or "XAUUSD", tf)
    scored = evaluate(df, cfg)

    last = scored.iloc[-1]
    sig = rules_signal(last, last.get("symbol", args.symbol or "XAUUSD"), cfg)
    recent = scored.tail(args.bars)

    summary = {
        "last_closed_bar": str(last["time"]),
        "price": round(float(last["close"]), 2),
        "atr": round(float(last["atr"]), 3),
        "spread_points": round(float(last["spread_points"]), 1),
        "verdict": sig.as_dict(),
        "fires_now": bool(last["rules_fire"]),
        f"signals_last_{args.bars}_bars": int(recent["rules_fire"].sum()),
        "fire_rate_all_history": round(float(scored["rules_fire"].mean()), 4),
        "signals_per_trading_day": _signals_per_day(scored),
        "blocked_share": round(float((~scored["allowed"]).mean()), 4),
        "block_reasons": scored.loc[~scored["allowed"], "block_reason"].value_counts().head(6).to_dict(),
        "fired_side_split": scored.loc[scored["rules_fire"], "rules_side"].value_counts().to_dict(),
        "score_distribution": {
            "p05": round(float(scored["rules_score"].quantile(0.05)), 1),
            "median": round(float(scored["rules_score"].median()), 1),
            "p95": round(float(scored["rules_score"].quantile(0.95)), 1),
        },
    }
    print(json.dumps(summary, indent=2, default=str))

    if args.history:
        fired = scored.loc[scored["rules_fire"]].tail(args.history)
        cols = ["time", "rules_side", "rules_score", "close", "atr"]
        print(fired[cols].to_string(index=False))
        print("\nContribution moyenne des composants sur les signaux emis:")
        print(fired[[f"w_{n}" for n in ("trend", "momentum", "mean_reversion",
                                         "volatility", "price_action", "session")]]
              .groupby(fired["rules_side"]).mean().round(4).to_string())
    if provider is not None:
        provider.shutdown()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bot.py", description="GOLD AI SIGNAL BOT - XAUUSD M5")
    parser.add_argument("--config", help="chemin config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="telecharger l'historique M5 dans le cache")
    p.add_argument("--days", type=int, default=400)
    p.add_argument("--chunk-days", type=int)
    p.add_argument("--timeframe")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("doctor", help="verifier MT5, symbole, tick et cache")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("check", help="verifier donnees + absence de look-ahead")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--tail", type=int, default=400, help="bougies gardees pour le test de fuite")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("signal", help="verdict regles sur la derniere bougie fermee")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--live", action="store_true", help="remet le cache a jour depuis MT5 avant")
    p.add_argument("--bars", type=int, default=12, help="fenetre de comptage des signaux")
    p.add_argument("--history", type=int, default=0, help="lister les N derniers signaux emis")
    p.set_defaults(func=cmd_signal)
    p = sub.add_parser("backtest", help="simuler les signaux regles sur le cache")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--engine", choices=["rules", "ml", "both"], help="override signal.engine")
    p.add_argument("--start", help="YYYY-MM-DD, ignore tout avant")
    p.add_argument("--end", help="YYYY-MM-DD, ignore tout apres")
    p.add_argument("--export", help="chemin CSV de sortie du journal de trades")
    p.set_defaults(func=cmd_backtest)

    from .live import cmd_live, cmd_paper
    from .signals.ml import cmd_train
    from .tune import cmd_compare, cmd_tune

    p = sub.add_parser("compare", help="confronter rules / ml / both sur les memes bougies")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--start", help="YYYY-MM-DD")
    p.add_argument("--export", action="store_true", help="ecrire data/trades_<engine>.csv")
    p.add_argument("--export-engine", default="both")
    p.add_argument("--seeds", type=int, default=5, help="tirages au hasard du temoin de direction")
    p.add_argument("--no-control", action="store_true", help="ne pas courir le temoin de direction")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("live", help="boucle temps reel sur chaque cloture M5")
    p.add_argument("--once", action="store_true", help="un seul cycle (pour cron)")
    p.add_argument("--delay", type=float, default=4.0, help="secondes apres la cloture")
    p.add_argument("--heartbeat", type=int, default=12, help="un etat papier toutes N bougies")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("paper", help="bilan du suivi papier journalise")
    p.set_defaults(func=cmd_paper)

    p = sub.add_parser("train", help="walk-forward du modele ML + entrainement final")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--months", type=int, default=3, help="taille d'un fold d'evaluation")
    p.add_argument("--no-save", action="store_true", help="ne pas ecrire le modele final")
    p.set_defaults(func=cmd_train)

    from .selftest import cmd_selftest

    p = sub.add_parser("selftest", help="verifier live == backtest == bridge")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--trades", type=int, default=8, help="signaux rejoues par moteur")
    p.add_argument("--port", type=int, default=8799)
    p.add_argument("--skip-bridge", action="store_true")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("tune", help="grille de reglage avec split in-sample/out-of-sample")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--split", help="date YYYY-MM-DD du passage IS -> OOS")
    p.add_argument("--scores", type=int, nargs="+", default=[66, 68, 70, 72])
    p.add_argument("--agreeing", type=int, default=4)
    p.add_argument("--min-trades", type=int, default=60)
    p.add_argument("--top", type=int, default=12)
    p.set_defaults(func=cmd_tune)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    return args.func(cfg, args)
