"""Backup & restore on the Settings page."""
import logging
import os
import re
import sqlite3
import stat
import threading
import time

import httpx
import pytest

from app import auth, backup
from app.store import APP_ID, Store
from tests.test_app import BASE, FakeBackends, TestClient, create_app, settings
from tests.test_security import ANDROID_CHROME_NFC, EVIL_FORM

PASSWORD = "correct horse battery"


def make(tmp_path, **kw):
    fake = FakeBackends()
    app = create_app(settings(tmp_path, **kw), transport=httpx.MockTransport(fake.handler))
    return app, fake


def configure(client):
    """Printers with tag ids and API keys, a location, and changed general settings."""
    client.post("/settings/printers", data={"name": "Voron V0", "slug": "v0", "num_tools": "1",
                                            "moonraker_url": "http://v0:7125", "api_key": "mk_v0_secret"})
    client.post("/settings/printers", data={"kind": "bambuddy", "name": "P1S", "slug": "p1s", "num_tools": "4",
                                            "moonraker_url": "http://bambuddy:8000", "api_key": "bb_secret_key",
                                            "remote_id": "3"})
    client.post("/settings/locations", data={"name": "Dry box 1"})
    r = client.post("/settings/general", data={"spoolman_url": "http://spoolman:8000", "base_url": BASE,
                                               "location_format": "{printer} slot {tool}",
                                               "unload_location": "Shelf", "pending_timeout": "300",
                                               "timezone": "America/Denver", "extra_hosts": "spooltag.lan"})
    assert r.status_code == 200


def download(client) -> bytes:
    r = client.post("/settings/backup")
    assert r.status_code == 200, r.text[:300]
    return r.content


def upload(client, data: bytes, confirm: bool = True, **kw):
    form = {"confirm_replace": "on"} if confirm else {}
    return client.post("/settings/restore", files={"backup": ("backup.db", data, "application/vnd.sqlite3")},
                       data=form, follow_redirects=False, **kw)


def state(store: Store):
    return ([p.__dict__ for p in store.printers()], [l.__dict__ for l in store.locations()],
            {k: v for k, v in store.settings().items()})


def open_bytes(tmp_path, data: bytes) -> sqlite3.Connection:
    path = tmp_path / f"probe-{time.monotonic_ns()}.db"
    path.write_bytes(data)
    return sqlite3.connect(path)


@pytest.fixture
def env(tmp_path):
    data = tmp_path / "data"
    app, fake = make(data)
    with TestClient(app) as client:
        configure(client)
        yield client, fake, app, data


def store_of(app) -> Store:
    return app.state.service.store


# Backup --------------------------------------------------------------------------------------

def test_backup_download(env, tmp_path):
    client, _, app, _ = env
    r = client.post("/settings/password", data={"action": "set", "new": PASSWORD, "confirm": PASSWORD})
    assert "Admin password saved." in r.text
    TestClient(app).get("/s/42")                                   # a pending scan on another phone
    r = client.post("/settings/backup")
    assert re.fullmatch(r'attachment; filename="spooltag-backup-\d{4}-\d{2}-\d{2}-\d{4}\.db"',
                        r.headers["content-disposition"])
    assert r.headers["cache-control"] == "no-store" and r.headers["content-type"] == "application/vnd.sqlite3"
    assert r.content.startswith(b"SQLite format 3\x00")
    db = open_bytes(tmp_path, r.content)
    assert db.execute("PRAGMA application_id").fetchone()[0] == APP_ID
    keys = dict(db.execute("SELECT slug, api_key FROM printers").fetchall())
    assert keys["v0"] == "mk_v0_secret" and keys["p1s"] == "bb_secret_key" and "sv08" in keys
    assert db.execute("SELECT slug FROM locations").fetchall() == [("dry-box-1",)]
    s = dict(db.execute("SELECT key, value FROM settings").fetchall())
    assert s["base_url"] == BASE and s["location_format"] == "{printer} slot {tool}"
    assert s[auth.HASH_KEY].startswith("scrypt$")                 # the admin credentials
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM scan_pending").fetchone()[0] == 0
    assert store_of(app).session_valid(auth.token_hash(client.cookies["spooltag_session"]))   # live untouched


