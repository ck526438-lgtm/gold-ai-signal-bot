"""Backtester événementiel réaliste.

Règles anti look-ahead (vérifiées par tests/test_no_lookahead.py) :
  1. Le signal est calculé sur la bougie N CLÔTURÉE (indicateurs causaux).
  2. L'entrée a lieu à l'OUVERTURE de N+1, jamais au close de N.
  3. SL/TP sont testés avec le high/low des bougies suivantes ; si les deux sont
     touchés dans la même bougie, on suppose le SL (hypothèse pessimiste).

Coûts simulés : spread (colonne `spread` MT5 en points, sinon config), slippage
défavorable à l'entrée et sur les stops, commission par lot aller-retour.
Les prix MT5 sont BID : un achat paie l'ASK = bid + spread.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .filters import EntryFilters
from .metrics import compute_metrics
from .risk_manager import RiskManager
from .strategy import BaseStrategy, Signal


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series
    metrics: dict


def run_backtest(
    strategy: BaseStrategy,
    df: pd.DataFrame,
    cfg: dict,
    trade_start: Optional[pd.Timestamp] = None,
    trade_end: Optional[pd.Timestamp] = None,
    prepared: Optional[pd.DataFrame] = None,
    initial_capital: Optional[float] = None,
) -> BacktestResult:
    bt = cfg["backtest"]
    capital = float(initial_capital if initial_capital is not None else cfg["account"]["initial_capital"])
    point = float(bt["point"])
    cs = float(bt["contract_size"])
    comm = float(bt["commission_per_lot"])
    slip = float(bt["slippage_points"]) * point
    vmin, vstep, vmax = float(bt["volume_min"]), float(bt["volume_step"]), float(bt["volume_max"])

    d = prepared if prepared is not None else strategy.prepare(df)
    idx = d.index
    n = len(d)
    o = d["open"].to_numpy(float)
    h = d["high"].to_numpy(float)
    lo = d["low"].to_numpy(float)
    c = d["close"].to_numpy(float)
    atr = d["atr"].to_numpy(float)
    sig = d["signal"].fillna(0).to_numpy(int)
    reasons = d["reason"].to_numpy(object) if "reason" in d else np.full(n, "")
    conf = d["confidence"].to_numpy(float) if "confidence" in d else np.full(n, np.nan)
    spread_pts = d["spread"].to_numpy(float) if "spread" in d else np.full(n, np.nan)
    spread_pts = np.where(np.isfinite(spread_pts), spread_pts, float(bt["spread_points"]))
    spread = spread_pts * point

    filters = EntryFilters(cfg)
    allowed = filters.mask(idx, spread_pts, atr)
    start_i = 0 if trade_start is None else int(idx.searchsorted(pd.Timestamp(trade_start)))
    end_i = n if trade_end is None else int(idx.searchsorted(pd.Timestamp(trade_end)))

    risk = RiskManager(cfg, verbose=False)
    equity = capital
    pos = None
    pending = None
    trades: list[dict] = []
    eq_vals = np.full(n, np.nan)

    for i in range(n):
        day = idx[i].date()
        risk.new_day_if_needed(day, equity)

        # 1) exécution de l'ordre en attente à l'OUVERTURE de la bougie i
        if pending is not None and pos is None:
            dirn, sl_dist, reason, cf, sp = pending
            entry = o[i] + sp + slip if dirn > 0 else o[i] - slip
            vol = risk.compute_volume(equity, sl_dist, cs, vmin, vstep, vmax, comm)
            if vol > 0:
                pos = {
                    "dir": dirn, "entry": entry, "vol": vol, "sl": entry - dirn * sl_dist,
                    "tp": entry + dirn * sl_dist * risk.rr, "sl_dist": sl_dist, "i": i,
                    "risk": vol * (sl_dist * cs + comm), "reason": reason, "conf": cf, "sp": sp,
                }
            pending = None

        # 2) gestion de la position pendant la bougie i
        if pos is not None:
            sp = spread[i]
            exit_price = None
            if pos["dir"] > 0:  # sortie au BID
                if lo[i] <= pos["sl"]:
                    exit_price = min(o[i], pos["sl"]) - slip if o[i] < pos["sl"] else pos["sl"] - slip
                elif h[i] >= pos["tp"]:
                    exit_price = max(o[i], pos["tp"]) if o[i] > pos["tp"] else pos["tp"]
            else:  # sortie à l'ASK = bid + spread
                if h[i] + sp >= pos["sl"]:
                    exit_price = (o[i] + sp + slip) if o[i] + sp > pos["sl"] else pos["sl"] + slip
                elif lo[i] + sp <= pos["tp"]:
                    exit_price = (o[i] + sp) if o[i] + sp < pos["tp"] else pos["tp"]
            if exit_price is not None:
                pnl = (exit_price - pos["entry"]) * pos["dir"] * cs * pos["vol"] - comm * pos["vol"]
                equity += pnl
                trades.append({
                    "open_time": idx[pos["i"]], "close_time": idx[i], "side": "BUY" if pos["dir"] > 0 else "SELL",
                    "volume": pos["vol"], "entry_price": round(pos["entry"], 3), "exit_price": round(exit_price, 3),
                    "sl": round(pos["sl"], 3), "tp": round(pos["tp"], 3), "pnl": round(pnl, 2),
                    "r_multiple": round(pnl / pos["risk"], 3) if pos["risk"] else 0.0,
                    "bars": i - pos["i"] + 1, "reason": pos["reason"], "confidence": pos["conf"],
                })
                risk.register_trade_result(pnl, equity)
                pos = None
            else:
                price = c[i] if pos["dir"] > 0 else c[i] + sp
                new_sl = risk.trailing_sl(pos["dir"], pos["entry"], pos["sl"], pos["sl_dist"], price, atr[i])
                if new_sl is not None:
                    pos["sl"] = new_sl

        # 3) equity marquée au close (pour Sharpe / drawdown)
        mark = equity
        if pos is not None:
            px = c[i] if pos["dir"] > 0 else c[i] + spread[i]
            mark += (px - pos["entry"]) * pos["dir"] * cs * pos["vol"]
        eq_vals[i] = mark
        risk.update_equity(mark, day)

        # 4) signal à la CLÔTURE de i -> ordre pour l'ouverture de i+1
        if (
            pos is None and pending is None and start_i <= i < end_i - 1 and sig[i] != 0
            and allowed[i] and not risk.locked and np.isfinite(atr[i])
        ):
            pending = (int(sig[i]), risk.sl_mult * atr[i], reasons[i], conf[i], spread[i])

    # position encore ouverte en fin de période : clôturée au dernier close
    if pos is not None and n:
        px = c[-1] if pos["dir"] > 0 else c[-1] + spread[-1]
        pnl = (px - pos["entry"]) * pos["dir"] * cs * pos["vol"] - comm * pos["vol"]
        equity += pnl
        trades.append({
            "open_time": idx[pos["i"]], "close_time": idx[-1], "side": "BUY" if pos["dir"] > 0 else "SELL",
            "volume": pos["vol"], "entry_price": round(pos["entry"], 3), "exit_price": round(px, 3),
            "sl": round(pos["sl"], 3), "tp": round(pos["tp"], 3), "pnl": round(pnl, 2),
            "r_multiple": round(pnl / pos["risk"], 3) if pos["risk"] else 0.0,
            "bars": n - pos["i"], "reason": pos["reason"] + " (fin de période)", "confidence": pos["conf"],
        })
        eq_vals[-1] = equity

    eq = pd.Series(eq_vals, index=idx, name="equity").iloc[start_i:end_i].dropna()
    tdf = pd.DataFrame(trades)
    return BacktestResult(trades=tdf, equity=eq, metrics=compute_metrics(tdf, eq, capital))


def signal_from_row(d: pd.DataFrame, i: int) -> Optional[Signal]:
    row = d.iloc[i]
    if int(row.get("signal", 0)) == 0:
        return None
    return Signal(int(row["signal"]), d.index[i], float(row["close"]), float(row["atr"]), str(row.get("reason", "")))
