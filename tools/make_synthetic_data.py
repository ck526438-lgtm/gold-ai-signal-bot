"""Génère un historique M1 XAUUSD SYNTHÉTIQUE (pour tester le pipeline sans MT5).

⚠️ Données aléatoires : aucun résultat de backtest sur ces données n'a de valeur de trading.

    python tools/make_synthetic_data.py --months 10 --out data/synthetic_m1.csv
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def make_m1(months: int = 10, start: str = "2024-01-01", seed: int = 7, price: float = 2050.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, pd.Timestamp(start) + pd.DateOffset(months=months), freq="1min", tz="UTC", inclusive="left")
    idx = idx[idx.dayofweek < 5]
    n = len(idx)
    hour = idx.hour.values
    session_vol = np.where((hour >= 12) & (hour < 17), 1.8, np.where((hour >= 7) & (hour < 12), 1.3, 0.7))
    regime = np.repeat(rng.normal(0, 0.000004, n // 5000 + 1), 5000)[:n]
    rets = regime + rng.standard_t(4, n) * 0.00012 * session_vol
    close = price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[price], close[:-1]])
    wick = np.abs(rng.normal(0, 0.00012, n)) * close * session_vol
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.00012, n)) * close * session_vol
    vol = (rng.poisson(60, n) * session_vol).astype(float) + 1
    spread = np.clip(rng.normal(22, 4, n) / np.sqrt(session_vol), 12, 60).round()
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "tick_volume": vol, "spread": spread}, index=idx
    ).rename_axis("time")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=10)
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--out", default="data/synthetic_m1.csv")
    args = ap.parse_args()
    df = make_m1(args.months, args.start)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    df.round(3).to_csv(args.out)
    print(f"{len(df):,} bougies M1 écrites dans {args.out}")


if __name__ == "__main__":
    main()