def test_backup_filename_uses_time_zone():
    from datetime import datetime, timezone
    when = datetime(2026, 11, 18, 0, 30, tzinfo=timezone.utc).timestamp()   # 17:30 on the 17th in Denver
    assert backup.filename("America/Denver", when) == "spooltag-backup-2026-11-17-1730.db"
    assert backup.filename("UTC", when) == "spooltag-backup-2026-11-18-0030.db"


def test_backup_only_by_post(env):
    client, _, _, _ = env
    assert client.get("/settings/backup").status_code == 405


def test_backup_consistent_while_app_writes(env, tmp_path):
    client, _, app, _ = env
    store = store_of(app)
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            store.log("Loaded", spool_id=n, target=f"T{n % 8}")
            store.set_pending(f"phone-{n % 3}", "spool", str(n))
            n += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(15):
            db = open_bytes(tmp_path, download(client))
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT COUNT(*) FROM printers").fetchone()[0] == 3
    finally:
        stop.set()
        t.join()


# Restore -------------------------------------------------------------------------------------

def test_restore_round_trip(env):
    client, fake, app, data = env
    saved = download(client)
    before = state(store_of(app))
    client.post("/settings/printers/v0/delete")
    client.post("/settings/locations/dry-box-1/delete")
    client.post("/settings/general", data={"spoolman_url": "http://10.0.0.99:7912", "base_url": "",
                                           "location_format": "{printer} T{tool}", "pending_timeout": "120",
                                           "extra_hosts": "spools.lan"})
    assert app.state.service.s.base_url == ""
    r = upload(client, saved)
    assert r.status_code == 303 and r.headers["location"].startswith("/settings?saved=Backup+restored")
    assert state(store_of(app)) == before                          # ids, keys and settings all back
    s = app.state.service.s
    assert s.base_url == BASE and s.timezone == "America/Denver" and s.pending_timeout == 300
    assert app.state.service.spoolman.base == "http://spoolman:8000"       # reloaded, no restart
    page = client.get(r.headers["location"]).text
    assert "2 printer" not in page and "3 printer(s), 1 location(s)" in page
    # existing tags keep working
    client.get("/t/v0/0", headers=ANDROID_CHROME_NFC)
    assert "Loaded" in client.get("/s/42", headers=ANDROID_CHROME_NFC).text
    assert fake.spools[42]["location"] == "Voron V0 slot 0"
    client.get("/l/dry-box-1", headers=ANDROID_CHROME_NFC)
    assert "Stored" in client.get("/s/42", headers=ANDROID_CHROME_NFC).text
    # permissions kept; one safety copy; no scratch files left behind
    assert stat.S_IMODE(os.stat(data).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(data / "spooltag.db").st_mode) == 0o600
    safety = list((data / "backups").iterdir())
    assert len(safety) == 1 and stat.S_IMODE(os.stat(data / "backups").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(safety[0]).st_mode) == 0o600
    assert sorted(p.name for p in data.iterdir()) == ["backups", "spooltag.db"]
    # the safety copy really is the configuration from just before the restore
    assert sqlite3.connect(safety[0]).execute("SELECT value FROM settings WHERE key='spoolman_url'"
                                              ).fetchone()[0] == "http://10.0.0.99:7912"


def test_restore_logs_everyone_out_and_cancels_scans(env):
    client, fake, app, _ = env
    client.post("/settings/password", data={"action": "set", "new": PASSWORD, "confirm": PASSWORD})
    saved = download(client)
    other = TestClient(app)
    other.post("/login", data={"password": PASSWORD})
    assert other.get("/settings").status_code == 200
    phone = TestClient(app)
    phone.get("/s/42", headers=ANDROID_CHROME_NFC)               # pending on a phone
    r = upload(client, saved)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=%2Fsettings%3Fsaved")
    assert 'spooltag_session=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
    assert other.get("/settings", follow_redirects=False).status_code == 303
    assert "Now scan a spool" in phone.get("/t/sv08/1", headers=ANDROID_CHROME_NFC).text   # 42 was dropped
    assert fake.lanes["printer"].get("T1") is None
    r = client.post("/login", data={"password": PASSWORD, "next": r.headers["location"].split("next=")[1]})
    assert r.status_code == 200


