"""Configuration : config.yaml, modifiable aussi depuis l'onglet Réglages."""
from __future__ import annotations

import copy

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml


@dataclass
class StrategyConfig:
    min_score: float = 6.0          # score minimum d'un signal (sur 10)
    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    rsi_period: int = 14
    atr_period: int = 14
    adx_period: int = 14
    adx_min: float = 18.0
    swing_lookback: int = 300
    zone_atr_mult: float = 0.6
    sl_atr_buffer: float = 0.25
    min_sl_pips: float = 25.0      # or : 1 pip = 0,10 $ -> 2,50 $
    max_sl_pips: float = 150.0     # 15 $
    rr_tp1: float = 1.5
    rr_tp2: float = 2.5
    min_room_r: float = 1.2
    cooldown_bars: int = 6
    use_sessions: bool = True
    sessions_utc: list = field(default_factory=lambda: [[7, 11], [12, 20]])  # Londres + New York
    spread_pips: float = 3.3        # coût simulé (0,33 $ mesuré sur XAUUSD, ForexClub démo) (backtest + suivi des signaux)
    max_hold_bars: int = 48         # sortie forcée après 4 h
    disabled_patterns: list = field(default_factory=list)  # figures ignorées


@dataclass
class TradingConfig:
    """Robot de trading. Désactivé par défaut ; comptes démo seulement sauf allow_live."""
    mode: str = "mt5"                  # mt5 | paper (simulateur, sans argent)
    start_enabled: bool = False        # activer le robot automatiquement au lancement
    autonomous: bool = False           # mode autonome : démarrage avec la session, robot relancé seul,
                                       # anti-veille, alertes de santé et bilan du jour
    allow_live: bool = False           # autoriser un compte RÉEL (sinon démo uniquement)
    risk_percent: float = 0.5          # % du solde risqué par trade
    min_score: float = 7.0             # score minimum pour exécuter (≥ score des alertes)
    max_open_trades: int = 1           # trades (signaux) ouverts en même temps
    max_trades_per_day: int = 4
    daily_loss_limit_percent: float = 2.0
    max_spread_pips: float = 6.0       # 0,60 $
    max_slippage_pips: float = 8.0     # écart max entre prix du signal et prix d'exécution
    max_lots: float = 1.0              # plafond de sécurité, quel que soit le calcul
    split_targets: bool = True         # 2 positions : TP1 et TP2
    breakeven: bool = True             # stop au prix d'entrée quand TP1 est atteint
    close_after_max_hold: bool = True  # clôture après max_hold_bars bougies
    # --- entrée de précision
    entry_mode: str = "market"         # market = au marché à la clôture | pullback = sur repli
    pullback_r: float = 0.3            # repli attendu vers le stop, en fraction du risque
    pullback_bars: int = 3             # durée de validité de l'entrée sur repli (bougies)
    # --- encaisser vite (petit / moyen profit)
    quick_target_r: float = 0.0        # 0 = objectifs TP1/TP2 de l'analyse ; sinon objectif unique à X R
    # --- trades à score faible (risque réduit)
    low_score_enabled: bool = False    # accepter aussi les signaux sous min_score…
    low_score_min: float = 5.0         # …à partir de ce score…
    low_score_risk_factor: float = 0.5 # …avec ce multiplicateur du risque (0,5 = moitié)
    # --- suivi des gains et sorties automatiques
    trailing: bool = True              # stop suiveur : suit le prix quand le trade gagne
    trail_start_r: float = 1.0         # s'active quand le gain atteint ce multiple du risque
    trail_distance_r: float = 0.7      # distance du stop derrière le meilleur prix (en R)
    reversal_exit: bool = True         # sortie si un signal opposé solide apparaît
    reversal_score: float = 6.0        # score minimum du signal opposé
    reversal_momentum: bool = False    # … ou perte d'élan (EMA 20 franchie contre le trade, RSI / 50)
    reversal_structure: bool = False   # … ou cassure du dernier creux / sommet formé depuis l'entrée
    management_auto: bool = True       # n'utiliser stop suiveur / retournement que s'ils font
                                       # mieux que la gestion classique sur l'historique
    magic: int = 730526
    deviation_points: int = 30
    paper_balance: float = 10_000.0
    # --- contrôle qualité avant ordre (voir analyzer/quality.py)
    edge_block_unproven: bool = False  # bloquer aussi les figures sans assez d'historique
    quality_edge: bool = True          # figure rentable sur l'historique
    edge_bars: int = 20_000            # bougies du backtest de validation (~10 semaines)
    edge_min_trades: int = 20          # taille d'échantillon minimale par figure
    edge_min_expectancy: float = 0.05  # espérance minimale (R par trade)
    edge_min_pf: float = 1.1           # profit factor minimal
    quality_mtf: bool = True           # M15 / H1 / H4 pas contre le trade
    quality_news: bool = True          # pas d'ordre autour des annonces majeures USD (l'or réagit surtout au dollar)
    news_window_min: int = 30
    news_block_on_error: bool = False  # calendrier indisponible : bloquer (True) ou ignorer
    min_atr_pips: float = 10.0         # ATR M5 de l'or, 1 pip = 0,10 $
    max_atr_pips: float = 120.0
    # --- intelligence artificielle (voir analyzer/ml.py)
    quality_ml: bool = True            # probabilité de gain estimée par le modèle entraîné
    ml_min_proba: float = 0.0          # 0 = seuil automatique choisi en validation, sinon 0,30–0,90


