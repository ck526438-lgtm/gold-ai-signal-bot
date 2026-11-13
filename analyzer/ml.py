"""Modèle d'intelligence artificielle : probabilité qu'un signal soit gagnant.

Principe (honnête, sans « boule de cristal ») :

* Pour chaque opportunité détectée sur ton historique, on mesure ~25 caractéristiques
  connues **à la clôture de la bougie signal** (score, figure, tendances M15/H1/H4, RSI, ADX,
  volatilité, distance aux moyennes, élan récent, heure…) et on rejoue le trade avec les
  règles exactes du backtest (TP1 / break-even / TP2 / sortie au temps, spread inclus).
* Un modèle de gradient boosting apprend la probabilité « trade gagnant » à partir de ces
  caractéristiques.
* Validation **chronologique** (walk-forward) : apprentissage sur les 60 % les plus anciens,
  choix du seuil sur les 20 % suivants, puis **mesure finale sur les 20 % les plus récents,
  jamais vus** pendant l'apprentissage ni le réglage. Une zone tampon (durée max d'un trade)
  sépare les périodes pour éviter toute fuite d'information.
* Le modèle n'est utilisé comme filtre que s'il fait **mieux que la stratégie seule** sur
  cette période test. Sinon il est affiché mais ne bloque rien.
"""
from __future__ import annotations

import json
import logging
import pickle
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)
UTC = timezone.utc
MODEL_VERSION = 1

FEATURES = [
    ("score", "Score de l'analyse"),
    ("direction", "Sens (achat / vente)"),
    ("pattern", "Figure de chandelier"),
    ("risk_pips", "Taille du stop (pips)"),
    ("atr_pips", "Volatilité ATR (pips)"),
    ("atr_ratio", "Volatilité vs moyenne 100 bougies"),
    ("rsi_dir", "RSI dans le sens du trade"),
    ("adx", "Force de tendance ADX"),
    ("m15", "Tendance M15 dans le sens du trade"),
    ("h1", "Tendance H1 dans le sens du trade"),
    ("h4", "Tendance H4 dans le sens du trade"),
    ("dist_ema_fast", "Distance à l'EMA 20 (ATR)"),
    ("ema_spread", "Écart EMA 20 / 50 (ATR)"),
    ("dist_ema_slow", "Distance à l'EMA 200 (ATR)"),
    ("body", "Corps de la bougie signal (ATR)"),
    ("range", "Amplitude de la bougie signal (ATR)"),
    ("mom12", "Élan sur 1 h (ATR)"),
    ("mom48", "Élan sur 4 h (ATR)"),
    ("pos48", "Position dans le range 4 h"),
    ("rr_tp2", "Potentiel jusqu'à TP2 (R)"),
    ("n_pos", "Nombre de facteurs favorables"),
    ("n_neg", "Nombre de facteurs défavorables"),
    ("hour_sin", "Heure (cycle)"),
    ("hour_cos", "Heure (cycle, bis)"),
    ("weekday", "Jour de la semaine"),
]
FEATURE_NAMES = [f[0] for f in FEATURES]
LABELS = dict(FEATURES)
PATTERN_COL = FEATURE_NAMES.index("pattern")


def sklearn_available() -> bool:
    try:
        import sklearn  # noqa: F401
        return True
    except ImportError:
        return False


def features(an, sig, patterns: list[str]) -> list[float]:
    """Caractéristiques du signal `sig` (bougie an[i]) — uniquement des données ≤ i."""
    i, d = sig.index, sig.direction
    atr = max(float(an.atr[i]), 1e-9)
    c = an.c
    lo, hi = max(0, i - 100), i + 1
    atr_mean = float(np.nanmean(an.atr[lo:hi])) or atr
    w0 = max(0, i - 47)
    hh, ll = float(np.max(an.h[w0:hi])), float(np.min(an.l[w0:hi]))
    pos = (c[i] - ll) / (hh - ll) if hh > ll else 0.5
    if d == -1:
        pos = 1 - pos
    close_t = sig.time.hour + sig.time.minute / 60 + 5 / 60
    ang = 2 * np.pi * close_t / 24
    pcode = patterns.index(sig.pattern) if sig.pattern in patterns else np.nan
    return [
        sig.score, d, pcode, sig.risk_pips, sig.atr_pips, atr / atr_mean,
        d * (an.rsi[i] - 50), an.adx[i],
        d * sig.mtf.get("m15", 0), d * sig.mtf.get("h1", 0), d * sig.mtf.get("h4", 0),
        d * (c[i] - an.ef[i]) / atr, d * (an.ef[i] - an.em[i]) / atr, d * (c[i] - an.es[i]) / atr,
        d * (c[i] - an.o[i]) / atr, (an.h[i] - an.l[i]) / atr,
        d * (c[i] - c[max(0, i - 12)]) / atr, d * (c[i] - c[max(0, i - 48)]) / atr,
        pos, sig.rr(sig.tp2),
        sum(1 for f in sig.factors if f.points > 0), sum(1 for f in sig.factors if f.points < 0),
        np.sin(ang), np.cos(ang), sig.time.weekday(),
    ]


