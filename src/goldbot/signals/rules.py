"""Version 1 du signal: moteur de regles pondere, vectorise.

Chaque famille calcule une direction nette dans [-1, +1]; la somme ponderee
donne un score 0-100 (50 = neutre). Des portes dures suppriment le signal quand
le cout de transaction mangerait l'edge: spread large, ATR trop petit, approche
de la coupure 21:00 UTC.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from ..config import get
from ..features import compute_features

EPS = 1e-9
COMPONENTS = ["trend", "momentum", "mean_reversion", "volatility", "price_action", "session"]


def _clip(x):
    return np.clip(x, -1.0, 1.0)


def _comp_trend(f: pd.DataFrame) -> pd.Series:
    e20, e50, e200 = f["ema_20"], f["ema_50"], f["ema_200"]
    align = (np.sign(e20 - e50) + np.sign(e50 - e200) + np.sign(f["close"] - e200)) / 3.0
    slope = _clip(f["slope_ema_20"] / 0.06)
    di = _clip((f["plus_di"] - f["minus_di"]) / (f["plus_di"] + f["minus_di"] + EPS))
    gate = _clip(f["adx"] / 25.0)
    return _clip(0.5 * align + 0.2 * slope + 0.3 * di) * gate


def _comp_momentum(f: pd.DataFrame) -> pd.Series:
    rsi_c = _clip((f["rsi"] - 50) / 25)
    macd_c = _clip(f["macd_hist"] / (f["atr"] * 0.12 + EPS))
    rsi_slope = _clip(f["rsi_slope"] / 8)
    stoch = _clip((f["stoch_k"] - f["stoch_d"]) / 12)
    net = 0.35 * rsi_c + 0.30 * macd_c + 0.15 * rsi_slope + 0.20 * stoch
    # Au-dela de RSI 78 / 22, le meme mouvement est de l'epuisement, pas du carburant.
    extreme = (_clip((f["rsi"] - 78) / 12).clip(lower=0) + _clip((22 - f["rsi"]) / 12).clip(lower=0)).clip(0, 1)
    return net * (1 - 0.7 * extreme)


def _comp_mean_reversion(f: pd.DataFrame) -> pd.Series:
    range_w = _clip((22 - f["adx"]) / 10).clip(0, 1)
    bb = _clip(f["bb_z"] / 2.2)
    vw = _clip(f["vwap_atr"] / 2.0)
    return -_clip(0.6 * bb + 0.4 * vw) * range_w


def _comp_volatility(f: pd.DataFrame) -> pd.Series:
    expansion = _clip((f["vol_regime"] - 1.0) / 0.6).clip(0, 1)
    breakout = _clip((f["donchian_pos"] - 0.5) * 2)
    push = np.sign(f["ret_6"].fillna(0)) * _clip(f["ret_6"].abs() / 0.18)
    squeeze = (_clip((0.25 - f["bb_width_pct"]) / 0.25).clip(0, 1) * _clip(f["volume_z"] / 2).clip(0, 1.5))
    return _clip(0.55 * breakout * expansion + 0.45 * push * (0.4 + squeeze))


def _comp_price_action(f: pd.DataFrame) -> pd.Series:
    reject = _clip(2 * (f["lower_wick"] - f["upper_wick"]))
    loc = _clip(2 * (f["close_loc"] - 0.5))
    range_w = _clip((22 - f["adx"]) / 10).clip(0, 1)
    exhaustion = -np.sign(f["consec_closes"]) * _clip(f["consec_closes"].abs() / 6) * range_w
    return _clip(0.4 * reject + 0.35 * loc + 0.25 * exhaustion)


def _comp_session(f: pd.DataFrame) -> pd.Series:
    bias = 0.55 * f["session_overlap"] + 0.30 * f["session_london"] - 0.45 * f["session_asia"]
    reopen = ((f["dow"] == 6) & (f["min_since_open"] < 90)).astype(float)
    return _clip(bias - 0.6 * reopen)


def gates(f: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Portes dures: pas de signal, meme bon, quand le cout detruit l'edge."""
    point = float(get(cfg, "broker.point", 0.01))
    reason = pd.Series("", index=f.index, dtype="object")

    reason = reason.mask(f["atr"].isna() | f["adx"].isna() | f["ema_200"].isna(), "warmup")
    if bool(get(cfg, "signal.session.enabled", True)):
        max_spread = float(get(cfg, "signal.session.max_spread_points", 45))
        min_multiple = float(get(cfg, "signal.session.min_atr_spread_multiple", 1.8))
        spread_price = f["spread_points"] * point
        # Un ATR a 1.20 USD avec 0.33 USD de spread aller-retour: 27% du
        # mouvement part chez le broker avant meme de discuter la direction.
        reason = reason.mask(f["spread_points"] > max_spread, "spread")
        reason = reason.mask(f["atr"] < spread_price * min_multiple, "atr_vs_spread")
        reason = reason.mask(f["min_to_break"] < 15, "pre_break")
    return pd.DataFrame({"allowed": reason.eq(""), "block_reason": reason})


