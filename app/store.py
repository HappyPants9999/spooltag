"""Tiny SQLite store: the one pending scan, and a log of assignments."""
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class Pending:
    kind: str  # "spool" or "tool"
    value: int
    ts: float


class Store:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "spooltag.db")
        with self._conn() as db:
            db.execute("CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY CHECK (id = 1),"
                       " kind TEXT NOT NULL, value INTEGER NOT NULL, ts REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS log (ts REAL NOT NULL, spool_id INTEGER,"
                       " tool INTEGER, action TEXT NOT NULL, detail TEXT)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def set_pending(self, kind: str, value: int) -> None:
        with self._conn() as db:
            db.execute("INSERT OR REPLACE INTO pending (id, kind, value, ts) VALUES (1, ?, ?, ?)",
                       (kind, value, time.time()))

    def get_pending(self, timeout: int) -> Optional[Pending]:
        with self._conn() as db:
            row = db.execute("SELECT kind, value, ts FROM pending WHERE id = 1").fetchone()
        if row is None:
            return None
        pending = Pending(*row)
        if time.time() - pending.ts > timeout:
            self.clear_pending()
            return None
        return pending

    def clear_pending(self) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM pending")

    def log(self, action: str, spool_id: Optional[int] = None, tool: Optional[int] = None,
            detail: str = "") -> None:
        with self._conn() as db:
            db.execute("INSERT INTO log (ts, spool_id, tool, action, detail) VALUES (?, ?, ?, ?, ?)",
                       (time.time(), spool_id, tool, action, detail))
            db.execute("DELETE FROM log WHERE rowid NOT IN"
                       " (SELECT rowid FROM log ORDER BY ts DESC LIMIT 200)")

    def recent(self, limit: int = 15) -> list[tuple]:
        with self._conn() as db:
            return db.execute("SELECT ts, spool_id, tool, action, detail FROM log"
                              " ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