def _trainable(sig, min_score: float) -> bool:
    """Opportunités retenues pour l'apprentissage : valides, ou rejetées seulement pour le score."""
    if sig is None or sig.score < min_score:
        return False
    return sig.blocked is None or sig.blocked.startswith("Score insuffisant")


def build_dataset(an, cfg, train_min_score: float = 4.0, mg=None) -> dict:
    """Toutes les opportunités de l'historique avec caractéristiques et résultat simulé."""
    from .backtest import simulate

    s = cfg.strategy
    patterns = sorted({p for p in _pattern_names()})
    X, y, r, idx, valid, times = [], [], [], [], [], []
    last = len(an.c) - s.max_hold_bars - 1          # trades encore ouverts : résultat inconnu
    for i in range(an.warmup, last):
        sig = an.inspect(i)
        if not _trainable(sig, train_min_score):
            continue
        out = simulate(an, sig, s.spread_pips, s.max_hold_bars, mg)
        if out.status in ("non rempli", "annulé"):     # entrée sur repli jamais déclenchée
            continue
        X.append(features(an, sig, patterns))
        y.append(1 if out.result_r > 0 else 0)
        r.append(out.result_r)
        idx.append(i)
        valid.append(sig.blocked is None)
        times.append(sig.time)
    return {"X": np.asarray(X, float), "y": np.asarray(y, int), "r": np.asarray(r, float),
            "idx": np.asarray(idx, int), "valid": np.asarray(valid, bool), "times": times,
            "patterns": patterns}


def _pattern_names() -> list[str]:
    from .patterns import PATTERN_NAMES
    return list(PATTERN_NAMES)


def _make_model():
    from sklearn.ensemble import HistGradientBoostingClassifier

    cat = np.zeros(len(FEATURE_NAMES), bool)
    cat[PATTERN_COL] = True
    return HistGradientBoostingClassifier(
        max_iter=120, learning_rate=0.04, max_depth=3, max_leaf_nodes=8, min_samples_leaf=40,
        l2_regularization=1.0, categorical_features=cat, random_state=7)


def _cooldown(idx: np.ndarray, direction: np.ndarray, bars: int) -> np.ndarray:
    """Masque des signaux que le robot aurait réellement reçus (délai entre signaux)."""
    keep = np.zeros(len(idx), bool)
    last = {1: -10**9, -1: -10**9}
    for k, (i, d) in enumerate(zip(idx, direction)):
        if i - last[int(d)] >= bars:
            last[int(d)] = i
            keep[k] = True
    return keep


def _perf(r: np.ndarray) -> dict:
    n = int(len(r))
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "expectancy_r": 0.0, "total_r": 0.0, "profit_factor": None}
    w, l = float(r[r > 0].sum()), float(-r[r <= 0].sum())
    return {"trades": n, "win_rate": round(float((r > 0).mean() * 100), 1),
            "expectancy_r": round(float(r.mean()), 3), "total_r": round(float(r.sum()), 2),
            "profit_factor": round(w / l, 2) if l > 0 else None}


def _auc(y: np.ndarray, p: np.ndarray) -> float | None:
    if len(set(y.tolist())) < 2:
        return None
    from sklearn.metrics import roc_auc_score
    return round(float(roc_auc_score(y, p)), 3)


