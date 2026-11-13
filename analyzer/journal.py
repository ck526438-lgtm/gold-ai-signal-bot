"""Journal des signaux (SQLite) avec suivi automatique du résultat."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    key        TEXT PRIMARY KEY,
    time       TEXT NOT NULL,       -- clôture de la bougie signal (UTC, ISO)
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,
    direction  INTEGER NOT NULL,
    pattern    TEXT NOT NULL,
    score      REAL NOT NULL,
    quality    TEXT NOT NULL,
    entry      REAL NOT NULL,
    sl         REAL NOT NULL,
    tp1        REAL NOT NULL,
    tp2        REAL NOT NULL,
    risk_pips  REAL NOT NULL,
    factors    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'en cours',
    result_r   REAL NOT NULL DEFAULT 0,
    tp1_hit    INTEGER NOT NULL DEFAULT 0,
    exit_time  TEXT,
    origin     TEXT NOT NULL DEFAULT 'direct',   -- direct | historique
    note       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(time);
"""


class Journal:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def add(self, sig_dict: dict, symbol: str, origin: str) -> bool:
        """Ajoute un signal. Renvoie False s'il était déjà enregistré."""
        with self._lock:
            cur = self._db.execute(
                """INSERT OR IGNORE INTO signals
                   (key, time, symbol, side, direction, pattern, score, quality, entry, sl,
                    tp1, tp2, risk_pips, factors, origin)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sig_dict["key"], sig_dict["close_time"], symbol, sig_dict["side"],
                 sig_dict["direction"], sig_dict["pattern"], sig_dict["score"],
                 sig_dict["quality"], sig_dict["entry"], sig_dict["sl"], sig_dict["tp1"],
                 sig_dict["tp2"], sig_dict["risk_pips"], json.dumps(sig_dict["factors"]), origin))
            self._db.commit()
            return cur.rowcount > 0

    def get(self, key: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM signals WHERE key=?", (key,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["factors"] = json.loads(d["factors"])
        d["close_time"] = d["time"]
        return d

    def open_keys(self) -> list[str]:
        with self._lock:
            return [r["key"] for r in self._db.execute(
                "SELECT key FROM signals WHERE status = 'en cours'")]

    def update_outcome(self, key: str, status: str, result_r: float, tp1_hit: bool,
                       exit_time: str | None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE signals SET status=?, result_r=?, tp1_hit=?, exit_time=? WHERE key=?",
                (status, result_r, int(tp1_hit), exit_time, key))
            self._db.commit()

    def set_note(self, key: str, note: str) -> None:
        with self._lock:
            self._db.execute("UPDATE signals SET note=? WHERE key=?", (note[:500], key))
            self._db.commit()

    def list(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM signals ORDER BY time DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["factors"] = json.loads(d["factors"])
            d["tp1_hit"] = bool(d["tp1_hit"])
            out.append(d)
        return out

    def stats(self) -> dict:
        rows = [r for r in self.list(100_000) if r["status"] != "en cours"]
        n = len(rows)
        if not n:
            return {"closed": 0, "open": len(self.open_keys())}
        rs = [r["result_r"] for r in rows]
        wins = [x for x in rs if x > 0]
        losses = [x for x in rs if x <= 0]
        return {
            "closed": n,
            "open": len(self.open_keys()),
            "win_rate": round(100 * len(wins) / n, 1),
            "total_r": round(sum(rs), 2),
            "expectancy_r": round(sum(rs) / n, 3),
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if sum(losses) else None,
        }

    def close(self) -> None:
        with self._lock:          # jamais pendant une écriture d'un autre thread
            self._db.close()
