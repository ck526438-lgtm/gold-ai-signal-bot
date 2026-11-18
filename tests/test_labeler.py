import numpy as np

from src.ml.labeler import triple_barrier_labels, uniqueness_weights
from tests.conftest import make_bars


def _base(n=40):
    return list(2000 + np.sin(np.arange(n)) * 0.5)  # ATR stable ~1


def test_upper_barrier_gives_plus_one():
    closes = _base() + [2000.5, 2002, 2004, 2006, 2008] + [2008] * 15
    lab = triple_barrier_labels(make_bars(closes, wick=0.1), atr_mult=1.5, horizon=15)
    assert lab["label"].iloc[40] == 1


def test_lower_barrier_gives_minus_one():
    closes = _base() + [1999.5, 1998, 1996, 1994, 1992] + [1992] * 15
    lab = triple_barrier_labels(make_bars(closes, wick=0.1), atr_mult=1.5, horizon=15)
    assert lab["label"].iloc[40] == -1


def test_timeout_gives_zero():
    closes = _base() + [2000.0] * 30
    lab = triple_barrier_labels(make_bars(closes, wick=0.05), atr_mult=1.5, horizon=15)
    assert lab["label"].iloc[45] == 0
    assert lab["t1"].iloc[45] == 45 + 15


def test_same_bar_both_barriers_is_ambiguous():
    closes = _base() + [2000.0] + [2000.0] * 20
    df = make_bars(closes, wick=0.05)
    df.iloc[41, df.columns.get_loc("high")] = 2010
    df.iloc[41, df.columns.get_loc("low")] = 1990
    assert triple_barrier_labels(df, 1.5, 15)["label"].iloc[40] == 0


def test_last_horizon_bars_unlabeled():
    lab = triple_barrier_labels(make_bars(_base(60)), 1.5, 15)
    assert lab["label"].iloc[-15:].isna().all()


def test_weights_below_one_and_decrease_with_overlap():
    n = 40
    isolated = np.full(n, -1)
    isolated[0] = 5
    w_iso = uniqueness_weights(isolated, n)
    assert w_iso[0] == 1.0  # aucun chevauchement

    t1_low = np.full(n, -1); t1_low[[0, 10]] = [5, 15]           # pas de chevauchement
    t1_mid = np.full(n, -1); t1_mid[[0, 3]] = [5, 8]             # chevauchement partiel
    t1_high = np.full(n, -1); t1_high[[0, 1, 2]] = [5, 6, 7]     # fort chevauchement
    w_low, w_mid, w_high = (uniqueness_weights(t, n)[0] for t in (t1_low, t1_mid, t1_high))
    assert w_low == 1.0 > w_mid > w_high


def test_real_data_weights(m5_data):
    lab = triple_barrier_labels(m5_data, 1.5, 15).dropna()
    w = lab["sample_weight"]
    assert ((w > 0) & (w <= 1)).all() and (w < 1).mean() > 0.9
    assert set(lab["label"].unique()) <= {-1, 0, 1}