def train_and_validate(ds: dict, cfg, min_score: float) -> dict:
    """Walk-forward 60 / 20 / 20 avec zone tampon. Renvoie modèle final + rapport."""
    s = cfg.strategy
    X, y, r, idx = ds["X"], ds["y"], ds["r"], ds["idx"]
    n = len(y)
    if n < 150:
        raise RuntimeError(f"pas assez d'opportunités dans l'historique pour entraîner l'IA "
                           f"({n}, minimum 150) — augmente « bougies de validation »")
    lo_i, hi_i = int(idx.min()), int(idx.max())
    cut1 = lo_i + int((hi_i - lo_i) * 0.6)
    cut2 = lo_i + int((hi_i - lo_i) * 0.8)
    gap = s.max_hold_bars + 1
    tr = idx < cut1 - gap
    va = (idx >= cut1) & (idx < cut2 - gap)
    te = idx >= cut2
    if tr.sum() < 80 or va.sum() < 30 or te.sum() < 30:
        raise RuntimeError("historique trop court pour une validation fiable (train/validation/test)")

    # ce que le robot aurait reçu : signal valide, score ≥ minimum robot, délai entre signaux
    robot = ds["valid"] & (X[:, 0] >= min_score)
    direction = X[:, FEATURE_NAMES.index("direction")].astype(int)

    def stream(mask):
        m = mask & robot
        keep = np.zeros(n, bool)
        sel = np.where(m)[0]
        keep[sel[_cooldown(idx[sel], direction[sel], s.cooldown_bars)]] = True
        return keep

    # 1) apprentissage sur la période ancienne
    model = _make_model().fit(X[tr], y[tr])
    p = model.predict_proba(X)[:, 1]

    # 2) seuil choisi sur la période de validation (jamais sur le test)
    va_s = stream(va)
    best_thr, best_total = 0.0, float(r[va_s].sum()) if va_s.any() else 0.0
    min_keep = max(8, int(va_s.sum() * 0.25))
    for thr in np.arange(0.40, 0.76, 0.025):
        sel = va_s & (p >= thr)
        if sel.sum() < min_keep:
            break
        total = float(r[sel].sum())
        if total > best_total + 1e-9:
            best_thr, best_total = float(round(thr, 3)), total

    # 3) mesure finale sur la période la plus récente, jamais vue
    te_s = stream(te)
    base = _perf(r[te_s])
    filt = _perf(r[te_s & (p >= best_thr)]) if best_thr > 0 else base
    va_base, va_filt = _perf(r[va_s]), _perf(r[va_s & (p >= best_thr)])
    # utile = mieux en validation ET sur le test jamais vu, avec un échantillon suffisant
    useful = bool(best_thr > 0
                  and filt["trades"] >= max(20, int(0.3 * base["trades"]))
                  and filt["expectancy_r"] > base["expectancy_r"] + 0.02
                  and filt["win_rate"] >= base["win_rate"]
                  and va_filt["expectancy_r"] > va_base["expectancy_r"])

    # calibration : réussite réelle par tranche de probabilité (période test, toutes opportunités)
    bins = []
    for a, b in ((0, .4), (.4, .5), (.5, .6), (.6, .7), (.7, 1.01)):
        m = te & (p >= a) & (p < b)
        if m.sum():
            bins.append({"range": f"{int(a * 100)}–{min(int(b * 100), 100)} %", "count": int(m.sum()),
                         "predicted": round(float(p[m].mean() * 100), 1),
                         "actual": round(float(y[m].mean() * 100), 1)})

    importance = _importance(model, X[te], y[te])

    # 4) modèle final ré-entraîné sur tout l'historique (même réglages, même seuil)
    final = _make_model().fit(X, y)

    t0 = ds["times"]
    period = lambda m: {"start": t0[int(np.where(m)[0][0])].isoformat(),  # noqa: E731
                        "end": t0[int(np.where(m)[0][-1])].isoformat(), "samples": int(m.sum())}
    report = {
        "version": MODEL_VERSION,
        "computed_at": datetime.now(UTC).isoformat(),
        "samples": n, "base_rate": round(float(y.mean() * 100), 1),
        "periods": {"train": period(tr), "validation": period(va), "test": period(te)},
        "threshold": best_thr,
        "auc_test": _auc(y[te], p[te]),
        "test_baseline": base, "test_filtered": filt,
        "validation_baseline": _perf(r[va_s]), "validation_filtered": _perf(r[va_s & (p >= best_thr)]),
        "useful": useful,
        "calibration": bins,
        "importance": importance,
        "score_min": min_score,
    }
    return {"model": final, "patterns": ds["patterns"], "threshold": best_thr, "report": report}


def _importance(model, X, y, top: int = 8) -> list[dict]:
    try:
        from sklearn.inspection import permutation_importance
        if len(set(y.tolist())) < 2:
            return []
        res = permutation_importance(model, X, y, n_repeats=4, random_state=7, scoring="roc_auc")
        order = np.argsort(-res.importances_mean)[:top]
        return [{"feature": FEATURE_NAMES[k], "label": LABELS[FEATURE_NAMES[k]],
                 "importance": round(float(res.importances_mean[k]), 4)} for k in order]
    except Exception as e:  # noqa: BLE001
        log.info("Importance des caractéristiques indisponible : %s", e)
        return []


