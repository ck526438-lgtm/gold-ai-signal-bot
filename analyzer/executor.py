"""Robot de trading : transforme les signaux validés en ordres, puis gère les positions.

Sécurités, dans l'ordre où elles sont vérifiées avant chaque ordre :
  robot activé · score ≥ score du robot · signal frais (< 3 min) · pas déjà traité ·
  compte démo (ou réel explicitement autorisé) · trading auto autorisé dans MT5 ·
  pas en pause (perte du jour) · trades du jour · trades ouverts · spread ·
  glissement de prix · stop valide · lot calculé sur le risque ≥ lot minimum.
Gestion : stop au prix d'entrée quand TP1 est atteint, clôture après la durée max.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import json

import pandas as pd

from .broker import MT5Broker, PaperBroker, floor_step
from .config import risk_factor, robot_min_score
from .quality import QualityGate

log = logging.getLogger(__name__)
UTC = timezone.utc
FRESH_SECONDS = 180

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL,
    leg        INTEGER NOT NULL,          -- 0 position unique, 1 = TP1, 2 = TP2
    mode       TEXT NOT NULL,
    ticket     INTEGER,
    side       TEXT NOT NULL,
    volume     REAL NOT NULL,
    price      REAL,
    sl         REAL NOT NULL,
    tp         REAL NOT NULL,
    tp1        REAL NOT NULL,
    status     TEXT NOT NULL,             -- ouvert | fermé | erreur
    message    TEXT NOT NULL DEFAULT '',
    profit     REAL,
    be_done    INTEGER NOT NULL DEFAULT 0,
    time       TEXT NOT NULL,
    close_time TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_key ON trades(key);
CREATE TABLE IF NOT EXISTS skips (            -- signaux non tradés et pourquoi
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    time    TEXT NOT NULL,
    key     TEXT NOT NULL,
    reason  TEXT NOT NULL,                -- identifiant court (disabled, score, edge, mtf…)
    detail  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    time    TEXT NOT NULL,
    level   TEXT NOT NULL,                -- info | ok | warn | err
    message TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _day_start(now: datetime | None = None) -> datetime:
    n = now or _now()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


class TradeStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(trades)")}
            if "analysis" not in cols:  # base créée par une version antérieure
                self.db.execute("ALTER TABLE trades ADD COLUMN analysis TEXT NOT NULL DEFAULT ''")
            for col in ("sl0", "peak", "risk_factor"):   # v3.3 : stop initial, meilleur prix, risque
                if col not in cols:
                    self.db.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
            self.db.commit()

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    def log(self, level: str, message: str) -> dict:
        t = _now().isoformat()
        self.x("INSERT INTO bot_log(time, level, message) VALUES (?,?,?)", (t, level, message))
        return {"time": t, "level": level, "message": message}

    def close(self) -> None:
        with self.lock:           # jamais pendant une écriture d'un autre thread
            self.db.close()


class Executor:
    def __init__(self, cfg, data_dir: Path, link=None, paper: PaperBroker | None = None,
                 quality: QualityGate | None = None):
        self.cfg = cfg
        self.quality = quality or QualityGate(Path(data_dir))
        self.store = TradeStore(Path(data_dir) / "trades.db")
        self.link = link
        self.paper = paper or PaperBroker(cfg.trading.paper_balance, cfg.strategy.spread_pips, cfg.pip)
        self.enabled = False
        self.paused: str | None = None
        self.paused_day: datetime | None = None
        self.last_error: str | None = None
        self.listeners: list = []
        self._lock = threading.RLock()
        self._pending: dict[str, dict] = {}     # entrées sur repli en attente

    # ------------------------------------------------------------------ outils
    @property
    def t(self):
        return self.cfg.trading

    @property
    def broker(self):
        if self.t.mode == "paper":
            return self.paper
        if self.link is None:
            from .mt5link import LINK
            self.link = LINK
        return MT5Broker(self.link, self.cfg.broker_utc_offset)

    def set_config(self, cfg) -> None:
        with self._lock:
            self.cfg = cfg

    def _emit(self, ev: dict) -> None:
        for cb in list(self.listeners):
            try:
                cb(ev)
            except Exception:  # noqa: BLE001
                pass

    def _log(self, level: str, message: str, notify: bool = False) -> None:
        entry = self.store.log(level, message)
        log.log(logging.ERROR if level == "err" else logging.INFO, message)
        self._emit({"type": "bot", "entry": entry, "notify": notify})

    def _skip(self, sig: dict, reason: str, code: str = "autre") -> None:
        self._last_skip = reason
        self._log("warn", f"Signal {sig['side']} {sig['pattern']} {sig.get('score', '')}/10 non exécuté : {reason}",
                  notify=True)
        try:
            self.store.x("INSERT INTO skips(time, key, reason, detail) VALUES (?,?,?,?)",
                         (_now().isoformat(), sig.get("key", ""), code, reason[:500]))
        except Exception:  # noqa: BLE001
            pass

    SKIP_LABELS = {
        "disabled": "Robot désactivé", "score": "Score trop faible", "stale": "Signal trop ancien",
        "edge": "Figure perdante sur l'historique", "hour": "Heure perdante", "mtf": "Tendances M15/H1/H4",
        "news": "Annonce économique", "vol": "Volatilité", "ml": "Probabilité IA", "account": "Compte / Algo Trading",
        "limits": "Limites (jour, trades ouverts, perte)", "market": "Spread / prix / symbole",
        "size": "Lot minimum du courtier", "pullback": "Repli non atteint (entrée annulée)", "error": "Ordre refusé par le courtier", "autre": "Autre",
    }

    def skip_stats(self, days: int = 7) -> list[dict]:
        since = (_now() - timedelta(days=days)).isoformat()
        rows = self.store.q("SELECT reason, COUNT(*) n, MAX(detail) detail FROM skips WHERE time >= ? "
                            "GROUP BY reason ORDER BY n DESC", (since,))
        return [{"code": r["reason"], "label": self.SKIP_LABELS.get(r["reason"], r["reason"]),
                 "count": r["n"], "example": r["detail"]} for r in rows]

    # ------------------------------------------------------------------ marche / arrêt
    def enable(self, confirm_live: bool = False) -> dict:
        """Active le robot après avoir vérifié la connexion et le type de compte."""
        with self._lock:
            acc = self.broker.account()
            if not acc.is_demo:
                if not self.t.allow_live:
                    raise PermissionError("Compte RÉEL détecté. Le trading réel n'est pas autorisé "
                                          "(Robot → « Autoriser un compte réel »).")
                if not confirm_live:
                    raise PermissionError("Confirmation requise pour activer le robot sur un compte RÉEL.")
            if not acc.trade_allowed:
                raise PermissionError(acc.trade_allowed_reason)
            self.enabled, self.paused, self.last_error = True, None, None
            kind = "DÉMO" if acc.is_demo else "RÉEL"
            self._log("ok", f"Robot activé — {self.broker.name}, compte {kind} {acc.login or ''} "
                            f"({acc.balance:,.2f} {acc.currency}), risque {self.t.risk_percent} % par trade",
                      notify=True)
            return self.status()

    def disable(self, reason: str = "Robot désactivé") -> None:
        with self._lock:
            self._pending.clear()
            if self.enabled:
                self.enabled = False
                self._log("info", reason, notify=True)

    def close_all(self) -> list[str]:
        """Arrêt d'urgence : désactive le robot et clôture toutes ses positions."""
        with self._lock:
            self.enabled = False
            self._pending.clear()
            msgs = []
            b = self.broker
            for p in b.positions(self.cfg.symbol, self.t.magic):
                r = b.close(p, self.cfg.symbol, self.t.magic, self.t.deviation_points, "arret urgence")
                msgs.append(f"#{p.ticket} : {'clôturée' if r.ok else r.message}")
            self._log("err" if not msgs else "warn",
                      "ARRÊT D'URGENCE — robot désactivé" + (f", {len(msgs)} position(s) clôturée(s)" if msgs else ""),
                      notify=True)
            self.sync()
            return msgs

    def close_position(self, ticket: int) -> str:
        with self._lock:
            b = self.broker
            pos = next((p for p in b.positions(self.cfg.symbol, self.t.magic) if p.ticket == ticket), None)
            if pos is None:
                raise LookupError("Position introuvable (déjà fermée ?)")
            r = b.close(pos, self.cfg.symbol, self.t.magic, self.t.deviation_points, "manuel")
            self._log("info" if r.ok else "err",
                      f"Clôture manuelle #{ticket} : {'OK' if r.ok else r.message}")
            self.sync()
            if not r.ok:
                raise RuntimeError(r.message)
            return "OK"

    # ------------------------------------------------------------------ entrée
    def on_signal(self, sig: dict) -> list[dict]:
        """Appelé pour chaque NOUVEAU signal détecté en direct. Renvoie les ordres passés."""
        with self._lock:
            if not self.enabled:
                if sig.get("score", 0) >= robot_min_score(self.cfg) and \
                        not self.store.q("SELECT 1 FROM skips WHERE key=? LIMIT 1", (sig.get("key", ""),)):
                    self._skip(sig, "le robot est désactivé (onglet Robot → Activer)", "disabled")
                return []
            try:
                return self._execute(sig)
            except Exception as e:  # noqa: BLE001 — une erreur ne doit jamais casser l'analyse
                self.last_error = str(e)
                self._log("err", f"Erreur d'exécution : {e}", notify=True)
                return []

    def _execute(self, sig: dict) -> list[dict]:
        t, sym, pip = self.t, self.cfg.symbol, self.cfg.pip
        d = int(sig["direction"])
        lo = robot_min_score(self.cfg)
        if sig["score"] < lo:
            self._skip(sig, f"score {sig['score']}/10 sous le minimum du robot ({lo:g}) — "
                            f"active « Trader aussi les scores faibles » ou baisse « Score minimum pour trader »", "score")
            return []
        if self.store.q("SELECT 1 FROM trades WHERE key=? LIMIT 1", (sig["key"],)):
            return []
        age = (_now() - datetime.fromisoformat(sig["close_time"])).total_seconds()
        if age > FRESH_SECONDS:
            self._skip(sig, f"signal trop ancien ({int(age)} s) — données en retard ?", "stale")
            return []

        # ---- contrôle qualité : statistiques, M15/H1/H4, annonces, volatilité
        passed, checks = self.quality.check(sig, self.cfg)
        self._emit({"type": "quality", "report": self.quality.last})
        if not passed:
            bad = [c for c in checks if c["status"] == "fail"]
            failed = [f"{c['label']} : {c['detail']}" for c in bad]
            self._skip(sig, "contrôle qualité — " + " ; ".join(failed), bad[0]["id"] if bad else "autre")
            return []
        analysis = json.dumps(checks, ensure_ascii=False)

        b = self.broker
        acc = b.account()
        if not acc.is_demo and not t.allow_live:
            self.disable("Robot désactivé : compte RÉEL détecté sans autorisation")
            return []
        if not acc.trade_allowed:
            self._skip(sig, acc.trade_allowed_reason, "account")
            return []
        if self._check_daily_loss(acc):
            self._skip(sig, self.paused, "limits")
            return []
        today = self.store.q("SELECT COUNT(DISTINCT key) n FROM trades WHERE status != 'erreur' AND time >= ?",
                             (_day_start().isoformat(),))[0]["n"]
        if today + len(self._pending) >= t.max_trades_per_day:
            self._skip(sig, f"limite de {t.max_trades_per_day} trades par jour atteinte", "limits")
            return []
        open_keys = {r["key"] for r in self.store.q("SELECT key FROM trades WHERE status='ouvert'")}
        open_keys |= set(self._pending)
        if len(open_keys) >= t.max_open_trades:
            self._skip(sig, f"déjà {len(open_keys)} trade(s) ouvert(s) (max {t.max_open_trades})", "limits")
            return []

        mg = self.quality.edge.management(self.cfg)
        if mg.target_r > 0:                                # encaisser vite : objectif unique proche
            from .backtest import quick_target
            tp = quick_target(sig["entry"], sig["sl"], int(sig["direction"]), mg.target_r)
            sig = dict(sig, tp1=tp, tp2=tp)
        if mg.entry_mode == "pullback":
            return self._arm_pullback(sig, checks, analysis)
        return self._place(sig, checks, analysis, acc)

    # ------------------------------------------------------------------ entrée sur repli
    def _arm_pullback(self, sig: dict, checks: list, analysis: str) -> list[dict]:
        """Entrée de précision : attend un repli du prix vers le stop avant d'entrer.

        Ordre limite « virtuel » surveillé toutes les secondes : quand le prix atteint le
        niveau, l'ordre part au marché (aucune dépendance au support des ordres en attente
        du courtier). Annulé si le prix touche TP1 avant, ou après `pullback_bars` bougies.
        """
        t, d = self.t, int(sig["direction"])
        risk = abs(sig["entry"] - sig["sl"])
        level = round(sig["entry"] - d * t.pullback_r * risk, 5)
        expires = datetime.fromisoformat(sig["close_time"]) + timedelta(minutes=5 * t.pullback_bars)
        self._pending[sig["key"]] = {"sig": sig, "checks": checks, "analysis": analysis,
                                     "level": level, "expires": expires}
        self._log("info", f"{sig['side']} {sig['pattern']} {sig['score']}/10 : entrée en attente d'un repli à "
                          f"{level:.2f} (signal {sig['entry']:.2f}, stop {sig['sl']:.2f}) jusqu'à {expires:%H:%M} UTC",
                  notify=True)
        self._emit({"type": "pending", "key": sig["key"], "level": level, "expires": expires.isoformat()})
        return []

    def _watch_pending(self) -> None:
        if not self._pending:
            return
        b, sym = self.broker, self.cfg.symbol
        tick = b.tick(sym)
        for key, p in list(self._pending.items()):
            sig, d, level = p["sig"], int(p["sig"]["direction"]), p["level"]
            price = tick.ask if d == 1 else tick.bid
            if (price - sig["tp1"]) * d >= 0:
                del self._pending[key]
                self._skip(sig, f"le prix a atteint TP1 sans revenir à {level:.2f} — entrée annulée", "pullback")
            elif (price - sig["sl"]) * d <= 0:
                del self._pending[key]
                self._skip(sig, "le prix a atteint le stop avant l'entrée — entrée annulée", "pullback")
            elif (price - level) * d <= 0:
                del self._pending[key]
                if not self.enabled:
                    continue
                filled = dict(sig, entry=level)
                acc = b.account()
                done = self._place(filled, p["checks"], p["analysis"], acc)
                if done:
                    self._log("ok", f"Repli atteint : entrée à {done[0]['price']:.2f} au lieu de "
                                    f"{sig['entry']:.2f} (stop plus proche, même objectif)")
            elif _now() >= p["expires"]:
                del self._pending[key]
                self._skip(sig, f"repli à {level:.2f} non atteint en {self.t.pullback_bars} bougies — entrée annulée",
                           "pullback")

    # ------------------------------------------------------------------ envoi des ordres
    def _place(self, sig: dict, checks: list, analysis: str, acc, manual: bool = False) -> list[dict]:
        t, sym, pip = self.t, self.cfg.symbol, self.cfg.pip
        d = int(sig["direction"])
        b = self.broker
        spec = b.symbol(sym)
        if not spec.tradable:
            self._skip(sig, "symbole non négociable actuellement", "market")
            return []
        tick = b.tick(sym)
        spread = (tick.ask - tick.bid) / pip
        if spread > t.max_spread_pips:
            self._skip(sig, f"spread trop élevé ({spread:.1f} pips > {t.max_spread_pips})", "market")
            return []
        price = tick.ask if d == 1 else tick.bid
        slip = abs(price - sig["entry"]) / pip
        if manual:          # ordre manuel : le prix peut avoir bougé, mais il doit rester du potentiel
            if (sig["tp1"] - price) * d < 0.5 * abs(price - sig["sl"]):
                self._skip(sig, "le prix est déjà trop près de TP1 (moins de 0,5 R de potentiel)", "market")
                return []
        elif slip > t.max_slippage_pips:
            self._skip(sig, f"le prix a trop bougé depuis le signal ({slip:.1f} pips)", "market")
            return []
        sl, tp1, tp2 = sig["sl"], sig["tp1"], sig["tp2"]
        min_dist = spec.stops_level_points * spec.point
        if (d == 1 and not sl < price - min_dist) or (d == -1 and not sl > price + min_dist) \
                or (d == 1 and tp1 <= price) or (d == -1 and tp1 >= price):
            self._skip(sig, "stop ou objectif déjà dépassé par le prix actuel", "market")
            return []

        # ---- taille de position
        k = risk_factor(sig["score"], self.cfg)           # scores faibles : risque réduit
        risk_money = acc.balance * t.risk_percent / 100 * k
        lpl = b.loss_per_lot(sym, d, price, sl)
        if lpl <= 0:
            self._skip(sig, "valeur du pip introuvable", "market")
            return []
        volume = min(floor_step(risk_money / lpl, spec.volume_step), t.max_lots, spec.volume_max)
        volume = floor_step(volume, spec.volume_step)
        if volume < spec.volume_min:
            self._skip(sig, f"risque de {risk_money:.2f} {acc.currency} trop faible pour le lot minimum "
                            f"{spec.volume_min} (il faudrait {spec.volume_min * lpl:.2f} {acc.currency})", "size")
            return []

        legs = [(volume, tp1, 0)]
        if t.split_targets and abs(tp2 - tp1) > pip / 10 and volume >= 2 * spec.volume_min:
            v1 = max(spec.volume_min, floor_step(volume / 2, spec.volume_step))
            v2 = floor_step(volume - v1, spec.volume_step)
            if v2 >= spec.volume_min:
                legs = [(v1, tp1, 1), (v2, tp2, 2)]

        done = []
        side_txt = "ACHAT" if d == 1 else "VENTE"
        now = _now().isoformat()
        for vol, tp, leg in legs:
            r = b.open(sym, d, vol, sl, tp, f"M5A {sig['key']}-{leg}", t.magic, t.deviation_points)
            status = "ouvert" if r.ok else "erreur"
            self.store.x("""INSERT INTO trades(key, leg, mode, ticket, side, volume, price, sl, tp, tp1, status,
                            message, time, analysis, sl0, peak, risk_factor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (sig["key"], leg, t.mode, r.ticket, side_txt, vol, r.price, sl, tp, tp1, status,
                          "" if r.ok else r.message, now, analysis, sl, r.price, k))
            if r.ok:
                done.append({"ticket": r.ticket, "volume": vol, "price": r.price, "tp": tp, "leg": leg})
            else:
                self._log("err", f"Ordre {side_txt} {vol} lot refusé : {r.message}", notify=True)
        if done:
            total = sum(x["volume"] for x in done)
            tps = " / ".join("%.2f" % x["tp"] for x in done)
            n_ok = sum(c["status"] == "ok" for c in checks)
            self._log("ok", f"{side_txt} {sym} {total:g} lot @ {done[0]['price']:.2f} — SL {sl:.2f} · "
                            f"TP {tps} — risque {risk_money:.2f} {acc.currency} — analyse ✓ {n_ok}/{len(checks)} "
                            f"contrôles ({sig['pattern']}, {sig['score']}/10)", notify=True)
            self._emit({"type": "trade", "side": side_txt, "volume": total, "price": done[0]["price"],
                        "sl": sl, "tps": [x["tp"] for x in done], "mode": t.mode})
        return done

    # ------------------------------------------------------------------ gestion
    def _check_daily_loss(self, acc) -> bool:
        if self.paused and self.paused_day == _day_start():
            return True
        self.paused = None
        start = _day_start()
        b = self.broker
        pnl = b.realized_since(start, self.t.magic) + sum(p.profit for p in b.positions(self.cfg.symbol, self.t.magic))
        limit = acc.balance * self.t.daily_loss_limit_percent / 100
        if pnl <= -limit:
            self.paused, self.paused_day = (f"perte du jour {pnl:.2f} {acc.currency} ≥ limite de "
                                            f"{self.t.daily_loss_limit_percent} %, reprise demain"), start
            self._log("err", f"Robot en pause : {self.paused}", notify=True)
            return True
        return False

    def sync(self) -> None:
        """Marque comme fermées les positions disparues du courtier."""
        b = self.broker
        live = {p.ticket for p in b.positions(self.cfg.symbol, self.t.magic)}
        for row in self.store.q("SELECT * FROM trades WHERE status='ouvert' AND mode=?", (self.t.mode,)):
            if row["ticket"] not in live:
                profit = b.closed_profit(row["ticket"])
                self.store.x("UPDATE trades SET status='fermé', profit=?, close_time=? WHERE id=?",
                             (profit, _now().isoformat(), row["id"]))
                if profit is not None:
                    self._log("ok" if profit >= 0 else "warn",
                              f"Position #{row['ticket']} ({row['side']} {row['volume']:g} lot) fermée : "
                              f"{profit:+.2f}", notify=True)

    def manage(self, market: dict | None = None) -> None:
        """Appelé régulièrement : break-even, stop suiveur, retournement, sortie au temps.

        `market` (à chaque clôture de bougie) : close/high/low et `candidate`, le signal
        (même bloqué) détecté sur la dernière bougie close, pour la sortie sur retournement.
        """
        with self._lock:
            if market and self.t.mode == "paper":
                self.paper.update_market(market["close"], market.get("high"), market.get("low"))
            try:
                self._watch_pending()
            except Exception as e:  # noqa: BLE001
                log.warning("Surveillance des entrées en attente : %s", e)
            if not self.store.q("SELECT 1 FROM trades WHERE status='ouvert' LIMIT 1"):
                return
            try:
                self._manage((market or {}).get("reversal"))
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.warning("Gestion des positions : %s", e)

    def _manage(self, reversal: dict | None = None) -> None:
        b, t, sym = self.broker, self.t, self.cfg.symbol
        mg = self.quality.edge.management(self.cfg)
        positions = b.positions(sym, t.magic)
        if mg.reversal_exit and reversal and positions:
            positions = self._reversal(positions, reversal, mg)
        rows = {r["ticket"]: r for r in self.store.q("SELECT * FROM trades WHERE status='ouvert'")}
        tick = b.tick(sym) if positions else None
        live = {p.ticket for p in positions}
        max_hold = timedelta(minutes=5 * self.cfg.strategy.max_hold_bars)
        for p in positions:
            row = rows.get(p.ticket)
            if row is None:
                continue
            if t.close_after_max_hold and _now() - p.time_open >= max_hold:
                r = b.close(p, sym, t.magic, t.deviation_points, "duree max")
                self._log("info" if r.ok else "err",
                          f"Position #{p.ticket} clôturée après {self.cfg.strategy.max_hold_bars * 5} min"
                          + ("" if r.ok else f" — échec : {r.message}"), notify=r.ok)
                continue
            if t.breakeven and row["leg"] == 2 and not row["be_done"]:
                price = tick.bid if p.side == 1 else tick.ask
                reached = (price >= row["tp1"]) if p.side == 1 else (price <= row["tp1"])
                leg1 = self.store.q("SELECT ticket FROM trades WHERE key=? AND leg=1 AND status!='erreur'",
                                    (row["key"],))
                tp1_closed = bool(leg1) and all(r1["ticket"] not in live for r1 in leg1)
                already = (p.sl >= p.price_open) if p.side == 1 else (0 < p.sl <= p.price_open)
                if (reached or tp1_closed) and not already:
                    r = b.modify(p, sym, p.price_open, p.tp)
                    if r.ok:
                        self.store.x("UPDATE trades SET be_done=1, sl=? WHERE id=?", (p.price_open, row["id"]))
                        self._log("ok", f"TP1 atteint : stop de #{p.ticket} déplacé au prix d'entrée "
                                        f"{p.price_open:.2f}", notify=True)
                    else:
                        self._log("err", f"Break-even #{p.ticket} impossible : {r.message}")
                elif already:
                    self.store.x("UPDATE trades SET be_done=1 WHERE id=?", (row["id"],))
            if mg.trailing and tick is not None:
                self._trail(p, row, tick, mg)
        self.sync()

    def _trail(self, p, row: dict, tick, mg) -> None:
        """Stop suiveur : verrouille les gains en suivant le meilleur prix atteint."""
        sl0 = row.get("sl0") or row["sl"]
        risk = abs(p.price_open - sl0)
        if risk <= 0:
            return
        d = p.side
        price = tick.bid if d == 1 else tick.ask
        peak = row.get("peak") or p.price_open
        if (price - peak) * d > 0:
            peak = price
            self.store.x("UPDATE trades SET peak=? WHERE id=?", (peak, row["id"]))
        if (peak - p.price_open) * d < mg.trail_start_r * risk:
            return
        spec = self.broker.symbol(self.cfg.symbol)
        new_sl = round(peak - d * mg.trail_distance_r * risk, spec.digits)
        min_gap = max(spec.stops_level_points, 1) * spec.point      # distance minimale du courtier
        if (price - new_sl) * d < min_gap:
            new_sl = round(price - d * min_gap, spec.digits)
        current = p.sl or sl0
        if (new_sl - current) * d < 0.1 * risk:                      # pas de micro-modifications
            return
        r = self.broker.modify(p, self.cfg.symbol, new_sl, p.tp)
        if r.ok:
            first = (current - p.price_open) * d < 0.25 * risk
            locked = (new_sl - p.price_open) * d / risk
            self.store.x("UPDATE trades SET sl=? WHERE id=?", (new_sl, row["id"]))
            self._log("ok", f"Stop suiveur #{p.ticket} → {new_sl:.2f} (gain verrouillé {locked:+.2f} R, "
                            f"meilleur prix {peak:.2f})", notify=first)
        else:
            self._log("warn", f"Stop suiveur #{p.ticket} non déplacé : {r.message}")

    def _reversal(self, positions: list, info: dict, mg) -> list:
        """Coupe les positions contre lesquelles un retournement vient d'être détecté."""
        from .backtest import reversal_reason

        if info.get("time") == getattr(self, "_last_reversal", None):
            return positions                       # bougie déjà examinée
        self._last_reversal = info.get("time")
        closed_at = datetime.fromisoformat(info["time"])
        keep = []
        for p in positions:
            opened = pd.Timestamp(p.time_open).floor("5min")
            why = reversal_reason(info, p.side, mg, opened) if p.time_open < closed_at else None
            if why is None:
                keep.append(p)
                continue
            r = self.broker.close(p, self.cfg.symbol, self.t.magic, self.t.deviation_points, "retournement")
            self._log("warn" if r.ok else "err",
                      f"Retournement détecté ({why}) — position #{p.ticket} coupée ({p.profit:+.2f})"
                      + ("" if r.ok else f" — échec : {r.message}"), notify=r.ok)
            if not r.ok:
                keep.append(p)
        return keep

    # ------------------------------------------------------------------ ordre manuel (bouton « Placer »)
    MANUAL_MAX_AGE_MIN = 30

    def _manual_sig(self, sig: dict) -> dict:
        mg = self.quality.edge.management(self.cfg)
        if mg.target_r > 0:
            from .backtest import quick_target
            tp = quick_target(sig["entry"], sig["sl"], int(sig["direction"]), mg.target_r)
            sig = dict(sig, tp1=tp, tp2=tp)
        return sig

    def _manual_guard(self, sig: dict):
        if self.store.q("SELECT 1 FROM trades WHERE key=? AND status != 'erreur' LIMIT 1", (sig["key"],)):
            raise ValueError("Ce signal a déjà été placé")
        age = (_now() - datetime.fromisoformat(sig["close_time"])).total_seconds() / 60
        if age > self.MANUAL_MAX_AGE_MIN:
            raise ValueError(f"Signal trop ancien ({age:.0f} min, maximum {self.MANUAL_MAX_AGE_MIN} min)")
        acc = self.broker.account()
        if not acc.is_demo and not self.t.allow_live:
            raise PermissionError("Compte RÉEL : autorise-le d'abord dans les réglages du robot")
        if not acc.trade_allowed:
            raise RuntimeError(acc.trade_allowed_reason)
        if self.paused:
            raise RuntimeError(self.paused)
        return acc

    def preview_manual(self, sig: dict) -> dict:
        """Ce que ferait le bouton « Placer » (prix, lot, montant risqué), sans rien envoyer."""
        with self._lock:
            sig = self._manual_sig(sig)
            acc = self._manual_guard(sig)
            b, sym, pip, d = self.broker, self.cfg.symbol, self.cfg.pip, int(sig["direction"])
            spec, tick = b.symbol(sym), b.tick(sym)
            price = tick.ask if d == 1 else tick.bid
            k = risk_factor(sig["score"], self.cfg)
            risk_money = acc.balance * self.t.risk_percent / 100 * k
            lpl = b.loss_per_lot(sym, d, price, sig["sl"])
            volume = floor_step(min(floor_step(risk_money / lpl, spec.volume_step) if lpl > 0 else 0,
                                    self.t.max_lots, spec.volume_max), spec.volume_step)
            passed, checks = self.quality.check(sig, self.cfg)
            warn = [f"{c['label']} : {c['detail']}" for c in checks if c["status"] == "fail"]
            return {"key": sig["key"], "side": sig["side"], "pattern": sig["pattern"], "score": sig["score"],
                    "price": price, "signal_entry": sig["entry"], "moved_pips": round((price - sig["entry"]) * d / pip, 1),
                    "spread_pips": round((tick.ask - tick.bid) / pip, 1), "sl": sig["sl"], "tp1": sig["tp1"],
                    "tp2": sig["tp2"], "sl_pips": round(abs(price - sig["sl"]) / pip, 1),
                    "volume": volume, "volume_min": spec.volume_min, "risk_money": round(volume * lpl, 2),
                    "currency": acc.currency, "account": "démo" if acc.is_demo else "RÉEL",
                    "quality_warnings": warn, "ok": volume >= spec.volume_min}

    def place_manual(self, sig: dict) -> list[dict]:
        """Bouton « Placer » : place le signal tout de suite, même robot désactivé.

        Garde-fous conservés : compte démo sauf autorisation, Algo Trading, pause de perte
        journalière, spread, lot selon le risque, potentiel restant jusqu'à TP1. Les filtres de
        sélection du robot (score minimum, statistiques, tendances, IA) ne bloquent pas : tu
        décides, ils sont seulement enregistrés avec l'ordre.
        """
        with self._lock:
            sig = self._manual_sig(sig)
            acc = self._manual_guard(sig)
            _, checks = self.quality.check(sig, self.cfg)
            checks = checks + [{"id": "manual", "label": "Ordre manuel", "status": "ok",
                                "detail": "placé avec le bouton « Placer » (filtres indicatifs seulement)"}]
            analysis = json.dumps(checks, ensure_ascii=False)
            self._last_skip = None
            done = self._place(sig, checks, analysis, acc, manual=True)
            if not done:
                raise RuntimeError(self._last_skip or "ordre non placé")
            self._log("ok", f"Ordre MANUEL placé : {sig['side']} {sig['pattern']} {sig['score']}/10", notify=True)
            return done

    # ------------------------------------------------------------------ test d'exécution
    def test_order(self) -> dict:
        """Ouvre puis ferme aussitôt une position au lot minimum, sur compte DÉMO uniquement.

        Vérifie toute la chaîne d'exécution (connexion, Algo Trading, remplissage, stops)
        indépendamment des signaux et des filtres.
        """
        with self._lock:
            b, sym, t, pip = self.broker, self.cfg.symbol, self.t, self.cfg.pip
            acc = b.account()
            if not acc.is_demo:
                raise PermissionError("Test d'ordre réservé aux comptes DÉMO")
            if not acc.trade_allowed:
                raise RuntimeError(acc.trade_allowed_reason)
            spec, tick = b.symbol(sym), b.tick(sym)
            price = tick.ask
            sl = round(price - 20 * pip, spec.digits)
            tp = round(price + 20 * pip, spec.digits)
            r = b.open(sym, 1, spec.volume_min, sl, tp, "M5A test", t.magic, t.deviation_points)
            if not r.ok:
                self._log("err", f"Test d'ordre : REFUSÉ par le courtier — {r.message}", notify=True)
                return {"ok": False, "step": "ouverture", "message": r.message}
            pos = next((p for p in b.positions(sym, t.magic) if p.ticket == r.ticket), None)
            c = b.close(pos, sym, t.magic, t.deviation_points, "test") if pos else None
            closed = bool(c and c.ok)
            self._log("ok" if closed else "warn",
                      f"Test d'ordre réussi : achat {spec.volume_min} lot #{r.ticket} ouvert à {r.price:.2f}"
                      + (" puis fermé" if closed else " — fermeture à faire à la main dans MT5"), notify=True)
            return {"ok": True, "ticket": r.ticket, "price": r.price, "volume": spec.volume_min, "closed": closed,
                    "message": "La chaîne d'exécution fonctionne." + ("" if closed else " Ferme la position dans MT5.")}

    def readiness(self, acc=None) -> list[dict]:
        """Ce qui empêche (ou peut empêcher) le robot de trader les signaux, du plus grave au moins grave."""
        t, out = self.t, []
        add = lambda level, text, fix="": out.append({"level": level, "text": text, "fix": fix})  # noqa: E731
        if not self.enabled:
            add("err", "Le robot est désactivé : les signaux sont affichés mais aucun ordre n'est passé.",
                "Clique sur « Activer le robot ».")
        if self.paused:
            add("err", self.paused, "Reprise automatique le lendemain.")
        if acc is not None:
            if not acc.is_demo and not t.allow_live:
                add("err", "Compte RÉEL connecté sans autorisation : le robot refuse de trader.",
                    "Utilise un compte démo, ou autorise le réel dans les réglages.")
            if not acc.trade_allowed:
                add("err", acc.trade_allowed_reason, "Dans MetaTrader 5, clique sur « Algo Trading » (bouton vert).")
        q = self.quality
        if t.quality_edge and q.edge.data:
            sm = q.edge.summary(t)
            tradable = len(sm["qualified"]) + (0 if t.edge_block_unproven else len(sm["unproven"]))
            if tradable == 0:
                add("err", "Aucune figure ne peut être tradée d'après les statistiques de ton historique.",
                    "Désactive « Figure rentable sur l'historique » ou baisse l'espérance minimale.")
            elif sm["blocked"]:
                add("info", f"{len(sm['blocked'])} figure(s) écartée(s) car perdantes sur ton historique : "
                            + ", ".join(sm["blocked"][:6]) + ".")
        lo = robot_min_score(self.cfg)
        if lo > self.cfg.strategy.min_score:
            add("warn", f"Les signaux de score {self.cfg.strategy.min_score:g} à {lo - 0.5:g} sont affichés mais "
                        f"le robot ne trade qu'à partir de {lo:g}.",
                "Active « Trader aussi les scores faibles » (risque réduit) ou baisse le score minimum du robot.")
        if t.mode == "mt5" and self.cfg.source not in ("auto", "mt5"):
            add("warn", "Les signaux viennent d'une autre source que MetaTrader 5 : prix décalés et signaux en retard.",
                "Réglages → Données → Source : Auto ou MetaTrader 5.")
        for st in self.skip_stats(7)[:3]:
            if st["code"] != "disabled":
                add("info", f"7 derniers jours : {st['count']} signal(aux) refusé(s) — {st['label']}.")
        return out

    # ------------------------------------------------------------------ état
    def status(self) -> dict:
        t = self.t
        out = {"enabled": self.enabled, "paused": self.paused, "mode": t.mode, "broker": None,
               "account": None, "positions": [], "error": None, "settings": dict(vars(t))}
        try:
            b = self.broker
            out["broker"] = b.name
            acc = b.account()
            out["account"] = acc.to_dict()
            out["positions"] = [p.to_dict() for p in b.positions(self.cfg.symbol, t.magic)]
            start = _day_start()
            out["today"] = {
                "realized": round(b.realized_since(start, t.magic), 2),
                "floating": round(sum(p["profit"] for p in out["positions"]), 2),
                "trades": self.store.q("SELECT COUNT(DISTINCT key) n FROM trades WHERE status != 'erreur' "
                                       "AND time >= ?", (start.isoformat(),))[0]["n"],
                "loss_limit": round(acc.balance * t.daily_loss_limit_percent / 100, 2),
            }
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
        out["trades"] = self.store.q("SELECT * FROM trades ORDER BY id DESC LIMIT 100")
        for row in out["trades"]:
            try:
                row["analysis"] = json.loads(row.get("analysis") or "[]")
            except ValueError:
                row["analysis"] = []
        out["quality"] = self.quality.status(self.cfg)
        acc_obj = None
        try:
            acc_obj = self.broker.account() if out.get("account") else None
        except Exception:  # noqa: BLE001
            pass
        out["readiness"] = self.readiness(acc_obj)
        out["pending"] = [{"key": k, "side": p["sig"]["side"], "pattern": p["sig"]["pattern"], "level": p["level"],
                           "entry": p["sig"]["entry"], "expires": p["expires"].isoformat()}
                          for k, p in self._pending.items()]
        out["skips"] = self.skip_stats(7)
        out["log"] = self.store.q("SELECT * FROM bot_log ORDER BY id DESC LIMIT 100")
        out["last_error"] = self.last_error
        return out

    def close(self) -> None:
        self.store.close()