def test_restore_replaces_admin_password(env):
    client, _, app, _ = env
    client.post("/settings/password", data={"action": "set", "new": PASSWORD, "confirm": PASSWORD})
    saved = download(client)
    client.post("/settings/password", data={"action": "set", "current": PASSWORD, "new": "another one 2",
                                            "confirm": "another one 2"})
    upload(client, saved)
    fresh = TestClient(app)
    assert fresh.post("/login", data={"password": "another one 2"}).status_code == 401
    assert fresh.post("/login", data={"password": PASSWORD}).status_code == 200


def test_restore_into_fresh_installation(env, tmp_path):
    client, _, _, _ = env
    client.post("/settings/password", data={"action": "set", "new": PASSWORD, "confirm": PASSWORD})
    saved = download(client)
    app2, fake2 = make(tmp_path / "new-server", legacy_printer=None, spoolman_url="http://spoolman:8000",
                       base_url="")
    with TestClient(app2, base_url="http://10.10.2.177:7913") as new:   # a new server, opened by IP
        assert "No printers" in new.get("/").text or not store_of(app2).printers()
        r = upload(new, saved)                                       # fresh install: no password yet
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert new.get("/settings", follow_redirects=False).status_code == 303   # backup's password now
        new.post("/login", data={"password": PASSWORD})
        assert [p.slug for p in store_of(app2).printers()] == ["sv08", "v0", "p1s"]
        assert store_of(app2).printer("p1s").api_key == "bb_secret_key"
        assert app2.state.service.s.base_url == BASE
        assert "http://spools.lan:7913/t/v0/0" in new.get("/tags").text
        new.get("/s/42", headers=ANDROID_CHROME_NFC)
        assert "Loaded" in new.get("/t/sv08/2", headers=ANDROID_CHROME_NFC).text
        assert fake2.lanes["printer"]["T2"]["spool_id"] == 42


def test_restore_older_layout(env, tmp_path):
    """A spooltag.db copied from 0.3 (no settings table, no printer kind column) still restores."""
    client, _, app, _ = env
    old = tmp_path / "old.db"
    db = sqlite3.connect(old)
    db.executescript("""
        CREATE TABLE pending (id INTEGER PRIMARY KEY CHECK (id = 1), kind TEXT, ref TEXT, ts REAL);
        CREATE TABLE log (ts REAL NOT NULL, spool_id INTEGER, tool INTEGER, action TEXT NOT NULL, detail TEXT);
        CREATE TABLE printers (slug TEXT PRIMARY KEY, name TEXT NOT NULL, moonraker_url TEXT NOT NULL,
            api_key TEXT NOT NULL DEFAULT '', num_tools INTEGER NOT NULL, save_variables INTEGER NOT NULL DEFAULT 0,
            macro_variable INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE locations (slug TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, position INTEGER NOT NULL DEFAULT 0);
        INSERT INTO printers (slug, name, moonraker_url, num_tools) VALUES ('sv08', 'SV08', 'http://printer:7125', 8);
        INSERT INTO locations (slug, name) VALUES ('shelf', 'Shelf');
        INSERT INTO pending VALUES (1, 'spool', '42', 1);
    """)
    db.commit()
    db.close()
    r = upload(client, old.read_bytes())
    assert r.status_code == 303, r.text[:300]
    store = store_of(app)
    assert [p.slug for p in store.printers()] == ["sv08"] and store.printers()[0].kind == "moonraker"
    assert [l.slug for l in store.locations()] == ["shelf"]
    assert app.state.service.s.base_url == BASE                    # missing settings keep current values
    assert store.settings()["base_url"] == BASE


# Invalid files -------------------------------------------------------------------------------

def _db_bytes(tmp_path, script: str, user_version: int = 0, app_id: int = 0) -> bytes:
    path = tmp_path / f"x-{time.monotonic_ns()}.db"
    db = sqlite3.connect(path)
    db.executescript(script)
    db.execute(f"PRAGMA user_version = {user_version}")
    db.execute(f"PRAGMA application_id = {app_id}")
    db.commit()
    db.close()
    return path.read_bytes()


