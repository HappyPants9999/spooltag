"""SQLite store: general settings, admin sessions, printers, storage locations, each device's
pending scan, and a log.

The database file and its directory are kept private to the service user (0600 / 0700): they
hold the Moonraker/Bambuddy API keys in plain text."""
import contextlib
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


# Marks the file as a spooltag database ('SPTG') and its layout version, so a restore can tell a
# spooltag backup from another SQLite file, and refuse one written by a newer spooltag.
APP_ID = int.from_bytes(b"SPTG", "big")
SCHEMA_VERSION = 1

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


def _private(path: str, mode: int) -> None:
    """chmod if we own the file; an existing install's root-owned files are fixed by the entrypoint."""
    with contextlib.suppress(OSError):
        if os.stat(path).st_uid == os.geteuid():
            os.chmod(path, mode)


class Store:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        _private(data_dir, 0o700)
        self.path = os.path.join(data_dir, "spooltag.db")
        old_umask = os.umask(0o077)          # new database and journal files: owner only
        try:
            self._init_schema()
        finally:
            os.umask(old_umask)
        for suffix in ("", "-journal", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                _private(self.path + suffix, 0o600)

    def _init_schema(self) -> None:
        with self._conn() as db:
            # Pending scans are transient: the single-slot tables of 0.1-0.3 are just dropped.
            db.execute("DROP TABLE IF EXISTS pending")
            db.execute("CREATE TABLE IF NOT EXISTS scan_pending (device TEXT PRIMARY KEY,"
                       " kind TEXT NOT NULL, ref TEXT NOT NULL, ts REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY,"
                       " expires REAL NOT NULL)")
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
                db.execute("ALTER TABLE printers ADD COLUMN kind TEXT NOT NULL DEFAULT 'moonraker'")
            if "remote_id" not in pcols:
                db.execute("ALTER TABLE printers ADD COLUMN remote_id INTEGER NOT NULL DEFAULT 0")
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS locations (slug TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,"
                       " position INTEGER NOT NULL DEFAULT 0)")
            db.execute("PRAGMA application_id = 1397773383")       # APP_ID
            db.execute("PRAGMA user_version = 1")                  # SCHEMA_VERSION

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    @contextlib.contextmanager
    def _write_txn(self):
        """An IMMEDIATE transaction: concurrent writers wait instead of interleaving."""
        db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    # General settings ----------------------------------------------------------------------

    def settings(self) -> dict[str, str]:
        with self._conn() as db:
            return dict(db.execute("SELECT key, value FROM settings").fetchall())

    def save_settings(self, values: dict[str, str], only_missing: bool = False) -> None:
        sql = ("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)" if only_missing else
               "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)")
        with self._conn() as db:
            db.executemany(sql, list(values.items()))

    def get_setting(self, key: str) -> str:
        with self._conn() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else ""

    def delete_setting(self, key: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM settings WHERE key = ?", (key,))

    # Admin sessions (only the SHA-256 of each token is stored) ---------------------------

    def add_session(self, token_hash: str, expires: float) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO sessions (token_hash, expires) VALUES (?, ?)", (token_hash, expires))

    def session_valid(self, token_hash: str) -> bool:
        with self._conn() as db:
            row = db.execute("SELECT expires FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
        return bool(row and row[0] > time.time())

    def delete_session(self, token_hash: str) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_all_sessions(self) -> None:
        with self._conn() as db:
            db.execute("DELETE FROM sessions")

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

    # Pending scans: one per device (browser cookie), so people scanning at once don't mix ----

    def set_pending(self, device: str, kind: str, ref: str) -> None:
        with self._write_txn() as db:
            db.execute("INSERT OR REPLACE INTO scan_pending (device, kind, ref, ts) VALUES (?, ?, ?, ?)",
                       (device, kind, ref, time.time()))

    def get_pending(self, device: str, timeout: int) -> Optional[Pending]:
        with self._conn() as db:
            row = db.execute("SELECT kind, ref, ts FROM scan_pending WHERE device = ?", (device,)).fetchone()
        if row is None or time.time() - row[2] > timeout:
            return None
        return Pending(*row)

    def take_pending(self, device: str, timeout: int, kinds: tuple[str, ...]) -> Optional[Pending]:
        """Atomically remove and return this device's pending scan if it is one of `kinds`, so two
        requests can never both pair with the same first scan. Expired scans are purged."""
        with self._write_txn() as db:
            db.execute("DELETE FROM scan_pending WHERE ts < ?", (time.time() - max(timeout, 0),))
            row = db.execute("SELECT kind, ref, ts FROM scan_pending WHERE device = ?", (device,)).fetchone()
            if row is None or row[0] not in kinds or time.time() - row[2] > timeout:
                return None
            db.execute("DELETE FROM scan_pending WHERE device = ?", (device,))
        return Pending(*row)

    def clear_all_pending(self) -> None:
        with self._write_txn() as db:
            db.execute("DELETE FROM scan_pending")

    def clear_pending(self, device: str) -> None:
        with self._write_txn() as db:
            db.execute("DELETE FROM scan_pending WHERE device = ?", (device,))

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
