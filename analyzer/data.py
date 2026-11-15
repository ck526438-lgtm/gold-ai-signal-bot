"""Sources de données M5 : MetaTrader 5 (temps réel), Yahoo Finance (gratuit), CSV.

Toutes les fonctions renvoient un DataFrame indexé en UTC avec les colonnes
open, high, low, close, volume — uniquement des bougies CLÔTURÉES.
"""
from __future__ import annotations

import logging

import pandas as pd

from .config import Config

log = logging.getLogger(__name__)
COLS = ["open", "high", "low", "close", "volume"]


def _drop_open_candle(df: pd.DataFrame, minutes: int = 5) -> pd.DataFrame:
    """Retire la dernière bougie si elle n'est pas encore fermée."""
    if df.empty:
        return df
    now = pd.Timestamp.now(tz="UTC")
    if df.index[-1] + pd.Timedelta(minutes=minutes) > now:
        return df.iloc[:-1]
    return df


def from_mt5(cfg: Config) -> pd.DataFrame:
    from .mt5link import LINK

    with LINK.lock:
        mt5 = LINK.ensure()
        mt5.symbol_select(cfg.symbol, True)
        rates = mt5.copy_rates_from_pos(cfg.symbol, mt5.TIMEFRAME_M5, 0, cfg.bars)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"Aucune donnée MT5 pour {cfg.symbol} : {mt5.last_error()}")
    df = pd.DataFrame(rates)
    idx = pd.to_datetime(df["time"], unit="s") - pd.Timedelta(hours=cfg.broker_utc_offset)
    df.index = idx.dt.tz_localize("UTC")
    df = df.rename(columns={"tick_volume": "volume"})[COLS]
    return _drop_open_candle(df.astype(float))


def mt5_forming_bar_utc(cfg: Config) -> pd.Timestamp | None:
    """Heure d'ouverture (UTC) de la bougie M5 en cours chez le courtier, ou None."""
    from .mt5link import LINK

    with LINK.lock:
        mt5 = LINK.ensure()
        rates = mt5.copy_rates_from_pos(cfg.symbol, mt5.TIMEFRAME_M5, 0, 1)
    if rates is None or len(rates) == 0:
        return None
    t = rates[0]["time"] if not isinstance(rates[0], dict) else rates[0]["time"]
    return pd.Timestamp(int(t), unit="s", tz="UTC") - pd.Timedelta(hours=cfg.broker_utc_offset)


def yahoo_ticker(symbol: str) -> str:
    """Yahoo ne publie pas l'or au comptant en M5 : on utilise le contrat à terme COMEX (GC=F).

    Il suit le XAUUSD à quelques dollars près (écart de portage) : très bien pour l'analyse et les
    alertes, mais les niveaux exacts d'entrée / stop doivent venir de MetaTrader 5.
    """
    s = symbol.upper()
    if "XAU" in s or s.startswith("GOLD"):
        return "GC=F"
    if "XAG" in s or s.startswith("SILVER"):
        return "SI=F"
    return f"{s[:6]}=X"


