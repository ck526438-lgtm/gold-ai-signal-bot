"""Walk-Forward Analysis : optimisation sur 6 mois, test hors échantillon sur 2 mois, pas de 2 mois.

Rien de la fenêtre de test n'est vu pendant l'optimisation :
  * la grille est évaluée sur [train_start, train_end) uniquement (Sharpe, pas le PnL) ;
  * le test démarre à test_start (+ embargo pour le ML), avec un « warm-up »
    d'indicateurs pris dans le PASSÉ (bougies avant test_start), jamais dans le futur ;
  * en mode ML, le modèle est réentraîné à chaque fenêtre et ses labels sont calculés
    sur une tranche qui s'arrête à train_end (les labels qui débordent sont purgés).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd
from loguru import logger

from .backtester import run_backtest
from .config import resolve
from .metrics import compute_metrics, max_drawdown_pct, sharpe_ratio
from .strategy import build_strategy

WARMUP_BARS = {"mean_reversion": 300, "trend_following": 400, "ml_xgboost": 700}


@dataclass
class Window:
    k: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def make_windows(index: pd.DatetimeIndex, train_m: int, test_m: int, step_m: int) -> list[Window]:
    first = index[0].normalize()
    last = index[-1]
    windows, k, start = [], 1, first
    while True:
        tr_end = start + pd.DateOffset(months=train_m)
        te_end = tr_end + pd.DateOffset(months=test_m)
        if te_end > last + pd.Timedelta(days=1):
            break
        windows.append(Window(k, start, tr_end, tr_end, te_end))
        start = start + pd.DateOffset(months=step_m)
        k += 1
    return windows


def subset(df: pd.DataFrame, max_bars: int) -> pd.DataFrame:
    if max_bars and len(df) > max_bars:
        logger.warning(f"{len(df):,} bougies > max_bars={max_bars:,} → on garde les {max_bars:,} plus récentes")
        return df.iloc[-max_bars:]
    return df


def param_grid(grid: dict) -> list[dict]:
    if not grid:
        return [{}]
    keys = list(grid)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]


def _slice_with_warmup(bars: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp, warmup: int) -> pd.DataFrame:
    i0 = max(0, int(bars.index.searchsorted(start)) - warmup)
    i1 = int(bars.index.searchsorted(end))
    return bars.iloc[i0:i1]


def _valid_combo(name: str, combo: dict) -> bool:
    if name == "trend_following":
        return combo.get("ema_fast", 0) < combo.get("ema_slow", 1)
    return True


def optimize_rules(cfg, name, bars, w: Window, grid: dict, min_trades: int):
    base = build_strategy(cfg, name)
    data = _slice_with_warmup(bars, w.train_start, w.train_end, WARMUP_BARS[name])
    best, best_sharpe, best_res = None, -np.inf, None
    for combo in param_grid(grid):
        if not _valid_combo(name, combo):
            continue
        res = run_backtest(base.with_params(**combo), data, cfg, trade_start=w.train_start)
        if res.metrics["trades"] < min_trades:
            continue
        s = res.metrics["sharpe"]
        if s > best_sharpe:
            best, best_sharpe, best_res = combo, s, res
    return best, best_sharpe, best_res


def train_ml_window(cfg, bars, w: Window):
    """Réentraîne XGBoost sur la fenêtre train (validation interne = 20 % finaux, purgée)."""
    from .ml.predictor import Predictor
    from .ml.trainer import build_dataset, fit_model, temporal_split

    data = _slice_with_warmup(bars, w.train_start, w.train_end, WARMUP_BARS["ml_xgboost"])
    ds = build_dataset(data, cfg)  # labels incomplets en fin de tranche → NaN → retirés (purge naturelle)
    in_train = ds.pos.to_numpy() >= int(data.index.searchsorted(w.train_start))
    split_t = w.train_start + (w.train_end - w.train_start) * 0.8
    tr, va = temporal_split(ds, data.index, split_t, w.train_end, int(cfg["ml"]["purge_bars"]), int(cfg["ml"]["embargo_bars"]))
    tr &= in_train
    model, scaler = fit_model(cfg, ds.X[tr], ds.y[tr], ds.w[tr], ds.X[va], ds.y[va], ds.w[va])
    return Predictor(model, scaler, ds.features, {"window": w.k}), data


def run_wfa(
    cfg: dict, name: str, bars: pd.DataFrame, progress: Optional[Callable[[str], None]] = None
) -> dict:
    wcfg = cfg["wfa"]
    bars = subset(bars, int(wcfg.get("max_bars", 500000)))
    windows = make_windows(bars.index, int(wcfg["train_months"]), int(wcfg["test_months"]), int(wcfg["step_months"]))
    if not windows:
        raise ValueError(
            f"Historique trop court pour une WFA {wcfg['train_months']}+{wcfg['test_months']} mois "
            f"({bars.index[0]:%Y-%m-%d} → {bars.index[-1]:%Y-%m-%d})."
        )
    ratio, min_trades = float(wcfg["accept_ratio"]), int(wcfg["min_trades_train"])
    grid = (wcfg.get("grids") or {}).get(name, {})
    equity = float(cfg["account"]["initial_capital"])
    rows, eq_parts, all_trades = [], [], []
    embargo = int(cfg["ml"]["embargo_bars"]) if name == "ml_xgboost" else 0

    for w in windows:
        msg = f"[{name}] fenêtre {w.k}/{len(windows)} : train {w.train_start:%Y-%m-%d}→{w.train_end:%Y-%m-%d} | test →{w.test_end:%Y-%m-%d}"
        logger.info(msg)
        if progress:
            progress(msg)
        if name == "ml_xgboost":
            try:
                predictor, train_data = train_ml_window(cfg, bars, w)
            except ValueError as exc:
                logger.warning(f"Fenêtre {w.k} ignorée : {exc}")
                continue
            strat = build_strategy(cfg, name, predictor=predictor)
            res_is = run_backtest(strat, train_data, cfg, trade_start=w.train_start)
            params, sharpe_is = {"retrained": True}, res_is.metrics["sharpe"]
            is_trades = res_is.metrics["trades"]
        else:
            params, sharpe_is, res_is = optimize_rules(cfg, name, bars, w, grid, min_trades)
            if params is None:
                logger.warning(f"Fenêtre {w.k} : aucune combinaison avec >= {min_trades} trades")
                rows.append({"window": w.k, "train_start": w.train_start, "train_end": w.train_end,
                             "test_start": w.test_start, "test_end": w.test_end, "params": "aucun",
                             "sharpe_is": np.nan, "sharpe_oos": np.nan, "max_dd_oos_pct": np.nan,
                             "trades_is": 0, "trades_oos": 0, "net_oos": 0.0, "pass": False})
                continue
            strat = build_strategy(cfg, name).with_params(**params)
            is_trades = res_is.metrics["trades"]

        test_data = _slice_with_warmup(bars, w.test_start, w.test_end, WARMUP_BARS[name])
        t_start = w.test_start
        if embargo:
            pos = int(test_data.index.searchsorted(w.test_start)) + embargo
            t_start = test_data.index[min(pos, len(test_data) - 1)]
        res_oos = run_backtest(strat, test_data, cfg, trade_start=t_start, initial_capital=equity)
        if len(res_oos.equity):
            eq_parts.append(res_oos.equity)
            equity = float(res_oos.equity.iloc[-1])
        if len(res_oos.trades):
            all_trades.append(res_oos.trades.assign(window=w.k))
        s_oos = res_oos.metrics["sharpe"]
        passed = bool(sharpe_is > 0 and s_oos > ratio * sharpe_is)
        rows.append({
            "window": w.k, "train_start": w.train_start, "train_end": w.train_end,
            "test_start": w.test_start, "test_end": w.test_end, "params": str(params),
            "sharpe_is": round(sharpe_is, 3), "sharpe_oos": round(s_oos, 3),
            "max_dd_oos_pct": res_oos.metrics["max_drawdown_pct"], "trades_is": is_trades,
            "trades_oos": res_oos.metrics["trades"], "net_oos": res_oos.metrics["net_profit"], "pass": passed,
        })

    table = pd.DataFrame(rows)
    oos_equity = pd.concat(eq_parts) if eq_parts else pd.Series(dtype=float)
    oos_equity = oos_equity[~oos_equity.index.duplicated(keep="last")]
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    n_valid = int(len(table))
    n_pass = int(table["pass"].sum()) if n_valid else 0
    pct = n_pass / n_valid * 100 if n_valid else 0.0
    verdict = "GO" if n_valid and pct >= float(wcfg["accept_windows_pct"]) else "NO-GO"
    summary = compute_metrics(trades, oos_equity, float(cfg["account"]["initial_capital"])) if len(oos_equity) else {}
    return {
        "strategy": name, "table": table, "oos_equity": oos_equity, "trades": trades,
        "pass_pct": round(pct, 1), "n_windows": n_valid, "n_pass": n_pass, "verdict": verdict,
        "oos_sharpe": round(sharpe_ratio(oos_equity), 3) if len(oos_equity) else 0.0,
        "oos_max_dd": round(max_drawdown_pct(oos_equity), 2) if len(oos_equity) else 0.0,
        "summary": summary,
    }


DISCLAIMER = (
    "> ⚠️ La WFA ne garantit PAS la rentabilité future. Elle montre seulement l'absence de "
    "sur-optimisation flagrante sur l'historique testé."
)


def write_wfa_outputs(cfg: dict, result: dict, out_dir: Path) -> dict:
    from .runner import plot_equity

    out_dir.mkdir(parents=True, exist_ok=True)
    name = result["strategy"]
    csv_p, md_p, png_p = out_dir / "wfa_results.csv", out_dir / "wfa_report.md", out_dir / "wfa_equity.png"
    result["table"].to_csv(csv_p, index=False)
    if len(result["oos_equity"]):
        plot_equity(result["oos_equity"], png_p, f"WFA {name} — equity out-of-sample concaténée")
    w = cfg["wfa"]
    s = result.get("summary", {})
    lines = [
        f"# Walk-Forward Analysis — {name}",
        "",
        f"## Verdict : **{result['verdict']}**",
        "",
        f"- Fenêtres : {result['n_windows']} (train {w['train_months']} mois / test {w['test_months']} mois / pas {w['step_months']} mois)",
        f"- Critère : Sharpe OOS > {float(w['accept_ratio']):.0%} du Sharpe IS sur ≥ {w['accept_windows_pct']} % des fenêtres",
        f"- Fenêtres validées : {result['n_pass']}/{result['n_windows']} ({result['pass_pct']} %)",
        f"- Sharpe OOS concaténé : {result['oos_sharpe']} | Max DD OOS : {result['oos_max_dd']} %",
    ]
    if s:
        lines.append(f"- OOS cumulé : {s['trades']} trades, winrate {s['winrate_pct']} %, PF {s['profit_factor']}, net {s['net_profit']} $ ({s['return_pct']} %)")
        if s["net_profit"] <= 0:
            lines.append("- ⚠️ Le résultat OOS cumulé est négatif : même si le critère passe, ne pas trader.")
    lines += ["", "## Détail par fenêtre", "", _md_table(result["table"]), "", DISCLAIMER, ""]
    md_p.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"WFA {name} : {result['verdict']} ({result['pass_pct']} % des fenêtres) → {md_p}")
    return {"csv": str(csv_p), "md": str(md_p), "png": str(png_p)}


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_aucune fenêtre_"
    cols = ["window", "test_start", "params", "sharpe_is", "sharpe_oos", "max_dd_oos_pct", "trades_oos", "net_oos", "pass"]
    d = df[cols].copy()
    d["test_start"] = pd.to_datetime(d["test_start"]).dt.strftime("%Y-%m-%d")
    d["pass"] = d["pass"].map({True: "✅", False: "❌"})
    head = "| " + " | ".join(cols) + " |"
    sep = "|" + "---|" * len(cols)
    body = ["| " + " | ".join(str(v) for v in r) + " |" for r in d.itertuples(index=False)]
    return "\n".join([head, sep, *body])


def write_ml_comparison(cfg: dict, results: dict[str, dict], out_dir: Path) -> Path:
    margin = float(cfg["wfa"].get("ml_significance_margin", 0.3))
    ml = results.get("ml_xgboost")
    rules = {k: v for k, v in results.items() if k != "ml_xgboost"}
    best_rule = max(rules.values(), key=lambda r: r["oos_sharpe"]) if rules else None
    lines = ["# Comparaison ML vs règles (walk-forward, out-of-sample)", "",
             "| Stratégie | Sharpe OOS | Max DD OOS % | Trades OOS | Fenêtres OK | Verdict WFA |",
             "|---|---|---|---|---|---|"]
    for k, r in results.items():
        lines.append(f"| {k} | {r['oos_sharpe']} | {r['oos_max_dd']} | {r.get('summary', {}).get('trades', 0)} | "
                     f"{r['n_pass']}/{r['n_windows']} | {r['verdict']} |")
    lines.append("")
    if ml and best_rule:
        threshold = best_rule["oos_sharpe"] + margin
        wins = ml["oos_sharpe"] > threshold
        lines += [
            f"## Le ML bat-il significativement les règles ? **{'OUI' if wins else 'NON'}**", "",
            f"Test : Sharpe OOS ML ({ml['oos_sharpe']}) > meilleur Sharpe OOS règles "
            f"({best_rule['strategy']} = {best_rule['oos_sharpe']}) + {margin} = {threshold:.3f}", "",
            "**Recommandation** : " + (
                "le ML peut être retenu, à condition que son propre verdict WFA soit GO."
                if wins and ml["verdict"] == "GO" else
                "garder la stratégie à règles (plus simple, plus robuste) tant que le ML ne passe pas ce test."
            ),
        ]
    lines += ["", DISCLAIMER, ""]
    p = out_dir / "wfa_ml_comparison.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def run_wfa_suite(cfg: dict, strategies: list[str], raw: Optional[pd.DataFrame] = None, progress=None) -> dict:
    from .data_feed import load_history, resample
    from .runner import strategy_timeframe

    raw = raw if raw is not None else load_history(cfg)
    base = resolve(cfg["backtest"].get("output_dir", "reports")) / "wfa"
    results, files = {}, {}
    for name in strategies:
        bars = resample(raw, strategy_timeframe(cfg, name))
        results[name] = run_wfa(cfg, name, bars, progress)
        files[name] = write_wfa_outputs(cfg, results[name], base / name)
    if "ml_xgboost" in strategies and len(strategies) > 1:
        files["comparison"] = str(write_ml_comparison(cfg, results, base))
    return {"results": results, "files": files, "dir": str(base)}
