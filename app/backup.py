"""Backup and restore of spooltag's own database.

A backup is a SQLite file holding everything spooltag stores: general settings, printers (with
their tag ids and API keys), storage locations, the admin password hash and the scan log. Login
sessions and pending scans are left out. Nothing in Spoolman, Moonraker, Bambuddy or on the
printers is backed up or changed.

Restore, in order, with nothing touched until step 3:
  1. validate the upload: SQLite header, integrity check, no views/triggers/unknown tables,
     spooltag's tables and columns, sane rows, not from a newer spooltag;
  2. prepare a migrated copy in a private scratch folder, without sessions or pending scans;
  3. save a safety copy of the current database to <data>/backups/ (the last 5 are kept);
  4. copy the prepared database into the live one with SQLite's backup API: one transaction, so
     other requests see either the old or the new configuration, and the file keeps its owner
     and 0600 mode;
  5. re-open it as the app does; if that fails, the safety copy is put back the same way.
"""
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .config import valid_timezone
from .store import APP_ID, BAMBUDDY, KLIPPER, SCHEMA_VERSION, SLUG_RE, Store

MAX_BACKUP_BYTES = 10 * 1024 * 1024
SQLITE_MAGIC = b"SQLite format 3\x00"
SAFETY_DIR = "backups"
KEEP_SAFETY = 5
TRANSIENT = ("sessions", "scan_pending", "pending")
CLEAR_TRANSIENT = {"sessions": "DELETE FROM sessions", "scan_pending": "DELETE FROM scan_pending",
                   "pending": "DELETE FROM pending"}
KNOWN_TABLES = {"settings", "printers", "locations", "log", *TRANSIENT}
TABLE_INFO = {"printers": 'PRAGMA table_info("printers")', "locations": 'PRAGMA table_info("locations")'}
REQUIRED = {"printers": {"slug", "name", "moonraker_url", "api_key", "num_tools"},
            "locations": {"slug", "name"}}


class BackupError(Exception):
    """A message safe to show on the Settings page (never contains backup contents)."""


def _ro_uri(path: str) -> str:
    return Path(path).resolve().as_uri() + "?mode=ro"


def _private_umask():
    class _Ctx:
        def __enter__(self):
            self.old = os.umask(0o077)

        def __exit__(self, *exc):
            os.umask(self.old)
    return _Ctx()


def filename(tz: str = "", now: Optional[float] = None) -> str:
    when = datetime.fromtimestamp(now or time.time(), ZoneInfo(tz) if tz and valid_timezone(tz) else None)
    return f"spooltag-backup-{when:%Y-%m-%d-%H%M}.db"