@dataclass
class Config:
    setup_done: bool = False        # assistant de premier démarrage terminé
    symbol: str = "XAUUSD"
    source: str = "auto"            # auto | mt5 | yfinance | csv | demo
    mt5_path: str = ""              # terminal64.exe à utiliser (vide = détection automatique)
    mt5_bridge: str = ""            # Linux : adresse du pont MT5 sous Wine, ex. 127.0.0.1:18822
    csv_path: str = ""
    bars: int = 1500
    broker_utc_offset: int = 0
    pip: float = 0.1                # XAUUSD : 1 pip = 0,10 $ (10 points de 0,01)
    desktop_notifications: bool = False
    data_dir: str = "data"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)

    def to_dict(self) -> dict:
        return asdict(self)


EDITABLE_ROOT = {"symbol", "source", "csv_path", "bars", "broker_utc_offset", "desktop_notifications",
                 "mt5_path", "mt5_bridge", "setup_done"}


def _apply(cfg: Config, raw: dict) -> Config:
    raw = dict(raw or {})
    strat = raw.pop("strategy", {}) or {}
    trad = raw.pop("trading", {}) or {}
    for k, v in raw.items():
        if hasattr(cfg, k) and k not in ("strategy", "trading"):
            setattr(cfg, k, _cast(getattr(cfg, k), v))
    for k, v in strat.items():
        if hasattr(cfg.strategy, k):
            setattr(cfg.strategy, k, _cast(getattr(cfg.strategy, k), v))
    for k, v in trad.items():
        if hasattr(cfg.trading, k):
            setattr(cfg.trading, k, _cast(getattr(cfg.trading, k), v))
    return cfg


def _cast(current, value):
    if isinstance(current, bool):
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "oui", "on")
    if isinstance(current, int) and not isinstance(value, bool):
        return int(float(value))
    if isinstance(current, float):
        return float(value)
    return value


def load_config(path: str | os.PathLike = "config.yaml") -> Config:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _apply(Config(), raw or {})


def update_config(cfg: Config, changes: dict) -> Config:
    """Applique des changements venant de l'interface (valeurs validées)."""
    root = {k: v for k, v in changes.items() if k in EDITABLE_ROOT}
    strat = changes.get("strategy", {}) or {}
    trad = changes.get("trading", {}) or {}
    new = _apply(Config(**{**asdict(cfg), "strategy": StrategyConfig(**asdict(cfg.strategy)),
                           "trading": TradingConfig(**asdict(cfg.trading))}),
                 {**root, "strategy": strat, "trading": trad})
    validate(new)
    return new


