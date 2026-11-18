"""Chargement et validation de config.yaml + chemins de base (compatible .exe PyInstaller)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

ALLOWED_MODES = {"demo", "backtest"}
STRATEGIES = {"mean_reversion", "trend_following", "ml_xgboost"}


def base_dir() -> Path:
    """Dossier racine : à côté du .exe si l'appli est compilée, sinon racine du projet."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resolve(path: str | os.PathLike) -> Path:
    p = Path(path)
    return p if p.is_absolute() else base_dir() / p


class ConfigError(ValueError):
    pass


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    cfg_path = resolve(path or "config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict[str, Any]) -> None:
    mode = cfg.get("mode")
    if mode not in ALLOWED_MODES:
        raise ConfigError(
            f"mode='{mode}' refusé. Seuls 'demo' et 'backtest' sont autorisés "
            "(le trading réel est volontairement bloqué)."
        )
    name = cfg.get("strategy", {}).get("name")
    if name not in STRATEGIES:
        raise ConfigError(f"strategy.name='{name}' inconnu. Choix : {sorted(STRATEGIES)}")

    risk = cfg["risk"]
    cap = float(risk.get("max_risk_per_trade_pct", 2.0))
    if cap > 2.0:
        raise ConfigError("max_risk_per_trade_pct ne peut pas dépasser 2 %.")
    rpt = float(risk["risk_per_trade_pct"])
    if not 0 < rpt <= cap:
        raise ConfigError(f"risk_per_trade_pct={rpt} doit être dans ]0, {cap}].")
    if float(risk["sl_atr_mult"]) <= 0 or float(risk["rr_ratio"]) <= 0:
        raise ConfigError("sl_atr_mult et rr_ratio doivent être > 0.")
    ks = risk["killswitch"]
    if float(ks["max_daily_drawdown_pct"]) <= 0 or int(ks["max_consecutive_losses"]) < 1:
        raise ConfigError("Paramètres du kill-switch invalides.")
    if int(cfg["filters"]["max_positions"]) < 1:
        raise ConfigError("filters.max_positions doit être >= 1.")
