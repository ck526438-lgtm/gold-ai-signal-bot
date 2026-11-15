"""Contexte macro de l'or : indice du dollar (DXY) et taux américain à 10 ans.

L'or ne rapporte aucun intérêt et se cote en dollars : il monte en général quand le dollar et les
taux réels baissent, et baisse quand ils montent. On mesure la tendance H1 des deux et on l'ajoute
aux bougies M5, SANS donnée future : la bougie H1 de 10 h n'est utilisée qu'à partir de 11 h.

Sources : MetaTrader 5 si le courtier publie l'indice (USDX, DXY…), sinon Yahoo Finance
(DX-Y.NYB et ^TNX, gratuits). Le résultat est mis en cache 10 minutes.
"""
from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

SERIES = {
    "dxy": {"label": "Dollar (DXY)", "mt5": ("USDX", "DXY", "USDIndex", "DX", "DOLLAR_INDX", "USDOLLAR"),
            "yahoo": "DX-Y.NYB"},
    "us10y": {"label": "Taux US 10 ans", "mt5": ("US10Y", "UST10Y", "US10YR", "TNOTE", "UST10"),
              "yahoo": "^TNX"},
}
CACHE_TTL = 600
_cache: dict[str, tuple[float, pd.Series | None, str]] = {}


# --------------------------------------------------------------------------- #
def trend(close: pd.Series, fast: int = 20, slow: int = 50) -> pd.Series:
    """+1 hausse / −1 baisse / 0 neutre sur des bougies H1 (prix vs EMA 20 vs EMA 50)."""
    ef = close.ewm(span=fast, adjust=False).mean()
    es = close.ewm(span=slow, adjust=False).mean()
    up = (close > ef) & (ef > es)
    down = (close < ef) & (ef < es)
    out = pd.Series(np.where(up, 1, np.where(down, -1, 0)), index=close.index)
    out.iloc[:slow] = 0          # pas assez d'historique
    return out


def attach(df: pd.DataFrame, macro: dict[str, pd.Series]) -> pd.DataFrame:
    """Ajoute <nom>, <nom>_trend et <nom>_chg24 (variation sur 24 h, %) aux bougies M5.

    Chaque série H1 est décalée d'une heure (bougie close) puis reportée sur les M5 suivantes."""
    out = df.copy()
    for name, s in macro.items():
        if s is None or len(s) < 60:
            continue
        s = s.sort_index()
        s = s[~s.index.duplicated(keep="last")]
        avail = s.copy()
        avail.index = avail.index + pd.Timedelta(hours=1)        # disponible à la clôture H1
        tr = trend(s)
        tr.index = avail.index
        chg = s.pct_change(24) * 100
        chg.index = avail.index
        # pas de valeur plus vieille que 3 jours (week-end compris)
        tol = pd.Timedelta(days=3)
        out[name] = avail.reindex(out.index, method="ffill", tolerance=tol)
        out[f"{name}_trend"] = tr.reindex(out.index, method="ffill", tolerance=tol).fillna(0).astype(int)
        out[f"{name}_chg24"] = chg.reindex(out.index, method="ffill", tolerance=tol)
    return out


def gold_bias(dxy_trend: int, us10y_trend: int) -> int:
    """Biais macro pour l'or : dollar et taux en baisse = +1, en hausse = −1."""
    tot = -(2 * dxy_trend + us10y_trend)
    return 1 if tot >= 2 else -1 if tot <= -2 else 0


def describe(row: pd.Series) -> dict | None:
    """Résumé lisible du contexte macro à une bougie (pour l'interface et l'assistant)."""
    if "dxy_trend" not in row.index and "us10y_trend" not in row.index:
        return None
    items = []
    for name, meta in SERIES.items():
        if name not in row.index or pd.isna(row.get(name)):
            continue
        t = int(row.get(f"{name}_trend", 0) or 0)
        chg = row.get(f"{name}_chg24")
        items.append({"key": name, "label": meta["label"], "value": round(float(row[name]), 3),
                      "trend": t, "chg24": None if chg is None or pd.isna(chg) else round(float(chg), 2)})
    if not items:
        return None
    b = gold_bias(int(row.get("dxy_trend", 0) or 0), int(row.get("us10y_trend", 0) or 0))
    text = {1: "Dollar et/ou taux en baisse : contexte favorable à l'or (achats).",
            -1: "Dollar et/ou taux en hausse : contexte défavorable à l'or (ventes).",
            0: "Dollar et taux sans direction nette : pas de biais macro."}[b]
    return {"items": items, "gold_bias": b, "text": text}


# --------------------------------------------------------------------------- #
def _from_mt5(cfg, names: tuple[str, ...], bars: int) -> tuple[pd.Series | None, str]:
    from .mt5link import LINK

    if not LINK.available():
        return None, ""
    with LINK.lock:
        mt5 = LINK.ensure()
        for n in names:
            info = mt5.symbol_info(n)
            if info is None:
                continue
            mt5.symbol_select(n, True)
            rates = mt5.copy_rates_from_pos(n, mt5.TIMEFRAME_H1, 0, bars)
            if rates is not None and len(rates) > 60:
                d = pd.DataFrame(rates)
                idx = pd.to_datetime(d["time"], unit="s") - pd.Timedelta(hours=cfg.broker_utc_offset)
                s = pd.Series(d["close"].astype(float).to_numpy(), index=idx.dt.tz_localize("UTC"))
                return s.iloc[:-1], f"mt5:{n}"            # dernière H1 en cours retirée
    return None, ""


def _from_yahoo(ticker: str, hours: int) -> pd.Series | None:
    import yfinance as yf

    days = min(729, max(5, hours // 24 + 5))
    d = yf.download(ticker, period=f"{days}d", interval="60m", progress=False, auto_adjust=False)
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    s = d["Close"].dropna().astype(float)
    s.index = pd.to_datetime(s.index)
    s.index = s.index.tz_convert("UTC") if s.index.tz else s.index.tz_localize("UTC")
    now = pd.Timestamp.now(tz="UTC")
    return s[s.index + pd.Timedelta(hours=1) <= now]     # bougies H1 clôturées seulement


def fetch(cfg, df: pd.DataFrame) -> dict[str, pd.Series]:
    """Séries H1 couvrant les bougies M5 de `df` (MT5, puis Yahoo). Ne lève jamais d'exception."""
    hours = int((df.index[-1] - df.index[0]) / pd.Timedelta(hours=1)) + 80
    out: dict[str, pd.Series] = {}
    for name, meta in SERIES.items():
        key = f"{name}:{hours // 500}"
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            if hit[1] is not None:
                out[name] = hit[1]
            continue
        s, src = None, ""
        try:
            if cfg.source in ("auto", "mt5"):
                s, src = _from_mt5(cfg, meta["mt5"], hours)
        except Exception as e:  # noqa: BLE001
            log.debug("Macro %s via MT5 indisponible : %s", name, e)
        if s is None:
            try:
                s, src = _from_yahoo(meta["yahoo"], hours), f"yahoo:{meta['yahoo']}"
            except Exception as e:  # noqa: BLE001
                log.info("Macro %s indisponible : %s", name, e)
        _cache[key] = (time.time(), s, src)
        if s is not None:
            out[name] = s
    return out


def sources() -> dict[str, str]:
    """Source utilisée pour chaque série (dernier chargement)."""
    res = {}
    for key, (_, s, src) in _cache.items():
        if s is not None:
            res[key.split(":")[0]] = src
    return res
