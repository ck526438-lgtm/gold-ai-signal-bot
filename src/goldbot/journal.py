"""Journal SQLite: signaux emis et suivi paper.

Le suivi paper n'est pas un gadget: c'est la seule facon de comparer le
comportement reel du bot au backtest avant de risquer de l'argent.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pandas as pd

from .config import get, resolve_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY,
  created_utc TEXT, bar_time TEXT, engine TEXT, symbol TEXT, side TEXT,
  score REAL, prob_up REAL, consensus INTEGER, entry REAL, sl REAL, tp REAL,
  rr REAL, atr REAL, block_reason TEXT, reasons TEXT, fired INTEGER, sent INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS paper_trades (
  id INTEGER PRIMARY KEY,
  engine TEXT, symbol TEXT, side TEXT, signal_time TEXT, entry_time TEXT,
  entry REAL, sl REAL, tp REAL, sl_dist REAL, tp_dist REAL,
  volume REAL, exit_time TEXT, exit REAL, reason TEXT, points REAL, r_multiple REAL,
  open INTEGER, status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""

# Les journaux deja ecrits avant le suivi "pending" n'ont pas ces colonnes.
_NEW_COLUMNS = {
    "signal_time": "TEXT", "sl_dist": "REAL", "tp_dist": "REAL", "status": "TEXT DEFAULT 'open'",
}


def connect(cfg: dict) -> sqlite3.Connection:
    path = resolve_path(get(cfg, "journal.db_path", "data/journal.sqlite"))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    have = {r["name"] for r in db.execute("PRAGMA table_info(paper_trades)")}
    for name, decl in _NEW_COLUMNS.items():
        if name not in have:
            db.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {decl}")
    db.commit()
    return db


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def put_state(db, key: str, value) -> None:
    db.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))
    db.commit()


def get_state(db, key: str, default=None):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def record_signal(db, sig: dict, fired: bool) -> int:
    cur = db.execute(
        "INSERT INTO signals(created_utc,bar_time,engine,symbol,side,score,prob_up,consensus,entry,sl,tp,rr,atr,block_reason,reasons,fired)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now(), sig["time"], sig["engine"], sig["symbol"], sig["side"], sig["score"],
         (sig.get("probabilities") or {}).get("up"), 1 if sig.get("consensus") else 0,
         sig["entry"], sig["sl"], sig["tp"], sig["rr"], sig["atr"], sig.get("block_reason", ""),
         json.dumps(sig.get("reasons", {})), 1 if fired else 0))
    db.commit()
    return cur.lastrowid


def last_fire_time(db, engine: str):
    row = db.execute("SELECT bar_time FROM signals WHERE engine=? AND fired=1 "
                     "ORDER BY bar_time DESC LIMIT 1", (engine,)).fetchone()
    return pd.Timestamp(row["bar_time"]) if row else None


def open_paper(db, engine: str, sig: dict, volume: float) -> int:
    """Enregistre le signal en attente d'execution.

    Le trade n'est pas ouvert au prix de cloture de la bougie de signal: comme
    dans le backtest, il se remplit a l'ouverture de la bougie suivante. Sans
    cette etape, le papier aurait l'air meilleur que le backtest.
    """
    cur = db.execute(
        "INSERT INTO paper_trades(engine,symbol,side,signal_time,entry_time,entry,sl,tp,"
        "sl_dist,tp_dist,volume,open,status) "
        "VALUES(?,?,?,?,NULL,NULL,NULL,NULL,?,?,?,1,'pending')",
        (engine, sig["symbol"], sig["side"], sig["time"],
         abs(float(sig["entry"]) - float(sig["sl"])), abs(float(sig["tp"]) - float(sig["entry"])), volume))
    db.commit()
    return cur.lastrowid


def fill_paper(db, trade_id: int, entry_time: str, entry: float, sl: float, tp: float) -> None:
    db.execute("UPDATE paper_trades SET status='open', entry_time=?, entry=?, sl=?, tp=? WHERE id=?",
               (entry_time, entry, sl, tp, trade_id))
    db.commit()


def list_open(db, engine: str | None = None):
    q = "SELECT * FROM paper_trades WHERE open=1" + (" AND engine=?" if engine else "")
    return [dict(r) for r in db.execute(q, (engine,) if engine else ()).fetchall()]


def close_paper(db, trade_id: int, exit_time: str, exit_price: float, reason: str,
                cost: float = 0.0) -> dict:
    row = dict(db.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone())
    side = 1 if row["side"] == "BUY" else -1
    gross = (exit_price - row["entry"]) * side
    points = gross - cost
    risk = row["sl_dist"] or abs(row["entry"] - row["sl"]) or 1e-9
    db.execute("UPDATE paper_trades SET open=0, status='closed', exit_time=?, exit=?, reason=?, "
               "points=?, r_multiple=? WHERE id=?",
               (exit_time, exit_price, reason, points, points / risk, trade_id))
    db.commit()
    return {**row, "exit_time": exit_time, "exit": exit_price, "reason": reason,
            "gross_points": round(gross, 3), "cost_points": round(cost, 3),
            "points": round(points, 3), "r_multiple": round(points / risk, 3)}


def paper_summary(db) -> dict:
    df = pd.read_sql_query("SELECT * FROM paper_trades WHERE open=0", db)
    if df.empty:
        return {"closed": 0, "open": len(list_open(db))}
    return {
        "closed": int(len(df)),
        "open": len(list_open(db)),
        "win_pct": round(float((df["points"] > 0).mean() * 100), 1),
        "total_points": round(float(df["points"].sum()), 2),
        "avg_r": round(float(df["r_multiple"].mean()), 3),
        "by_engine": df.groupby("engine")["points"].agg(["size", "sum"]).round(2).to_dict("index"),
    }