SPOOLTAG_TABLES = """
    CREATE TABLE printers (slug TEXT PRIMARY KEY, name TEXT NOT NULL, moonraker_url TEXT NOT NULL,
        api_key TEXT NOT NULL DEFAULT '', num_tools INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'moonraker');
    CREATE TABLE locations (slug TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def invalid_files(tmp_path, good: bytes) -> dict[str, bytes]:
    probe = open_bytes(tmp_path, good)
    page_size = probe.execute("PRAGMA page_size").fetchone()[0]
    root = probe.execute("SELECT rootpage FROM sqlite_master WHERE name = 'printers'").fetchone()[0]
    probe.close()
    corrupt = bytearray(good)
    start = (root - 1) * page_size
    corrupt[start:start + 64] = b"\xa5" * 64                       # scramble the printers table's page
    return {
        "empty": b"",
        "text": b"not a database at all\n" * 50,
        "truncated": good[:len(good) // 2],
        "corrupt page": bytes(corrupt),
        "other app's sqlite": _db_bytes(tmp_path, "CREATE TABLE notes (body TEXT);"),
        "foreign application id": _db_bytes(tmp_path, SPOOLTAG_TABLES, app_id=0x12345678),
        "newer spooltag": _db_bytes(tmp_path, SPOOLTAG_TABLES, user_version=99, app_id=APP_ID),
        "trigger": _db_bytes(tmp_path, SPOOLTAG_TABLES + "CREATE TRIGGER t AFTER INSERT ON settings "
                                                         "BEGIN DELETE FROM printers; END;"),
        "view": _db_bytes(tmp_path, SPOOLTAG_TABLES + "CREATE VIEW v AS SELECT * FROM printers;"),
        "extra table": _db_bytes(tmp_path, SPOOLTAG_TABLES + "CREATE TABLE extra (x);"),
        "extra index": _db_bytes(tmp_path, SPOOLTAG_TABLES + "CREATE INDEX i ON printers(name);"),
        "missing columns": _db_bytes(tmp_path, "CREATE TABLE printers (slug TEXT); CREATE TABLE locations (slug);"),
        "bad printer id": _db_bytes(tmp_path, SPOOLTAG_TABLES + "INSERT INTO printers (slug, name, moonraker_url, "
                                                                "num_tools) VALUES ('../etc', 'X', 'http://x', 1);"),
        "bad tool count": _db_bytes(tmp_path, SPOOLTAG_TABLES + "INSERT INTO printers (slug, name, moonraker_url, "
                                                                "num_tools) VALUES ('x', 'X', 'http://x', 0);"),
        "bad kind": _db_bytes(tmp_path, SPOOLTAG_TABLES + "INSERT INTO printers (slug, name, moonraker_url, "
                                                          "num_tools, kind) VALUES ('x', 'X', 'http://x', 1, 'evil');"),
        "bad location id": _db_bytes(tmp_path, SPOOLTAG_TABLES + "INSERT INTO locations VALUES ('A B', 'x');"),
        "non-text setting": _db_bytes(tmp_path, SPOOLTAG_TABLES + "INSERT INTO settings VALUES ('k', x'00ff');"),
    }


def test_invalid_files_rejected_before_any_change(env, tmp_path):
    client, _, app, data = env
    good = download(client)
    before = state(store_of(app))
    for name, blob in invalid_files(tmp_path, good).items():
        r = upload(client, blob)
        assert r.status_code == 400, name
        assert 'class="card bad"' in r.text, name
        assert state(store_of(app)) == before, name
    assert not (data / "backups").exists()                         # nothing was even safety-copied
    assert sorted(p.name for p in data.iterdir()) == ["spooltag.db"]


def test_upload_size_limited(env):
    client, _, app, data = env
    before = state(store_of(app))
    r = upload(client, b"SQLite format 3\x00" + b"\0" * (backup.MAX_BACKUP_BYTES + 128 * 1024))
    assert r.status_code == 413                                    # refused from Content-Length
    r = upload(client, b"SQLite format 3\x00" + b"\0" * (backup.MAX_BACKUP_BYTES - 15))
    assert r.status_code == 413                                    # refused while streaming
    assert state(store_of(app)) == before and sorted(p.name for p in data.iterdir()) == ["spooltag.db"]


def test_restore_needs_file_and_confirmation(env):
    client, _, app, _ = env
    good = download(client)
    before = state(store_of(app))
    assert upload(client, good, confirm=False).status_code == 400
    r = client.post("/settings/restore", data={"confirm_replace": "on"}, files={"other": ("x", b"x")})
    assert r.status_code == 400 and "Choose a backup file" in r.text
    assert state(store_of(app)) == before


# Failure during restore ----------------------------------------------------------------------

def test_failed_swap_leaves_configuration(env, monkeypatch):
    client, _, app, data = env
    good = download(client)
    client.post("/settings/locations", data={"name": "Added after backup"})
    before = state(store_of(app))
    real = backup._copy_db

    def flaky(src, dst, src_uri=False):
        if dst == store_of(app).path and not src_uri:              # the swap into the live database
            raise sqlite3.OperationalError("disk I/O error")
        return real(src, dst, src_uri)

    monkeypatch.setattr(backup, "_copy_db", flaky)
    r = upload(client, good)
    assert r.status_code == 400 and "previous configuration was put back" in r.text
    assert state(store_of(app)) == before
    assert len(list((data / "backups").iterdir())) == 1            # the safety copy was made first


def test_failed_verification_rolls_back(env, monkeypatch):
    client, _, app, _ = env
    good = download(client)
    client.post("/settings/printers/v0/delete")
    before = state(store_of(app))

    def broken(store, expect):
        raise RuntimeError("simulated failure after the swap")

    monkeypatch.setattr(backup, "_verify_live", broken)
    r = upload(client, good)
    assert r.status_code == 400 and "put back" in r.text
    assert state(store_of(app)) == before                          # rolled back from the safety copy
    assert app.state.service.store.printer("v0") is None


def test_safety_copies_pruned(env):
    client, _, _, data = env
    good = download(client)
    for _ in range(backup.KEEP_SAFETY + 2):
        assert upload(client, good).status_code == 303
    assert len(list((data / "backups").iterdir())) == backup.KEEP_SAFETY


# Security ------------------------------------------------------------------------------------

def test_backup_and_restore_need_admin(env):
    client, _, app, _ = env
    good = download(client)
    client.post("/settings/password", data={"action": "set", "new": PASSWORD, "confirm": PASSWORD})
    before = state(store_of(app))
    stranger = TestClient(app)
    r = stranger.post("/settings/backup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login") and b"SQLite" not in r.content
    r = upload(stranger, good)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert state(store_of(app)) == before


def test_backup_and_restore_refuse_cross_site(env):
    client, _, app, _ = env
    good = download(client)
    before = state(store_of(app))
    r = client.post("/settings/backup", headers=EVIL_FORM)
    assert r.status_code == 403 and b"SQLite" not in r.content
    assert upload(client, good, headers=EVIL_FORM).status_code == 403
    assert upload(client, good, headers={"Origin": "https://evil.example"}).status_code == 403
    assert state(store_of(app)) == before


def test_no_route_serves_database_files(env):
    client, _, _, _ = env
    upload(client, download(client))                               # make sure a safety copy exists
    for url in ("/spooltag.db", "/data/spooltag.db", "/backups/", "/static/spooltag.db", "/static/../spooltag.db",
                "/static/%2e%2e/spooltag.db", "/settings/backups", "/settings/backup/latest"):
        r = client.get(url)
        assert r.status_code in (404, 405) and not r.content.startswith(b"SQLite"), url


def test_nothing_sensitive_logged(env, caplog, capfd):
    client, _, _, _ = env
    client.post("/settings/password", data={"action": "set", "new": PASSWORD, "confirm": PASSWORD})
    with caplog.at_level(logging.DEBUG):
        good = download(client)
        upload(client, good)
        upload(client, b"SQLite format 3\x00garbage")
    out, err = capfd.readouterr()
    text = caplog.text + out + err
    for secret in ("mk_v0_secret", "bb_secret_key", "scrypt$", PASSWORD):
        assert secret not in text


def test_settings_page_section(env):
    client, _, _, _ = env
    page = client.get("/settings").text
    assert page.index("Backup &amp; restore") > page.index("Admin password")
    assert "contains your API keys" in page and 'action="/settings/backup"' in page
    assert 'enctype="multipart/form-data"' in page and 'type="file" name="backup" required' in page
    assert 'name="confirm_replace" required' in page and 'data-confirm="Replace ALL spooltag configuration' in page
