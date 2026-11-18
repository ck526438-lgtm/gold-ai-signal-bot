"""Walk-Forward Analysis.

    python walk_forward.py                              # stratégie de config.yaml
    python walk_forward.py --strategy trend_following
    python walk_forward.py --strategy ml_xgboost        # réentraîne le ML à chaque fenêtre + compare aux règles
    python walk_forward.py --strategy all --csv data/xauusd_m1.csv

Sorties : reports/wfa/<stratégie>/wfa_results.csv, wfa_report.md, wfa_equity.png
          reports/wfa/wfa_ml_comparison.md (mode ml_xgboost)
"""
import argparse
import sys

from src.config import load_config
from src.logging_setup import setup_logging
from src.wfa import run_wfa_suite

RULES = ["mean_reversion", "trend_following"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Walk-Forward Analysis XAUUSD")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--strategy", choices=RULES + ["ml_xgboost", "all"])
    ap.add_argument("--csv")
    ap.add_argument("--max-bars", type=int)
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.csv:
        cfg["backtest"]["data_csv"] = args.csv
    if args.max_bars:
        cfg["wfa"]["max_bars"] = args.max_bars
    setup_logging(cfg)
    name = args.strategy or cfg["strategy"]["name"]
    if name == "ml_xgboost":
        strategies = ["ml_xgboost", *RULES]   # le ML est toujours comparé aux règles
    elif name == "all":
        strategies = [*RULES, "ml_xgboost"]
    else:
        strategies = [name]
    out = run_wfa_suite(cfg, strategies)
    for k, r in out["results"].items():
        print(f"{k:16s} verdict={r['verdict']:6s} fenêtres OK={r['n_pass']}/{r['n_windows']} Sharpe OOS={r['oos_sharpe']}")
    print(f"Rapports : {out['dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
