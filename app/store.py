"""SQLite store: general settings, printers, storage locations, the one pending scan, and a log."""
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:32].strip("-")


KLIPPER = "moonraker"
BAMBUDDY = "bambuddy"


@dataclass
class Printer:
    """A Klipper printer (tools T0..T<n-1> via Moonraker) or a Bambu AMS handled by Bambuddy
    (slots 1..n via Bambuddy's API). `moonraker_url`/`api_key` hold Bambuddy's URL and key for
    the latter, and `remote_id` is the printer's id in Bambuddy."""
    slug: str
    name: str
    moonraker_url: str
    api_key: str
    num_tools: int
    save_variables: bool
    macro_variable: bool
    kind: str = KLIPPER
    remote_id: int = 0

    @property
    def is_ams(self) -> bool:
        return self.kind == BAMBUDDY

    def tool_numbers(self) -> range:
        """Tool numbers as written on tags: T0-based for Klipper, slot 1-based for an AMS."""
        return range(1, self.num_tools + 1) if self.is_ams else range(self.num_tools)

    def tool_label(self, tool: int) -> str:
        if not self.is_ams:
            return f"T{tool}"
        if self.num_tools <= 4:
            return f"AMS {tool}"
        return f"AMS {(tool - 1) // 4 + 1}-{(tool - 1) % 4 + 1}"   # unit-slot with several AMS units

    def ams_slot(self, tool: int) -> tuple[int, int]:
        """(ams_id, tray_id) in Bambuddy for 1-based slot `tool`: four trays per AMS unit."""
        return (tool - 1) // 4, (tool - 1) % 4


@dataclass
class Location:
    slug: str
    name: str  # exactly what Spoolman's location field gets


@dataclass
class Pending:
    kind: str   # "spool", "tool" or "location"
    ref: str    # "42" | "sv08:3" | "dry-box-1"
    ts: float


class Store:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "spooltag.db")
        with self._conn() as db:
            cols = [r[1] for r in db.execute("PRAGMA table_info(pending)")]
            if cols and "ref" not in cols:          # 0.1 schema: pending is transient, just rebuild
                db.execute("DROP TABLE pending")
            db.execute("CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY CHECK (id = 1),"
                       " kind TEXT NOT NULL, ref TEXT NOT NULL, ts REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS log (ts REAL NOT NULL, spool_id INTEGER,"
                       " tool INTEGER, action TEXT NOT NULL, detail TEXT)")
            if "target" not in [r[1] for r in db.execute("PRAGMA table_info(log)")]:
                db.execute("ALTER TABLE log ADD COLUMN target TEXT")
            db.execute("CREATE TABLE IF NOT EXISTS printers (slug TEXT PRIMARY KEY, name TEXT NOT NULL,"
                       " moonraker_url TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '',"
                       " num_tools INTEGER NOT NULL, save_variables INTEGER NOT NULL DEFAULT 0,"
                       " macro_variable INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0)")
            pcols = [r[1] for r in db.execute("PRAGMA table_info(printers)")]
            if "kind" not in pcols:
                db.execute(f"ALTER TABLE printers ADD COLUMN kind TEXT NOT NULL DEFAULT '{KLIPPER}'")
            if "remote_id" not in pcols:
                db.execute("ALTER TABLE printers ADD COLUMN remote_id INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS locations (slug TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,"
                       " position INTEGER NOT NULL DEFAULT 0)")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    # General settings ----------------------------------------------------------------------

    def settings(self) -> dict[str, str]:
        with self._conn() as db:
            return dict(db.execute("SELECT key, value FROM settings").fetchall())

    def save_settings(self, values: dict[str, str], only_missing: bool = False) -> None:
        verb = "INSERT OR IGNORE" if only_missing else "INSERT OR REPLACE"
        with self._conn() as db:
            db.executemany(f"{verb} INTO settings (key, value) VALUES (?, ?)", list(values.items()))

    # Printers ------------------------------------------------------------------------------

    def printers(self) -> list[Printer]:
        with self._conn() as db:
            rows = db.execute("SELECT slug, name, moonraker_url, api_key, num_tools, save_variables,"
                              " macro_variable, kind, remote_id FROM printers ORDER BY position, rowid").fetchall()
        return [Printer(r[0], r[1], r[2], r[3], r[4], bool(r[5]), bool(r[6]), r[7], r[8]) for r in rows]

    def printer(self, slug: str) -> Optional[Printer]:
        return next((p for p in self.printers() if p.slug == slug), None)

    def save_printer(self, p: Printer) -> None:
        with self._conn() as db:
            exists = db.execute("SELECT 1 FROM printers WHERE slug = ?", (p.slug,)).fetchone()
            if exists:
                db.execute("UPDATE printers SET name=?, moonraker_url=?, api_key=?, num_tools=?,"
                           " save_variables=?, macro_variable=?, kind=?, remote_id=? WHERE slug=?",
                           (p.name, p.moonraker_url, p.api_key, p.num_tools, int(p.save_variables),
                            int(p.macro_variable), p.kind, p.remote_id, p.slug))
            else:
                pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM printers").fetchone()[0]
                db.execute("INSERT INTO printers (slug, name, moonraker_url, api_key, num_tools,"
                           " save_variables, macro_variable, position, kind, remote_id)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (p.slug, p.name, p.moonraker_url, p.api_key, p.num_tools,
                            int(p.save_variables), int(p.macro_variable), pos, p.kind, p.remote_id))

    def delete_printer(self, slug: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM printers WHERE slug = ?", (slug,))

    # Locations -----------------------------------------------------------------------------

    def locations(self) -> list[Location]:
        with self._conn() as db:
            rows = db.execute("SELECT slug, name FROM locations ORDER BY position, rowid").fetchall()
        return [Location(*r) for r in rows]

    def location(self, slug: str) -> Optional[Location]:
        return next((l for l in self.locations() if l.slug == slug), None)

    def add_location(self, loc: Location) -> None:
        with self._conn() as db:
            pos = db.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM locations").fetchone()[0]
            db.execute("INSERT INTO locations VALUES (?, ?, ?)", (loc.slug, loc.name, pos))

    def delete_location(self, slug: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM locations WHERE slug = ?", (slug,))

    # Pending scan --------------------------------------------------------------------------

    def set_pending(self, kind: str, ref: str) -> None:
        with self._conn() as db:
            db.execute("INSERT OR REPLACE INTO pending (id, kind, ref, ts) VALUES (1, ?, ?, ?)",
                       (kind, ref, time.time()))

    def get_pending(self, timeout: int) -> Optional[Pending]:
        with self._conn() as db:
            row = db.execute("SELECT kind, ref, ts FROM pending WHERE id = 1").fetchone()
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

    # Log -----------------------------------------------------------------------------------

    def log(self, action: str, spool_id: Optional[int] = None, target: str = "", detail: str = "") -> None:
        with self._conn() as db:
            db.execute("INSERT INTO log (ts, spool_id, action, detail, target) VALUES (?, ?, ?, ?, ?)",
                       (time.time(), spool_id, action, detail, target))
            db.execute("DELETE FROM log WHERE rowid NOT IN"
                       " (SELECT rowid FROM log ORDER BY ts DESC LIMIT 200)")

    def recent(self, limit: int = 15) -> list[tuple]:
        """(ts, spool_id, target, action, detail); 0.1 rows show their tool as "T<n>"."""
        with self._conn() as db:
            return db.execute("SELECT ts, spool_id, COALESCE(target, CASE WHEN tool IS NULL THEN ''"
                              " ELSE 'T' || tool END), action, detail FROM log"
                              " ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
