"""Métriques de performance : winrate, profit factor, Sharpe, max drawdown…"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def sharpe_ratio(equity: pd.Series, periods_per_year: int = 252) -> float:
    """Sharpe annualisé sur les rendements JOURNALIERS de la courbe d'equity (taux sans risque = 0)."""
    if equity is None or len(equity) < 2:
        return 0.0
    daily = equity.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    if len(rets) < 5:
        return 0.0
    sd = rets.std(ddof=1)
    if not np.isfinite(sd) or sd == 0:
        return 0.0
    return float(rets.mean() / sd * math.sqrt(periods_per_year))


def max_drawdown_pct(equity: pd.Series) -> float:
    if equity is None or len(equity) == 0:
        return 0.0
    peak = equity.cummax()
    dd = (equity - peak) / peak
    return float(-dd.min() * 100.0)


def compute_metrics(trades: pd.DataFrame, equity: pd.Series, initial_capital: float) -> dict:
    n = int(len(trades))
    if n:
        pnl = trades["pnl"].astype(float)
        wins, losses = pnl[pnl > 0], pnl[pnl < 0]
        gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
        pf = gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0)
        winrate = len(wins) / n * 100.0
        avg_r = float(trades["r_multiple"].mean()) if "r_multiple" in trades else 0.0
        net = float(pnl.sum())
    else:
        pf, winrate, avg_r, net = 0.0, 0.0, 0.0, 0.0
    final = float(equity.iloc[-1]) if len(equity) else initial_capital
    return {
        "trades": n,
        "winrate_pct": round(winrate, 2),
        "profit_factor": round(pf, 3) if math.isfinite(pf) else "inf",
        "sharpe": round(sharpe_ratio(equity), 3),
        "max_drawdown_pct": round(max_drawdown_pct(equity), 2),
        "net_profit": round(net, 2),
        "return_pct": round((final / initial_capital - 1) * 100.0, 2) if initial_capital else 0.0,
        "avg_r": round(avg_r, 3),
        "final_equity": round(final, 2),
    }


def format_metrics(m: dict) -> str:
    return (
        f"Trades: {m['trades']} | Winrate: {m['winrate_pct']}% | PF: {m['profit_factor']} | "
        f"Sharpe: {m['sharpe']} | MaxDD: {m['max_drawdown_pct']}% | Net: {m['net_profit']}$ "
        f"({m['return_pct']}%) | R moyen: {m['avg_r']}"
    )