def validate(cfg: Config) -> None:
    s = cfg.strategy
    if cfg.source not in ("auto", "mt5", "yfinance", "csv", "demo"):
        raise ValueError("Source inconnue")
    if not 0 <= s.min_score <= 10:
        raise ValueError("Le score minimum doit être entre 0 et 10")
    if not 0 < s.min_sl_pips < s.max_sl_pips:
        raise ValueError("Stop mini doit être > 0 et < stop maxi")
    if not 0 < s.rr_tp1 <= s.rr_tp2:
        raise ValueError("TP1 doit être > 0 et ≤ TP2")
    if not 300 <= cfg.bars <= 100_000:
        raise ValueError("Le nombre de bougies doit être entre 300 et 100 000")
    for a, b in s.sessions_utc:
        if not 0 <= a < b <= 24:
            raise ValueError("Sessions : heures invalides")
    t = cfg.trading
    if t.mode not in ("mt5", "paper"):
        raise ValueError("Mode du robot inconnu")
    if not 0.01 <= t.risk_percent <= 5:
        raise ValueError("Le risque par trade doit être entre 0,01 % et 5 %")
    if not 0 <= t.min_score <= 10:
        raise ValueError("Le score minimum du robot doit être entre 0 et 10")
    if t.max_open_trades < 1 or t.max_trades_per_day < 1:
        raise ValueError("Les limites de trades doivent être d'au moins 1")
    if not 0.1 <= t.daily_loss_limit_percent <= 20:
        raise ValueError("La perte max par jour doit être entre 0,1 % et 20 %")
    if t.max_spread_pips <= 0 or t.max_slippage_pips <= 0 or t.max_lots <= 0:
        raise ValueError("Spread, glissement et lot max doivent être positifs")
    if not 3000 <= t.edge_bars <= 100_000:
        raise ValueError("Historique de validation : entre 3 000 et 100 000 bougies")
    if t.edge_min_trades < 5:
        raise ValueError("Échantillon minimal : au moins 5 trades")
    if not 0 <= t.news_window_min <= 240:
        raise ValueError("Fenêtre autour des annonces : entre 0 et 240 min")
    if not 0 <= t.min_atr_pips < t.max_atr_pips:
        raise ValueError("Volatilité : le minimum doit être inférieur au maximum")
    if t.quick_target_r and not 0.3 <= t.quick_target_r <= 3:
        raise ValueError("Objectif « encaisser vite » : 0 (désactivé) ou entre 0,3 et 3 R")
    if t.entry_mode not in ("market", "pullback"):
        raise ValueError("Mode d'entrée inconnu (market ou pullback)")
    if not 0.05 <= t.pullback_r <= 0.8 or not 1 <= t.pullback_bars <= 12:
        raise ValueError("Entrée sur repli : repli entre 0,05 et 0,8 R, validité entre 1 et 12 bougies")
    if not 0 <= t.low_score_min <= 10:
        raise ValueError("Score faible minimum : entre 0 et 10")
    if t.low_score_enabled and t.low_score_min >= t.min_score:
        raise ValueError("Le score faible minimum doit être inférieur au score minimum du robot")
    if not 0.1 <= t.low_score_risk_factor <= 1:
        raise ValueError("Multiplicateur de risque des scores faibles : entre 0,1 et 1")
    if not 0.2 <= t.trail_start_r <= 5 or not 0.2 <= t.trail_distance_r <= 3:
        raise ValueError("Stop suiveur : départ entre 0,2 et 5 R, distance entre 0,2 et 3 R")
    if not 0 <= t.reversal_score <= 10:
        raise ValueError("Score du signal de retournement : entre 0 et 10")
    if t.ml_min_proba and not 0.3 <= t.ml_min_proba <= 0.9:
        raise ValueError("Probabilité IA minimum : 0 (automatique) ou entre 0,30 et 0,90")