def from_yfinance(cfg: Config) -> pd.DataFrame:
    import yfinance as yf

    ticker = yahoo_ticker(cfg.symbol)
    period = "60d" if cfg.bars > 1400 else "5d"
    df = yf.download(ticker, period=period, interval="5m", progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo Finance n'a renvoyé aucune donnée pour {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[COLS]
    df.index = pd.to_datetime(df.index)
    df.index = df.index.tz_convert("UTC") if df.index.tz else df.index.tz_localize("UTC")
    df = df.dropna().tail(cfg.bars)
    return _drop_open_candle(df.astype(float))


def from_csv(path: str) -> pd.DataFrame:
    """CSV avec colonnes time/datetime, open, high, low, close[, volume] (heure UTC)."""
    df = pd.read_csv(path)
    df.columns = [c.lower().strip() for c in df.columns]
    tcol = next(c for c in ("time", "datetime", "date", "timestamp") if c in df.columns)
    df.index = pd.to_datetime(df[tcol], utc=True)
    if "volume" not in df.columns:
        df["volume"] = 0.0
    return df[COLS].astype(float).sort_index()


def from_demo(cfg: Config, seed: int = 42) -> pd.DataFrame:
    """Marché de l'or (XAUUSD) simulé, aligné sur l'heure actuelle (une nouvelle bougie toutes les 5 min).

    Sert à découvrir l'interface sans MetaTrader 5 ni connexion. Les prix ne sont PAS réels.
    """
    import numpy as np

    end = pd.Timestamp.now(tz="UTC").floor("5min") - pd.Timedelta(minutes=5)
    anchor = pd.Timestamp("2026-01-01", tz="UTC")
    n_total = int((end - anchor) / pd.Timedelta(minutes=5)) + 1
    rng = np.random.default_rng(seed)
    idx = pd.date_range(anchor, periods=n_total, freq="5min")
    hours = idx.hour.to_numpy()
    vol = np.where((hours >= 7) & (hours < 20), 1.8, 0.8)   # en $ par bougie M5
    regime = np.repeat(rng.choice([-1, 0, 1], n_total // 300 + 1), 300)[:n_total] * 0.3
    steps = regime + rng.normal(0, 1, n_total) * vol
    dev = np.empty(n_total)
    x = 0.0
    for k in range(n_total):  # retour lent vers 4000 $ : niveau plausible sur des mois
        x = x * (1 - 1 / 20000) + steps[k]
        dev[k] = x
    close = 4000.0 + dev
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, 1, (2, n_total))) * vol * 0.8
    df = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + wick[0],
                       "low": np.minimum(open_, close) - wick[1], "close": close,
                       "volume": rng.integers(50, 400, n_total).astype(float)}, index=idx)
    return df.tail(cfg.bars)


SOURCE_LABELS = {"mt5": "MetaTrader 5", "yfinance": "Yahoo Finance", "csv": "Fichier CSV",
                 "demo": "Démo (simulé)"}


def load_data(cfg: Config) -> tuple[pd.DataFrame, str]:
    """Renvoie (bougies M5 clôturées + contexte macro si disponible, source réellement utilisée)."""
    df, src = _load_prices(cfg)
    if not cfg.strategy.use_macro or df.empty:
        return df, src
    from . import macro
    try:
        series = demo_macro(df) if src == "demo" else macro.fetch(cfg, df) if src in ("mt5", "yfinance") else {}
        if series:
            df = macro.attach(df, series)
    except Exception as e:  # noqa: BLE001 — le macro ne doit jamais bloquer l'analyse
        log.info("Contexte macro ignoré : %s", e)
    return df, src


def demo_macro(df: pd.DataFrame, seed: int = 7) -> dict[str, pd.Series]:
    """DXY et taux simulés, inversement liés à l'or simulé (mode démo seulement)."""
    import numpy as np

    h1 = df["close"].resample("1h").last().dropna()
    rng = np.random.default_rng(seed)
    g = (h1 - h1.mean()) / max(h1.std(), 1e-9)
    dxy = 100 - 1.2 * g + rng.normal(0, 0.15, len(h1)).cumsum() * 0.2
    tnx = 4.2 - 0.08 * g + rng.normal(0, 0.01, len(h1)).cumsum()
    return {"dxy": pd.Series(dxy.to_numpy(), index=h1.index), "us10y": pd.Series(tnx.to_numpy(), index=h1.index)}


def _load_prices(cfg: Config) -> tuple[pd.DataFrame, str]:
    if cfg.source == "csv":
        return from_csv(cfg.csv_path).tail(cfg.bars), "csv"
    if cfg.source == "demo":
        return from_demo(cfg), "demo"
    if cfg.source == "mt5":
        return from_mt5(cfg), "mt5"
    if cfg.source == "yfinance":
        return from_yfinance(cfg), "yfinance"
    # auto : MT5 si disponible (meilleure qualité), sinon Yahoo
    try:
        return from_mt5(cfg), "mt5"
    except Exception as e:  # noqa: BLE001
        log.info("MT5 indisponible (%s) -> Yahoo Finance", e)
        return from_yfinance(cfg), "yfinance"
