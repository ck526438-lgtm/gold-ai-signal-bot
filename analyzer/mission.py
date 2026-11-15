"""Missions en langage naturel (« Donne une mission au bot ») — gratuit, hors ligne.

Exemple : « achète seulement les cassures du range asiatique, risque 0,5 %, prends les profits à
2R, maximum 3 trades, arrête-toi à 16h et ferme tout ».

`parse()` comprend la phrase avec des règles (aucune IA extérieure) et renvoie les réglages du robot
à appliquer + les contraintes de la mission (sens, figures, heure de fin, objectif de gain, perte
maximale). Le robot les applique ensuite tout seul : filtre les signaux, arrête la mission à l'heure,
sécurise les gains quand l'objectif est atteint.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc

SETUPS = {
    "range": (["Cassure du range asiatique haussière", "Cassure du range asiatique baissière"],
              "cassures du range asiatique"),
    "balayage": (["Balayage du plus bas de la veille", "Balayage du plus haut de la veille"],
                 "balayages du plus haut / plus bas de la veille"),
    "avalement": (["Avalement haussier", "Avalement baissier"], "avalements"),
    "pin": (["Pin bar haussière (marteau)", "Pin bar baissière (étoile filante)"], "pin bars"),
    "etoile": (["Étoile du matin", "Étoile du soir"], "étoiles du matin / du soir"),
    "soldats": (["Trois soldats blancs", "Trois corbeaux noirs"], "trois soldats / corbeaux"),
    "pince": (["Pince basse (tweezer bottom)", "Pince haute (tweezer top)"], "pinces (tweezer)"),
    "inside": (["Cassure d'inside bar haussière", "Cassure d'inside bar baissière"], "cassures d'inside bar"),
}
SETUP_WORDS = [
    (r"range asiat|cassure.{0,10}asi|asian|london break|breakout", "range"),
    (r"balayage|liquidit|sweep|piege|stop hunt", "balayage"),
    (r"avalement|engulf", "avalement"),
    (r"pin ?bar|marteau|etoile filante|hammer", "pin"),
    (r"etoile du (matin|soir)|morning star|evening star", "etoile"),
    (r"soldats|corbeaux", "soldats"),
    (r"pince|tweezer", "pince"),
    (r"inside ?bar", "inside"),
]


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFD", text.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return t.replace("’", "'").replace(" ", " ").replace("\xa0", " ")


def _fr(x: float) -> str:
    return f"{x:g}".replace(".", ",")


def _num(s: str) -> float:
    return float(s.replace(",", ".").replace(" ", ""))


@dataclass
class Mission:
    text: str
    started_at: str
    end_at: str | None = None
    close_at_end: bool = False
    direction: int = 0                     # +1 achats seulement, −1 ventes seulement
    patterns: list[str] | None = None      # figures autorisées (None = toutes)
    profit_target: float | None = None     # en devise du compte
    loss_stop: float | None = None
    max_trades: int | None = None
    start_balance: float = 0.0
    status: str = "active"                 # active | terminée | arrêtée
    end_reason: str = ""
    understood: list[str] = field(default_factory=list)
    changes: dict = field(default_factory=dict)
    equity: list = field(default_factory=list)   # [[iso, équité], …]
    trades: int = 0

    @property
    def active(self) -> bool:
        return self.status == "active"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Mission":
        keys = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in keys})

    # -------------------------------------------------------------- règles appliquées par le robot
    def allows(self, sig: dict) -> str | None:
        """None si la mission autorise ce signal, sinon la raison du refus."""
        if not self.active:
            return None
        if self.direction and int(sig.get("direction", 0)) != self.direction:
            return f"mission : {'achats' if self.direction == 1 else 'ventes'} seulement"
        if self.patterns and sig.get("pattern") not in self.patterns:
            return f"mission : figure {sig.get('pattern')} non demandée"
        if self.max_trades is not None and self.trades >= self.max_trades:
            return f"mission : {self.max_trades} trade(s) déjà pris"
        if self.end_at and datetime.now(UTC) >= datetime.fromisoformat(self.end_at):
            return "mission terminée (heure de fin)"
        return None

    def record_equity(self, equity: float, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if self.equity:
            last_t = datetime.fromisoformat(self.equity[-1][0])
            if (now - last_t).total_seconds() < 20 and abs(self.equity[-1][1] - equity) < 0.005:
                return
        self.equity.append([now.isoformat(), round(float(equity), 2)])
        if len(self.equity) > 3000:
            self.equity = self.equity[::2]

    def check_end(self, equity: float, now: datetime | None = None) -> str | None:
        """Raison de fin de mission (heure, objectif, perte max), sinon None."""
        if not self.active:
            return None
        now = now or datetime.now(UTC)
        pnl = equity - self.start_balance
        if self.profit_target is not None and pnl >= self.profit_target:
            return f"objectif atteint : +{pnl:,.2f} (objectif +{self.profit_target:,.2f})"
        if self.loss_stop is not None and pnl <= -self.loss_stop:
            return f"perte maximale atteinte : {pnl:,.2f} (limite −{self.loss_stop:,.2f})"
        if self.end_at and now >= datetime.fromisoformat(self.end_at):
            return f"heure de fin atteinte ({datetime.fromisoformat(self.end_at):%H:%M} UTC)"
        return None


# --------------------------------------------------------------------------- compréhension
def _end_time(t: str, now: datetime) -> tuple[datetime | None, str]:
    m = re.search(r"pendant\s+(\d+(?:[.,]\d+)?)\s*(h|heures?|min|minutes?)\b|"
                  r"(?:pour|durant)\s+(?:les\s+)?(\d+(?:[.,]\d+)?)\s*(?:prochaines?\s+)?(h|heures?|min|minutes?)\b", t)
    if m:
        val, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        delta = timedelta(minutes=_num(val)) if unit.startswith("min") else timedelta(hours=_num(val))
        return now + delta, f"pendant {val} {'min' if unit.startswith('min') else 'h'}"
    m = re.search(r"(?:jusqu'?\s*a|avant|d'ici|arrete[sz]?(?:-toi|-vous|\s+toi)?\s*(?:a|vers)?|stop\w*\s*(?:a|vers)|"
                  r"fin\w*\s*(?:a|vers)|termine\w*\s*(?:a|vers)|ferme\w*\s+tout\s*(?:a|vers)|by)\s*"
                  r"(\d{1,2})\s*(?:h|:)\s*(\d{2})?", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if h > 23 or mi > 59:
            return None, ""
        end = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if end <= now:
            end += timedelta(days=1)
        return end, f"jusqu'à {h:02d}h{mi:02d} UTC"
    if re.search(r"fin de (la )?journee|ce soir|aujourd'hui seulement", t):
        end = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if end <= now:
            end += timedelta(days=1)
        return end, "jusqu'à 20h00 UTC (fin de la session de New York)"
    return None, ""


def parse(text: str, balance: float = 0.0, now: datetime | None = None, currency: str = "$") -> dict:
    """Comprend une mission. Renvoie {"changes", "mission", "understood", "warnings", "empty"}."""
    now = now or datetime.now(UTC)
    t = _norm(text)
    trading: dict = {}
    strategy: dict = {}
    und: list[str] = []
    warn: list[str] = []
    ms = Mission(text=text.strip(), started_at=now.isoformat(), start_balance=round(balance, 2))

    # style d'abord (les valeurs précises dites ensuite l'emportent)
    if re.search(r"prudent|doucement|debutant|sans risque|securi[st]e?\b.*compte|petit risque", t):
        trading.update(risk_percent=0.25, min_score=7.5, max_trades_per_day=3)
        und.append("Style prudent : risque 0,25 %, score 7,5 minimum, 3 trades par jour")
    elif re.search(r"agressi|beaucoup de trades|tous les signaux|scalp|actif", t):
        trading.update(risk_percent=0.3, low_score_enabled=True, low_score_min=5.5, low_score_risk_factor=0.5,
                       max_trades_per_day=12, max_open_trades=2)
        strategy["cooldown_bars"] = 3
        und.append("Style actif : plus de trades, scores faibles à risque réduit")

    # mode
    if re.search(r"simulat|paper|sans argent|pour (de )?faux|virtuel", t):
        trading["mode"] = "paper"
        und.append("Simulateur (sans argent)")
    elif re.search(r"\bmt5\b|metatrader|compte demo|\bdemo\b", t):
        trading["mode"] = "mt5"
        und.append("MetaTrader 5 (compte démo)")

    # sens
    if re.search(r"(seulement|uniquement|que)\s+(les\s+|des\s+)?(achats?|acheter|long)|only (buy|long)|achats? (seulement|uniquement)|"
                 r"pas de vente", t):
        ms.direction = 1
        und.append("Achats seulement")
    elif re.search(r"(seulement|uniquement|que)\s+(les\s+|des\s+)?(ventes?|vendre|short)|only (sell|short)|ventes? (seulement|uniquement)|"
                   r"pas d'achat", t):
        ms.direction = -1
        und.append("Ventes seulement")

    # setups
    keys = [k for rx, k in SETUP_WORDS if re.search(rx, t)]
    if keys:
        ms.patterns = sorted({p for k in keys for p in SETUPS[k][0]})
        und.append("Setups : " + ", ".join(SETUPS[k][1] for k in dict.fromkeys(keys)))
        if any(k in ("range", "balayage") for k in keys):
            strategy.update({"asian_breakout": True} if "range" in keys else {})
            strategy.update({"liquidity_sweep": True} if "balayage" in keys else {})

    # risque par trade (avant les autres pourcentages)
    m = re.search(r"risqu\w*\D{0,25}?(\d+(?:[.,]\d+)?)\s*%", t)
    if m:
        r = _num(m.group(1))
        if 0.01 <= r <= 5:
            trading["risk_percent"] = r
            und.append(f"Risque {_fr(r)} % du solde par trade")
        else:
            warn.append(f"Risque de {_fr(r)} % refusé (entre 0,01 % et 5 %)")
        t = t[:m.start()] + " " + t[m.end():]

    # qualité minimale
    m = re.search(r"score\D{0,15}(\d+(?:[.,]\d)?)", t)
    if m and 0 <= _num(m.group(1)) <= 10:
        trading["min_score"] = _num(m.group(1))
        und.append(f"Score minimum {_fr(trading['min_score'])}/10")
    elif re.search(r"\ba\+|meilleurs? (signaux|setups?|trades?)|que du (tres )?bon|haute qualite", t):
        trading["min_score"] = 8.0
        und.append("Seulement les meilleurs signaux (A+, score 8)")

    # prise de profit en R (« 2R », « 2x le risque », « 10x »)
    m = re.search(r"(?:profit|tp|objectif|cible|gain|encaisse\w*|sors?)\D{0,25}?(\d+(?:[.,]\d+)?)\s*(?:r\b|x\b|fois)", t) \
        or re.search(r"(\d+(?:[.,]\d+)?)\s*(?:r|x)\b\s*(?:de\s+)?(?:profit|gain|objectif)", t)
    if m:
        x = _num(m.group(1))
        if x > 3:
            strategy["rr_tp2"] = min(x, 10.0)
            strategy["rr_tp1"] = min(1.5, x)
            trading["quick_target_r"] = 0.0
            und.append(f"Objectif final à {_fr(min(x, 10.0))} fois le risque (TP1 à 1,5 R, puis le reste court)")
            if x > 10:
                warn.append("Objectif ramené à 10 R : au-delà, il n'est presque jamais atteint en M5")
        elif x >= 0.3:
            trading["quick_target_r"] = x
            und.append(f"Prise de profit unique à {_fr(x)} fois le risque")
        t = t[:m.start()] + " " + t[m.end():]

    # objectif de gain de la mission (montant ou %)
    m = re.search(r"(?:gagne\w*|gain|benefice|profit|objectif|vise\w*)\D{0,20}?(\d+(?:[.,]\d+)?)\s*(\$|usd|dollars?|eur|€|%)", t)
    if m:
        v, unit = _num(m.group(1)), m.group(2)
        amount = balance * v / 100 if unit == "%" else v
        if amount > 0:
            ms.profit_target = round(amount, 2)
            und.append(f"Objectif de la mission : +{amount:,.2f} {currency}" + (f" ({_fr(v)} %)" if unit == "%" else "")
                       + " — gains sécurisés et robot arrêté une fois atteint")
        t = t[:m.start()] + " " + t[m.end():]

    # perte maximale de la mission
    m = re.search(r"(?:perte|perds?|perdre|pertes|drawdown)\D{0,20}?(\d+(?:[.,]\d+)?)\s*(\$|usd|dollars?|eur|€|%)", t)
    if m:
        v, unit = _num(m.group(1)), m.group(2)
        amount = balance * v / 100 if unit == "%" else v
        if amount > 0:
            ms.loss_stop = round(amount, 2)
            und.append(f"Perte maximale : −{amount:,.2f} {currency}" + (f" ({_fr(v)} %)" if unit == "%" else ""))
            if unit == "%" and 0.1 <= v <= 20:
                trading["daily_loss_limit_percent"] = v

    # nombre de trades
    m = re.search(r"(?:max\w*|pas plus de|au plus|limite\w*\s*a)\s*(\d{1,2})\s*trades?|(\d{1,2})\s*trades?\s*(?:max|maximum|au plus)", t) \
        or re.search(r"\b(\d{1,2})\s*trades?\b", t)
    if m:
        n = int(next(g for g in m.groups() if g))
        if n >= 1:
            ms.max_trades = n
            trading["max_trades_per_day"] = max(n, 1)
            und.append(f"Maximum {n} trade{'s' if n > 1 else ''} pour cette mission")
    elif re.search(r"\bun seul trade|\b1 seul trade", t):
        ms.max_trades = 1
        und.append("Un seul trade")

    # fin de mission
    end, label = _end_time(t, now)
    if end:
        ms.end_at = end.isoformat()
        und.append(f"Mission {label}")
    if re.search(r"ferme\w* tout|cloture\w* tout|ferme\w* (les|mes) positions|tout fermer|lock|verrouill|"
                 r"securise\w* (le reste|tout|les gains)", t):
        ms.close_at_end = True
        und.append("À la fin : positions fermées et gains sécurisés")

    # gestion
    if re.search(r"stop suiveur|trailing|suis le prix|laisse courir", t):
        trading["trailing"] = True
        und.append("Stop suiveur pour laisser courir les gains")
    if re.search(r"break.?even|stop (a|au) (l'|point d')?entree|securise\w* apres tp1", t):
        trading["breakeven"] = True
        und.append("Stop à l'entrée après TP1")
    if re.search(r"retest|precision|ordre limite|meilleur prix", t):
        trading["retest_entries"] = True
        und.append("Entrée de précision sur retest")
    if re.search(r"valide|confirme|verifie|assistant|\bia\b|analyse chaque", t):
        trading["ai_confirm"] = True
        und.append("Chaque trade validé par l'assistant (PLACER / IGNORER)")
    if re.search(r"annonce|news|nfp|cpi|fed", t):
        trading["quality_news"] = True
        und.append("Pas de trade autour des annonces USD")

    changes = {}
    if trading:
        changes["trading"] = trading
    if strategy:
        changes["strategy"] = strategy
    ms.changes = changes
    ms.understood = und
    return {"changes": changes, "mission": ms, "understood": und, "warnings": warn,
            "empty": not und and not warn}


def reply(res: dict, started: bool, error: str | None = None) -> str:
    if res.get("empty"):
        return ("Je n'ai pas compris la mission. Exemples :\n"
                "- « achète seulement les cassures du range asiatique, risque 0,5 %, arrête-toi à 11h »\n"
                "- « trade les balayages de la veille, prends les profits à 2R, maximum 3 trades »\n"
                "- « mode prudent en simulateur, objectif +50 $, perte max 30 $, ferme tout à 20h »")
    lines = []
    if error:
        lines.append(f"Je ne peux pas lancer la mission : {error}")
    elif started:
        lines.append("C'est parti. Je surveille l'or (XAUUSD M5) et je trade selon ta mission :")
    else:
        lines.append("Mission comprise :")
    lines += [f"- {u}" for u in res["understood"]]
    lines += [f"- ⚠️ {w}" for w in res["warnings"]]
    if started and not error:
        lines.append("Les contrôles de sécurité restent actifs (annonces, spread, perte du jour, compte réel refusé "
                     "sans autorisation). Tu peux arrêter la mission à tout moment.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- persistance
class MissionStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Mission | None:
        try:
            return Mission.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def save(self, m: Mission | None) -> None:
        try:
            if m is None:
                self.path.unlink(missing_ok=True)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(m.to_dict(), ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
