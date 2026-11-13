"""Contrôle qualité avant chaque ordre du robot.

Un signal validé par l'analyse (score, zones, risque) doit encore passer ces contrôles :

1. **Validation statistique** — sur ton propre historique (backtest de plusieurs mois), la figure
   doit avoir une espérance positive avec un échantillon suffisant, et l'heure ne doit pas
   être structurellement perdante.
2. **Multi-unités de temps** — M15, H1 et H4 ne doivent pas aller contre le trade.
3. **Annonces économiques** — pas d'ordre autour d'une annonce à fort impact USD (NFP, CPI, Fed… : ce qui fait bouger l'or).
4. **Volatilité** — ATR M5 dans une plage normale (ni marché mort, ni panique).
5. **Intelligence artificielle** — probabilité de gain estimée par un modèle entraîné sur ton
   historique et validé sur une période récente jamais vue (voir analyzer/ml.py).

Chaque contrôle renvoie ✓ / ✕ avec son explication : c'est le « rapport d'analyse » de l'ordre.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)
UTC = timezone.utc
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def _chk(cid: str, label: str, ok: bool | None, detail: str) -> dict:
    """ok=None : contrôle désactivé ou neutre (n'empêche pas l'ordre)."""
    return {"id": cid, "label": label, "status": "skip" if ok is None else ("ok" if ok else "fail"),
            "detail": detail}


# ====================================================================== statistiques
class EdgeModel:
    """Performance historique par figure et par heure, calculée par backtest."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict | None = None
        self.lock = threading.Lock()
        self.computing = False
        self.error: str | None = None
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = None

    def compute(self, df, cfg) -> dict:
        from .backtest import Management, run_backtest
        from .config import Config, StrategyConfig, TradingConfig, robot_min_score

        t = cfg.trading
        bt_cfg = Config(**{**vars(cfg), "strategy": StrategyConfig(**vars(cfg.strategy)),
                           "trading": TradingConfig(**vars(t))})
        bt_cfg.strategy.min_score = robot_min_score(cfg)
        mg = Management.from_cfg(bt_cfg)
        res = run_backtest(df, bt_cfg, compare=True)
        cmp = res["management"]
        rob, cla = cmp["robot"], cmp["classic"]
        if not mg.active:
            use_robot = None
        elif rob.get("trades", 0) < t.edge_min_trades:
            use_robot = None                               # pas assez de données : réglage utilisateur
        else:
            use_robot = bool(rob.get("expectancy_r", 0) >= cla.get("expectancy_r", 0))
        if use_robot is False and t.management_auto:       # stats cohérentes avec la gestion utilisée
            bt_cfg.trading.trailing = bt_cfg.trading.reversal_exit = False
            res = run_backtest(df, bt_cfg, compare=False)
        management = {"active": mg.active, "use_robot": use_robot, "auto": bool(t.management_auto),
                      "rules": {k: v for k, v in vars(mg).items()},
                      "robot": json.loads(json.dumps(rob, default=float)),
                      "classic": json.loads(json.dumps(cla, default=float))}

        def rows(group):
            return {str(g["key"]): {"trades": int(g["trades"]), "win_rate": round(float(g["win_rate"]), 1),
                                    "expectancy_r": round(float(g["expectancy_r"]), 3),
                                    "total_r": round(float(g["total_r"]), 2)} for g in group}

        trades = res["trades"]
        pf = {}
        for tr in trades:
            d = pf.setdefault(tr["pattern"], [0.0, 0.0])
            d[0 if tr["result_r"] > 0 else 1] += abs(tr["result_r"])
        patterns = rows(res["by_pattern"])
        for name, p in patterns.items():
            w, l = pf.get(name, [0, 0])
            p["profit_factor"] = round(float(w / l), 2) if l else None
        data = {
            "computed_at": datetime.now(UTC).isoformat(),
            "period": res["period"],
            "score_min": bt_cfg.strategy.min_score,
            "stats": json.loads(json.dumps(res["stats"], default=float)),
            "patterns": patterns,
            "hours": rows(res["by_hour"]),
            "management": management,
        }
        with self.lock:
            self.data, self.error = data, None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("Sauvegarde des statistiques impossible : %s", e)
        return data

    def age_hours(self) -> float | None:
        if not self.data:
            return None
        return (datetime.now(UTC) - datetime.fromisoformat(self.data["computed_at"])).total_seconds() / 3600

    def management(self, cfg):
        """Gestion à appliquer (Management) : celle du robot, sauf si l'historique la désavoue."""
        from .backtest import Management

        mg = Management.from_cfg(cfg)
        m = (self.data or {}).get("management") or {}
        if cfg.trading.management_auto and m.get("use_robot") is False:
            return Management()                            # gestion classique (TP1 / BE / TP2)
        return mg

    def verdict(self, pattern: str, t) -> tuple[bool | None, str]:
        """True = rentable, False = perdante (ou non prouvée si edge_block_unproven), None = non bloquant."""
        p = (self.data or {}).get("patterns", {}).get(pattern)
        if p is None or p["trades"] < t.edge_min_trades:
            n = 0 if p is None else p["trades"]
            txt = f"échantillon insuffisant ({n} trade(s) historiques, minimum {t.edge_min_trades})"
            return (False, txt) if t.edge_block_unproven else (None, txt + " — non bloquant")
        pf = p.get("profit_factor")
        good = bool(p["expectancy_r"] >= t.edge_min_expectancy and (pf is None or pf >= t.edge_min_pf))
        txt = (f"{p['expectancy_r']:+.2f} R par trade sur {p['trades']} trades, "
               f"réussite {p['win_rate']:.0f} %, PF {pf if pf is not None else '∞'}")
        return good, txt

    def hour_verdict(self, hour: int, t) -> tuple[bool | None, str]:
        h = (self.data or {}).get("hours", {}).get(str(hour))
        if h is None or h["trades"] < max(10, t.edge_min_trades // 2):
            return None, f"{hour} h UTC : pas assez de données, non bloquant"
        if float(h["expectancy_r"]) < 0:
            return False, f"{hour} h UTC perdante sur l'historique ({h['expectancy_r']:+.2f} R sur {h['trades']} trades)"
        return True, f"{hour} h UTC : {h['expectancy_r']:+.2f} R sur {h['trades']} trades"

    def summary(self, t) -> dict:
        if not self.data:
            return {"ready": False, "computing": self.computing, "error": self.error}
        rows = []
        for name, p in sorted(self.data["patterns"].items(), key=lambda kv: -kv[1]["expectancy_r"]):
            ok, why = self.verdict(name, t)
            rows.append({"pattern": name, **p, "qualified": ok, "reason": why})
        return {"ready": True, "computing": self.computing, "error": self.error,
                "computed_at": self.data["computed_at"], "period": self.data["period"],
                "score_min": self.data["score_min"], "stats": self.data["stats"], "patterns": rows,
                "management": self.data.get("management"),
                "qualified": [r["pattern"] for r in rows if r["qualified"] is True],
                "blocked": [r["pattern"] for r in rows if r["qualified"] is False],
                "unproven": [r["pattern"] for r in rows if r["qualified"] is None]}


# ====================================================================== annonces
def _fetch_calendar(url: str = CALENDAR_URL, timeout: float = 15) -> list[dict]:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "gold-ai-signal-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class NewsCalendar:
    """Calendrier économique de la semaine (annonces USD), mis en cache 6 h."""

    def __init__(self, path: Path, fetcher=_fetch_calendar):
        self.path = path
        self.fetcher = fetcher
        self.events: list[dict] = []
        self.fetched_at: datetime | None = None
        self.error: str | None = None
        self.lock = threading.Lock()
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            self.events = cached["events"]
            self.fetched_at = datetime.fromisoformat(cached["fetched_at"])
        except (OSError, ValueError, KeyError):
            pass

    def refresh(self, force: bool = False) -> None:
        with self.lock:
            if not force and self.fetched_at and datetime.now(UTC) - self.fetched_at < timedelta(hours=6):
                return
            try:
                raw = self.fetcher()
                events = []
                for e in raw:
                    if e.get("country") != "USD":
                        continue
                    try:
                        when = datetime.fromisoformat(e["date"]).astimezone(UTC)
                    except (KeyError, ValueError):
                        continue
                    events.append({"time": when.isoformat(), "country": e["country"],
                                   "title": e.get("title", ""), "impact": e.get("impact", "")})
                events.sort(key=lambda x: x["time"])
                self.events, self.fetched_at, self.error = events, datetime.now(UTC), None
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"fetched_at": self.fetched_at.isoformat(),
                                                 "events": events}, ensure_ascii=False), encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                self.error = f"calendrier indisponible ({e})"
                log.warning("Calendrier économique : %s", e)

    @property
    def available(self) -> bool:
        return bool(self.fetched_at) and datetime.now(UTC) - self.fetched_at < timedelta(days=8)

    def blocking(self, now: datetime, window_min: int) -> list[dict]:
        w = timedelta(minutes=window_min)
        return [e for e in self.events if e["impact"] == "High"
                and abs(datetime.fromisoformat(e["time"]) - now) <= w]

    def upcoming(self, now: datetime, n: int = 6) -> list[dict]:
        return [e for e in self.events if e["impact"] in ("High", "Medium")
                and datetime.fromisoformat(e["time"]) >= now - timedelta(minutes=30)][:n]