def _apply_cooldown(flags: pd.Series, bars: int) -> pd.Series:
    """Garder un feu et supprimer ceux des `bars` bougies suivantes."""
    out = pd.Series(False, index=flags.index)
    last_kept = -10**9
    for pos in np.flatnonzero(flags.to_numpy()):
        if pos - last_kept >= bars:
            out.iloc[pos] = True
            last_kept = pos
    return out


def rule_frame(f: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    weights = get(cfg, "signal.rules.weights", {}) or {}
    total = sum(float(v) for v in weights.values()) or 100.0
    parts = {
        "trend": _comp_trend(f),
        "momentum": _comp_momentum(f),
        "mean_reversion": _comp_mean_reversion(f),
        "volatility": _comp_volatility(f),
        "price_action": _comp_price_action(f),
        "session": _comp_session(f),
    }
    out = f.copy()
    for name, series in parts.items():
        out[f"w_{name}"] = series * (float(weights.get(name, 0)) / total)
    out["net"] = sum(out[f"w_{name}"] for name in COMPONENTS)
    out["rules_score"] = np.round(50 + 50 * out["net"], 1)
    out["rules_side"] = np.where(out["net"] > 0, "BUY", np.where(out["net"] < 0, "SELL", "FLAT"))

    cols = out[[f"w_{n}" for n in COMPONENTS]]
    direction = np.sign(out["net"])
    out["agreeing"] = (cols.gt(0).sum(axis=1).where(direction > 0, 0) +
                       cols.lt(0).sum(axis=1).where(direction < 0, 0))
    # Sans colonne vertebrale directionnelle, les composants de retroussement
    # produisent un signal en permanence: trend ou volatility doit suivre.
    if bool(get(cfg, "signal.require_directional_backbone", True)):
        backbone = np.sign(cols["w_trend"]) == np.sign(out["net"])
        backbone |= np.sign(cols["w_volatility"]) == np.sign(out["net"])
        backbone &= out["net"] != 0
    else:
        backbone = True

    g = gates(out, cfg)
    out["allowed"] = g["allowed"]
    out["block_reason"] = g["block_reason"]
    min_score = float(get(cfg, "signal.min_score", 65))
    strong = out["rules_score"].ge(min_score) | out["rules_score"].le(100 - min_score)
    state = (
        out["allowed"] & strong
        & out["agreeing"].ge(int(get(cfg, "signal.min_agreeing_components", 4)))
        & pd.Series(backbone, index=out.index)
    )
    out["rules_state"] = state
    # Un signal = un FRONT, pas un etat. Les indicateurs sont autocorrelés: sans
    # ca le meme mouvement de tendance emet 30 signaux identiques et le
    # backtest croit torturer une frequence qu'il n'a pas.
    previous = state.shift(1).fillna(True).astype(bool)
    fire = state & ~previous
    cooldown = int(get(cfg, "signal.cooldown_bars", 6))
    out["rules_fire"] = _apply_cooldown(fire, max(1, cooldown)) if cooldown > 1 else fire
    return out


def evaluate(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Bars brutes -> bars + features + scores regles."""
    f = df if "atr" in df.columns else compute_features(df, cfg)
    return rule_frame(f, cfg)


@dataclass
class Signal:
    time: pd.Timestamp
    symbol: str
    side: str
    score: float
    entry: float
    atr: float
    reasons: dict = field(default_factory=dict)
    sl: Optional[float] = None
    tp: Optional[float] = None
    rr: Optional[float] = None
    engine: str = "rules"
    probabilities: Optional[dict] = None
    block_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "time": self.time.isoformat(),
            "symbol": self.symbol,
            "engine": self.engine,
            "side": self.side,
            "score": self.score,
            "entry": round(self.entry, 2),
            "sl": None if self.sl is None else round(self.sl, 2),
            "tp": None if self.tp is None else round(self.tp, 2),
            "rr": None if self.rr is None else round(self.rr, 2),
            "atr": round(self.atr, 3),
            "block_reason": self.block_reason,
            "reasons": {k: round(v, 4) for k, v in self.reasons.items()},
            "probabilities": self.probabilities,
        }


def rules_signal(row: pd.Series, symbol: str, cfg: dict) -> Signal:
    from ..risk import bracket

    sig = Signal(
        time=row["time"], symbol=symbol, side=row["rules_side"], score=float(row["rules_score"]),
        entry=float(row["close"]), atr=float(row["atr"]),
        reasons={name: float(row[f"w_{name}"]) for name in COMPONENTS},
        block_reason=str(row["block_reason"]),
    )
    if sig.side != "FLAT" and row["allowed"]:
        sig.sl, sig.tp, sig.rr = bracket(sig.entry, sig.side, sig.atr, cfg)
    return sig