def snapshot(db_path: str) -> bytes:
    """A consistent copy of the running database, taken with SQLite's online backup API."""
    src = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=15)
    mem = sqlite3.connect(":memory:")
    try:
        src.backup(mem)
        tables = {r[0] for r in mem.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table, sql in CLEAR_TRANSIENT.items():
            if table in tables:
                mem.execute(sql)
        mem.commit()
        mem.execute("VACUUM")                 # no deleted session rows left in free pages
        return mem.serialize()
    finally:
        src.close()
        mem.close()


def _open_untrusted(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(_ro_uri(path), uri=True)
    if hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
        db.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
    db.execute("PRAGMA trusted_schema = OFF")
    db.execute("PRAGMA cell_size_check = ON")
    return db


def validate(path: str) -> dict[str, int]:
    """Raise BackupError unless `path` is an intact, compatible spooltag database.
    Returns counts of what it holds."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(100)
    except OSError as e:
        raise BackupError("The uploaded file couldn't be read.") from e
    if size == 0:
        raise BackupError("The uploaded file is empty.")
    if size > MAX_BACKUP_BYTES:
        raise BackupError("The uploaded file is too large to be a spooltag backup.")
    if not head.startswith(SQLITE_MAGIC):
        raise BackupError("That file isn't a spooltag backup (not a SQLite database).")
    try:
        db = _open_untrusted(path)
    except sqlite3.Error as e:
        raise BackupError("That file isn't a readable SQLite database.") from e
    try:
        try:
            check = db.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.DatabaseError as e:
            raise BackupError("The backup file is damaged.") from e
        if check != [("ok",)]:
            raise BackupError("The backup file is damaged (integrity check failed).")
        app_id = db.execute("PRAGMA application_id").fetchone()[0]
        if app_id not in (0, APP_ID):
            raise BackupError("That database belongs to another application.")
        if db.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
            raise BackupError("That backup was made by a newer version of spooltag; update spooltag first.")
        tables = set()
        for kind, name in db.execute("SELECT type, name FROM sqlite_master"):
            if kind == "table" and (name in KNOWN_TABLES or name == "sqlite_sequence"):
                tables.add(name)
            elif kind == "index" and name.startswith("sqlite_autoindex_"):
                continue
            else:
                raise BackupError("That database has objects a spooltag backup never has, so it wasn't used.")
        for table, columns in REQUIRED.items():
            if table not in tables:
                raise BackupError("That database isn't a spooltag backup (no printers or locations).")
            have = {r[1] for r in db.execute(TABLE_INFO[table])}
            if not columns <= have:
                raise BackupError("That backup's layout isn't one spooltag knows.")
        pcols = {r[1] for r in db.execute(TABLE_INFO["printers"])}
        printers = db.execute(
            "SELECT slug, name, moonraker_url, api_key, num_tools, kind FROM printers" if "kind" in pcols else
            "SELECT slug, name, moonraker_url, api_key, num_tools, 'moonraker' FROM printers").fetchall()
        for slug, name, url, key, tools, kind in printers:
            if not (isinstance(slug, str) and SLUG_RE.match(slug) and isinstance(name, str) and name
                    and isinstance(url, str) and isinstance(key, str) and isinstance(tools, int)
                    and 1 <= tools <= 32 and kind in (KLIPPER, BAMBUDDY)):
                raise BackupError("A printer in the backup has invalid data.")
        locations = db.execute("SELECT slug, name FROM locations").fetchall()
        for slug, name in locations:
            if not (isinstance(slug, str) and SLUG_RE.match(slug) and isinstance(name, str) and name):
                raise BackupError("A storage location in the backup has invalid data.")
        if "settings" in tables:
            for key, value in db.execute("SELECT key, value FROM settings"):
                if not (isinstance(key, str) and isinstance(value, str)):
                    raise BackupError("The backup's settings have invalid data.")
        return {"printers": len(printers), "locations": len(locations)}
    except sqlite3.Error as e:
        raise BackupError("The backup file couldn't be read as a spooltag database.") from e
    finally:
        db.close()


def _copy_db(src_path: str, dst_path: str, src_uri: bool = False) -> None:
    """Replace dst's contents with src's in one SQLite transaction (online backup API)."""
    src = sqlite3.connect(_ro_uri(src_path) if src_uri else src_path, uri=src_uri, timeout=15)
    dst = sqlite3.connect(dst_path, timeout=15)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def safety_backup(store: Store) -> str:
    """Copy the live database to <data>/backups/ (0700 folder, 0600 file); keep the newest 5."""
    folder = os.path.join(os.path.dirname(store.path), SAFETY_DIR)
    with _private_umask():
        os.makedirs(folder, mode=0o700, exist_ok=True)
        name = f"spooltag-before-restore-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}.db"
        path = os.path.join(folder, name)
        _copy_db(store.path, path, src_uri=True)
    os.chmod(folder, 0o700)
    os.chmod(path, 0o600)
    old = sorted(f for f in os.listdir(folder) if re.fullmatch(r"spooltag-before-restore-[0-9a-f-]+\.db", f))
    for stale in old[:-KEEP_SAFETY]:
        os.remove(os.path.join(folder, stale))
    return path


def _verify_live(store: Store, expect: dict[str, int]) -> None:
    fresh = Store(os.path.dirname(store.path))          # runs the same migrations as a start-up
    if len(fresh.printers()) != expect["printers"] or len(fresh.locations()) != expect["locations"]:
        raise BackupError("The restored database didn't read back as expected.")


def restore(store: Store, upload_path: str) -> tuple[dict[str, int], str]:
    """Validate, prepare, safety-copy, swap in, verify. Returns (counts, safety copy path).
    Raises BackupError; the live database is unchanged (or put back) whenever it does."""
    expect = validate(upload_path)
    data_dir = os.path.dirname(store.path)
    with _private_umask():
        scratch = tempfile.mkdtemp(prefix=".restore-", dir=data_dir)
    try:
        prepared = os.path.join(scratch, "spooltag.db")
        _copy_db(upload_path, prepared, src_uri=True)
        staged = Store(scratch)                       # migrate an older layout; set app id/version
        with sqlite3.connect(prepared) as db:
            for table, sql in CLEAR_TRANSIENT.items():
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    db.execute(sql)
        if len(staged.printers()) != expect["printers"] or len(staged.locations()) != expect["locations"]:
            raise BackupError("The backup couldn't be prepared for this version of spooltag.")
        safety = safety_backup(store)
        try:
            _copy_db(prepared, store.path)
            _verify_live(store, expect)
        except Exception as e:
            try:
                _copy_db(safety, store.path, src_uri=True)
            except Exception as again:  # pragma: no cover - both copies failing
                raise BackupError(f"Restore failed and the previous configuration couldn't be put back "
                                  f"automatically; it is saved as backups/{os.path.basename(safety)}.") from again
            raise BackupError("Restore failed; your previous configuration was put back.") from e
        return expect, safety
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