# Profils du robot (onglet Robot). Ne touchent jamais au mode (MT5 / simulateur) ni au compte réel.
PROFILES = {
    "debutant": {
        "label": "Débutant", "desc": "Peu de trades, très sélectifs, risque minimal. Idéal pour commencer en démo.",
        "trading": {"risk_percent": 0.25, "min_score": 7.5, "low_score_enabled": False, "max_open_trades": 1,
                    "max_trades_per_day": 3, "daily_loss_limit_percent": 1.0, "trailing": True,
                    "trail_start_r": 1.0, "trail_distance_r": 0.7, "reversal_exit": True, "reversal_score": 6.0,
                    "reversal_momentum": False, "reversal_structure": False,
                    "entry_mode": "market", "management_auto": True, "quality_edge": True, "quality_mtf": True,
                    "quality_news": True, "quality_ml": True, "edge_block_unproven": False},
        "strategy": {"cooldown_bars": 6},
    },
    "equilibre": {
        "label": "Équilibré", "desc": "Les réglages par défaut : sélectif, 4 trades par jour au plus.",
        "trading": {"risk_percent": 0.5, "min_score": 7.0, "low_score_enabled": False, "max_open_trades": 1,
                    "max_trades_per_day": 4, "daily_loss_limit_percent": 2.0, "trailing": True,
                    "trail_start_r": 1.0, "trail_distance_r": 0.7, "reversal_exit": True, "reversal_score": 6.0,
                    "reversal_momentum": False, "reversal_structure": False,
                    "entry_mode": "market", "management_auto": True, "quality_edge": True, "quality_mtf": True,
                    "quality_news": True, "quality_ml": True, "edge_block_unproven": False},
        "strategy": {"cooldown_bars": 6},
    },
    "actif": {
        "label": "Actif", "desc": "Plus de trades : scores faibles à risque réduit, 2 positions en même temps, "
                                  "jusqu'à 12 trades par jour, stop suiveur plus serré.",
        "trading": {"risk_percent": 0.3, "min_score": 7.0, "low_score_enabled": True, "low_score_min": 5.5,
                    "low_score_risk_factor": 0.5, "max_open_trades": 2, "max_trades_per_day": 12,
                    "daily_loss_limit_percent": 2.0, "trailing": True, "trail_start_r": 0.8,
                    "trail_distance_r": 0.6, "reversal_exit": True, "reversal_score": 6.0,
                    "reversal_momentum": True, "reversal_structure": False,
                    "management_auto": True, "quality_edge": True, "quality_mtf": True, "quality_news": True,
                    "quality_ml": True, "edge_block_unproven": False},
        "strategy": {"cooldown_bars": 3},
    },
    "tous": {
        "label": "Tous les signaux", "desc": "Prend tous les signaux dès le score 4 (sous 7 : risque divisé par 2), "
                                              "sans filtre de statistiques, de tendances ni d'IA ; coupe le trade "
                                              "au premier retournement (figure inverse, perte d'élan, cassure). "
                                              "Annonces, spread et perte maximale du jour restent actifs.",
        "trading": {"risk_percent": 0.25, "min_score": 7.0, "low_score_enabled": True, "low_score_min": 4.0,
                    "low_score_risk_factor": 0.5, "max_open_trades": 3, "max_trades_per_day": 20,
                    "daily_loss_limit_percent": 2.0, "trailing": True, "trail_start_r": 0.8,
                    "trail_distance_r": 0.6, "reversal_exit": True, "reversal_score": 5.0,
                    "reversal_momentum": True, "reversal_structure": True, "management_auto": True,
                    "quality_edge": False, "quality_mtf": False, "quality_news": True, "quality_ml": False,
                    "edge_block_unproven": False},
        "strategy": {"cooldown_bars": 2},
    },
}


def apply_profile(cfg: Config, name: str) -> Config:
    if name not in PROFILES:
        raise ValueError(f"Profil inconnu : {name}")
    p = PROFILES[name]
    return update_config(cfg, {"trading": dict(p["trading"]), "strategy": dict(p["strategy"])})


def robot_min_score(cfg: Config) -> float:
    """Score le plus bas que le robot peut trader (scores faibles inclus)."""
    t = cfg.trading
    return min(t.min_score, t.low_score_min) if t.low_score_enabled else t.min_score


def risk_factor(score: float, cfg: Config) -> float:
    """Multiplicateur du risque : réduit pour les signaux à score faible."""
    t = cfg.trading
    if t.low_score_enabled and score < t.min_score:
        return t.low_score_risk_factor
    return 1.0


def analysis_config(cfg: Config) -> Config:
    """Copie dont le score minimum d'analyse descend au seuil « score faible » du robot."""
    lo = robot_min_score(cfg)
    if lo >= cfg.strategy.min_score:
        return cfg
    c = copy.deepcopy(cfg)
    c.strategy.min_score = lo
    return c


def save_config(cfg: Config, path: str | os.PathLike = "config.yaml") -> None:
    d = cfg.to_dict()
    d.pop("pip", None)
    d.pop("data_dir", None)
    header = "# Gold AI Signal Bot — configuration (modifiable aussi dans l'onglet Réglages)\n"
    Path(path).write_text(header + yaml.safe_dump(d, sort_keys=False, allow_unicode=True),
                          encoding="utf-8")