# ====================================================================== contrôle
class QualityGate:
    def __init__(self, data_dir: Path, fetcher=_fetch_calendar):
        self.edge = EdgeModel(Path(data_dir) / "edge.json")
        self.news = NewsCalendar(Path(data_dir) / "calendar.json", fetcher)
        from .ml import WinModel
        self.ml = WinModel(Path(data_dir))
        self.last: dict | None = None

    def check(self, sig: dict, cfg, now: datetime | None = None) -> tuple[bool, list[dict]]:
        t = cfg.trading
        now = now or datetime.now(UTC)
        d = int(sig["direction"])
        from .config import risk_factor, robot_min_score
        lo, k = robot_min_score(cfg), risk_factor(sig["score"], cfg)
        extra = f" — score faible : risque × {k:g}" if k < 1 and sig["score"] >= lo else ""
        checks = [_chk("score", "Score de l'analyse", sig["score"] >= lo,
                       f"{sig['score']}/10 (minimum robot {lo:g}) — {sig['pattern']}{extra}")]

        # 1. statistiques
        if t.quality_edge:
            if not self.edge.data:
                checks.append(_chk("edge", "Validation statistique", False if t.edge_block_unproven else None,
                                   "statistiques pas encore calculées (onglet Robot → Recalculer)"
                                   + ("" if t.edge_block_unproven else " — non bloquant")))
            else:
                ok, why = self.edge.verdict(sig["pattern"], t)
                checks.append(_chk("edge", "Figure rentable sur l'historique", ok, why))
                hour = datetime.fromisoformat(sig["close_time"]).hour
                ok_h, why_h = self.edge.hour_verdict(hour, t)
                checks.append(_chk("hour", "Heure favorable", ok_h, why_h))
        else:
            checks.append(_chk("edge", "Validation statistique", None, "désactivée"))

        # 2. multi-unités de temps
        mtf = sig.get("mtf") or {}
        if t.quality_mtf and mtf:
            names = {"m15": "M15", "h1": "H1", "h4": "H4"}
            against = [names[k] for k, v in mtf.items() if v == -d]
            aligned = [names[k] for k, v in mtf.items() if v == d]
            arrow = lambda v: "▲" if v == 1 else "▼" if v == -1 else "↔"  # noqa: E731
            detail = " · ".join(f"{names[k]} {arrow(v)}" for k, v in mtf.items())
            ok = not against and len(aligned) >= 2
            checks.append(_chk("mtf", "Tendances M15 / H1 / H4", ok,
                               detail + (f" — contre le trade : {', '.join(against)}" if against else
                                         "" if ok else " — pas assez d'unités de temps alignées")))
        else:
            checks.append(_chk("mtf", "Tendances M15 / H1 / H4", None, "désactivé"))

        # 3. annonces
        if t.quality_news:
            if not self.news.available:
                checks.append(_chk("news", "Annonces économiques", False if t.news_block_on_error else None,
                                   (self.news.error or "calendrier non chargé")
                                   + (" — ordre bloqué par prudence" if t.news_block_on_error else " — non bloquant")))
            else:
                block = self.news.blocking(now, t.news_window_min)
                checks.append(_chk("news", "Annonces économiques", not block,
                                   "aucune annonce majeure USD à ±%d min" % t.news_window_min if not block else
                                   "annonce proche : " + ", ".join(
                                       f"{e['country']} {e['title']} ({datetime.fromisoformat(e['time']):%H:%M} UTC)"
                                       for e in block)))
        else:
            checks.append(_chk("news", "Annonces économiques", None, "filtre désactivé"))

        # 4. volatilité
        atr = sig.get("atr_pips")
        if atr:
            ok = t.min_atr_pips <= atr <= t.max_atr_pips
            checks.append(_chk("vol", "Volatilité normale", ok,
                               f"ATR M5 {atr} pips (plage {t.min_atr_pips}–{t.max_atr_pips})"))

        # 5. intelligence artificielle
        if t.quality_ml:
            ok_ml, why_ml = self.ml.verdict(sig.get("ml"), t)
            checks.append(_chk("ml", "Probabilité IA", ok_ml, why_ml))
        else:
            checks.append(_chk("ml", "Probabilité IA", None, "désactivée"))

        passed = all(c["status"] != "fail" for c in checks)
        self.last = {"time": now.isoformat(), "signal": {k: sig.get(k) for k in
                                                        ("key", "side", "pattern", "score", "close_time")},
                     "passed": passed, "checks": checks}
        return passed, checks

    def status(self, cfg) -> dict:
        now = datetime.now(UTC)
        return {"edge": self.edge.summary(cfg.trading), "ml": self.ml.summary(cfg.trading),
                "news": {"available": self.news.available, "error": self.news.error,
                         "fetched_at": self.news.fetched_at.isoformat() if self.news.fetched_at else None,
                         "upcoming": self.news.upcoming(now),
                         "blocking_now": self.news.blocking(now, cfg.trading.news_window_min)},
                "last": self.last}
