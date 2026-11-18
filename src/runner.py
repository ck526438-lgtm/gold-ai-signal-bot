"""Fonctions de haut niveau partagées par les scripts CLI et l'appli Windows."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from .backtester import run_backtest
from .config import resolve
from .data_feed import load_history, resample
from .metrics import format_metrics
from .strategy import build_strategy


def strategy_timeframe(cfg: dict, name: str) -> str:
    if name == "ml_xgboost":
        return cfg["strategy"].get("ml_xgboost", {}).get("timeframe", cfg["ml"].get("timeframe", "M5"))
    return cfg["strategy"][name].get("timeframe", "M5")


def load_bars(cfg: dict, timeframe: str, raw: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    raw = raw if raw is not None else load_history(cfg)
    logger.info(f"Historique chargé : {len(raw):,} bougies M1 ({raw.index[0]} → {raw.index[-1]})")
    return resample(raw, timeframe)


def plot_equity(equity: pd.Series, path: Path, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11, 4.5))
    ax.plot(equity.index, equity.values, color="#c9a227", linewidth=1.2)
    ax.set_title(title)
    ax.set_ylabel("Equity ($)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def backtest_strategy(cfg: dict, name: Optional[str] = None, raw: Optional[pd.DataFrame] = None) -> dict:
    name = name or cfg["strategy"]["name"]
    strat = build_strategy(cfg, name)
    bars = load_bars(cfg, strategy_timeframe(cfg, name), raw)
    res = run_backtest(strat, bars, cfg)
    out = resolve(cfg["backtest"].get("output_dir", "reports"))
    out.mkdir(parents=True, exist_ok=True)
    res.equity.to_csv(out / f"equity_{name}.csv", header=True)
    res.trades.to_csv(out / f"trades_backtest_{name}.csv", index=False)
    if len(res.equity):
        plot_equity(res.equity, out / f"equity_{name}.png", f"Backtest {name} — {cfg['symbol']}")
    logger.info(f"[{name}] " + format_metrics(res.metrics))
    return {"metrics": res.metrics, "output_dir": str(out), "equity": res.equity, "trades": res.trades}
