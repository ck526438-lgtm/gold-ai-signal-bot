import pandas as pd

from src.wfa import make_windows, param_grid, run_wfa, subset, write_wfa_outputs
from tools.make_synthetic_data import make_m1


def test_windows_rolling():
    idx = pd.date_range("2024-01-01", "2025-02-28", freq="1h", tz="UTC")
    ws = make_windows(idx, 6, 2, 2)
    assert len(ws) == 4  # jan-juin→juil-août, mars-août→sept-oct, mai-oct→nov-déc, juil-déc→jan-fév
    for w in ws:
        assert w.train_end == w.test_start and w.train_start < w.train_end < w.test_end
    assert ws[1].train_start == ws[0].train_start + pd.DateOffset(months=2)


def test_subset_and_grid():
    df = pd.DataFrame({"a": range(10)})
    assert len(subset(df, 4)) == 4 and subset(df, 4)["a"].iloc[0] == 6
    assert len(param_grid({"x": [1, 2], "y": [3, 4, 5]})) == 6


def test_wfa_end_to_end_rules(cfg, tmp_path):
    from src.data_feed import resample

    raw = make_m1(months=9, seed=3)
    bars = resample(raw, "M5")
    cfg["wfa"]["grids"]["trend_following"] = {"ema_fast": [9], "ema_slow": [21], "atr_threshold": [0.5, 1.0]}
    cfg["wfa"]["min_trades_train"] = 5
    res = run_wfa(cfg, "trend_following", bars)
    assert res["n_windows"] >= 1 and res["verdict"] in ("GO", "NO-GO")
    files = write_wfa_outputs(cfg, res, tmp_path / "wfa")
    md = open(files["md"], encoding="utf-8").read()
    assert "Verdict" in md and "ne garantit PAS" in md
    table = pd.read_csv(files["csv"])
    assert {"params", "sharpe_is", "sharpe_oos", "max_dd_oos_pct", "trades_oos"} <= set(table.columns)
    # le test OOS commence exactement à la fin du train (aucun chevauchement)
    if len(res["trades"]):
        first_test = pd.to_datetime(table["test_start"].iloc[0], utc=True)
        assert (pd.to_datetime(res["trades"]["open_time"], utc=True) >= first_test).all()
