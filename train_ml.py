"""Entraîne le modèle XGBoost sur l'historique MT5 (ou un CSV) et le sauvegarde dans src/models/.

    python train_ml.py
    python train_ml.py --csv data/xauusd_m1.csv --train-end 2024-06-30 --val-end 2024-12-31
"""
import argparse
import json
import sys

from src.config import load_config
from src.data_feed import load_history, resample
from src.logging_setup import setup_logging
from src.ml.trainer import train


def main() -> int:
    ap = argparse.ArgumentParser(description="Entraînement XGBoost XAUUSD")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--csv")
    ap.add_argument("--train-end")
    ap.add_argument("--val-end")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.csv:
        cfg["backtest"]["data_csv"] = args.csv
    setup_logging(cfg)
    bars = resample(load_history(cfg), cfg["ml"].get("timeframe", "M5"))
    res = train(bars, cfg, args.train_end, args.val_end)
    print(json.dumps({k: {m: v for m, v in d.items() if m != "confusion_matrix"} for k, d in res.metrics.items()}, indent=2))
    print("Fichiers :", json.dumps(res.paths, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
