"""Backtest d'une stratégie sur l'historique MT5 (ou un CSV).

    python backtest.py                          # stratégie de config.yaml
    python backtest.py --strategy mean_reversion
    python backtest.py --csv data/xauusd_m1.csv
"""
import argparse
import json
import sys

from src.config import load_config
from src.logging_setup import setup_logging
from src.runner import backtest_strategy


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest XAUUSD")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--strategy", choices=["mean_reversion", "trend_following", "ml_xgboost"])
    ap.add_argument("--csv", help="CSV M1 (time,open,high,low,close,tick_volume,spread)")
    ap.add_argument("--start")
    ap.add_argument("--end")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.csv:
        cfg["backtest"]["data_csv"] = args.csv
    if args.start:
        cfg["backtest"]["start"] = args.start
    if args.end:
        cfg["backtest"]["end"] = args.end
    setup_logging(cfg)
    res = backtest_strategy(cfg, args.strategy)
    print(json.dumps(res["metrics"], indent=2, ensure_ascii=False))
    print(f"Rapports : {res['output_dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