# ====================================================================== service
class WinModel:
    """Modèle persistant (data/ml_model.pkl) + rapport lisible (data/ml.json)."""

    def __init__(self, data_dir: Path):
        self.pkl = Path(data_dir) / "ml_model.pkl"
        self.json = Path(data_dir) / "ml.json"
        self.lock = threading.Lock()
        self.bundle: dict | None = None
        self.report: dict | None = None
        self.computing = False
        self.error: str | None = None if sklearn_available() else \
            "scikit-learn n'est pas installé (pip install scikit-learn)"
        try:
            with open(self.pkl, "rb") as f:
                b = pickle.load(f)  # noqa: S301 — fichier écrit par l'application elle-même
            if b.get("version") == MODEL_VERSION:
                self.bundle, self.report = b, b["report"]
        except Exception:  # noqa: BLE001 — absent, corrompu ou d'une autre version de sklearn
            self.bundle = None

    @property
    def available_to_train(self) -> bool:
        return sklearn_available()

    @property
    def ready(self) -> bool:
        return self.bundle is not None

    def age_hours(self) -> float | None:
        if not self.report:
            return None
        return (datetime.now(UTC) - datetime.fromisoformat(self.report["computed_at"])).total_seconds() / 3600

    def compute(self, df, cfg, mg=None) -> dict:
        """Entraîne sur l'historique ; `mg` = gestion des sorties utilisée par le robot."""
        from .config import analysis_config, robot_min_score
        from .strategy import Analyzer

        if not sklearn_available():
            raise RuntimeError(self.error or "scikit-learn manquant")
        an = Analyzer(df, analysis_config(cfg))
        ds = build_dataset(an, cfg, mg=mg if (mg is not None and mg.active) else None)
        min_score = robot_min_score(cfg)
        out = train_and_validate(ds, cfg, min_score)
        period = {"start": df.index[0].isoformat(), "end": df.index[-1].isoformat(), "bars": len(df)}
        out["report"]["data_period"] = period
        bundle = {"version": MODEL_VERSION, **out}
        with self.lock:
            self.bundle, self.report, self.error = bundle, out["report"], None
        try:
            self.pkl.parent.mkdir(parents=True, exist_ok=True)
            with open(self.pkl, "wb") as f:
                pickle.dump(bundle, f)
            self.json.write_text(json.dumps(out["report"], ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("Sauvegarde du modèle IA impossible : %s", e)
        return out["report"]

    def predict(self, an, sig) -> float | None:
        b = self.bundle
        if b is None or sig is None:
            return None
        try:
            x = np.asarray([features(an, sig, b["patterns"])], float)
            return round(float(b["model"].predict_proba(x)[0, 1]), 3)
        except Exception as e:  # noqa: BLE001
            log.warning("Prédiction IA impossible : %s", e)
            return None

    def threshold(self, t) -> float:
        if t.ml_min_proba and t.ml_min_proba > 0:
            return float(t.ml_min_proba)
        return float((self.bundle or {}).get("threshold") or 0.5)

    def annotate(self, an, sig, t) -> dict | None:
        """Bloc « ml » ajouté au dictionnaire d'un signal (radar, journal, robot)."""
        p = self.predict(an, sig)
        if p is None:
            return None
        thr = self.threshold(t)
        return {"proba": p, "threshold": thr, "pass": p >= thr,
                "useful": bool((self.report or {}).get("useful"))}

    def summary(self, t) -> dict:
        return {"ready": self.ready, "computing": self.computing, "error": self.error,
                "available": sklearn_available(), "enabled": bool(t.quality_ml),
                "threshold": self.threshold(t) if self.ready else None,
                "threshold_mode": "manuel" if t.ml_min_proba and t.ml_min_proba > 0 else "automatique",
                "report": self.report}

    def verdict(self, ml: dict | None, t) -> tuple[bool | None, str]:
        if not self.ready:
            return None, (self.error or "modèle pas encore entraîné") + " — non bloquant"
        if not ml:
            return None, "probabilité non calculée pour ce signal — non bloquant"
        rep = self.report or {}
        base, filt = rep.get("test_baseline", {}), rep.get("test_filtered", {})
        txt = (f"probabilité de gain {ml['proba'] * 100:.0f} % (seuil {ml['threshold'] * 100:.0f} %)")
        if not rep.get("useful") and not (t.ml_min_proba and t.ml_min_proba > 0):
            return None, txt + " — le modèle ne bat pas la stratégie seule sur la période test, non bloquant"
        if filt.get("trades"):
            txt += (f" — sur la période test jamais vue : {filt['win_rate']:.0f} % gagnants au-dessus du seuil"
                    f" contre {base.get('win_rate', 0):.0f} % sans IA")
        return bool(ml["pass"]), txt
