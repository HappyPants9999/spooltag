"""Regression tests for the 2026-11-17 security audit. Each block names the finding it covers."""
import http.server
import importlib.metadata
import os
import re
import sqlite3
import stat
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

from app import auth
from app.netguard import GuardedTransport, check_url, classify
from app.store import Store
from tests.test_app import BASE, FakeBackends, TestClient, create_app, settings

ROOT = Path(__file__).resolve().parent.parent

# Headers real browsers send (fetch metadata). A tag tap is a browser-initiated navigation:
# Android Chrome opening the NFC intent's URL, iOS Safari opening the tag notification, a QR
# scan from the camera, a bookmark, a typed URL - all "Sec-Fetch-Site: none".
ANDROID_CHROME_NFC = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
                      "Sec-Fetch-User": "?1",
                      "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 8) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/142.0.0.0 Mobile Safari/537.36"}
IOS_SAFARI_NFC = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
                  "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 "
                                "(KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1"}
OLD_IOS_SAFARI = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_8 like Mac OS X) AppleWebKit/605.1.15 "
                                "(KHTML, like Gecko) Version/15.6 Mobile/15E148 Safari/604.1"}  # no fetch metadata
EVIL_IMG = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"}
EVIL_FETCH = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
              "Origin": "https://evil.example"}
EVIL_NAV = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
SAME_SITE_NAV = {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
EVIL_FORM = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
             "Origin": "https://evil.example"}
SAME_ORIGIN_FORM = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
                    "Origin": BASE}

GENERAL = {"spoolman_url": "http://spoolman:8000", "base_url": BASE, "location_format": "{printer} T{tool}",
           "unload_location": "Dry rack", "pending_timeout": "120"}


def make(tmp_path, **kw):
    fake = FakeBackends()
    app = create_app(settings(tmp_path, **kw), transport=httpx.MockTransport(fake.handler))
    return app, fake


@pytest.fixture
def env(tmp_path):
    app, fake = make(tmp_path)
    with TestClient(app) as client:
        client.post("/settings/locations", data={"name": "Dry box 1"})
        yield client, fake, app


def lane(fake, tool):
    return fake.lanes["printer"].get(f"T{tool}", {}).get("spool_id")


# H1: cross-site requests and navigations can't silently change anything -----------------------

@pytest.mark.parametrize("headers", [ANDROID_CHROME_NFC, IOS_SAFARI_NFC, OLD_IOS_SAFARI],
                         ids=["android-chrome", "ios-safari", "ios-15-no-fetch-metadata"])
def test_nfc_tag_taps_still_work(env, headers):
    client, fake, _ = env
    assert "Now scan a tool" in client.get("/s/42", headers=headers).text
    r = client.get("/t/sv08/3", headers=headers)
    assert "Loaded" in r.text and lane(fake, 3) == 42 and fake.spools[42]["location"] == "SV08 T3"
    assert "Now scan a storage" not in client.get("/l/dry-box-1", headers=headers).text
    assert "Stored" in client.get("/s/42", headers=headers).text and fake.spools[42]["location"] == "Dry box 1"


def test_legacy_tool_tag_tap_works(env):
    client, fake, _ = env
    client.get("/t/2", headers=ANDROID_CHROME_NFC)
    assert "Loaded" in client.get("/s/7", headers=ANDROID_CHROME_NFC).text and lane(fake, 2) == 7


@pytest.mark.parametrize("headers", [EVIL_IMG, EVIL_FETCH], ids=["img", "fetch"])
def test_cross_site_subresources_cannot_scan(env, headers):
    client, fake, _ = env
    for url in ("/s/42", "/t/sv08/5", "/t/5", "/l/dry-box-1"):
        assert client.get(url, headers=headers).status_code == 403
    assert lane(fake, 5) is None and fake.spools[42]["location"] == "Dry rack"
    # and nothing was left pending: a real tap now starts fresh instead of pairing with spool 42
    assert "Now scan a spool" in client.get("/t/sv08/5", headers=ANDROID_CHROME_NFC).text


@pytest.mark.parametrize("headers", [EVIL_NAV, SAME_SITE_NAV, {"Referer": "https://evil.example/x"}],
                         ids=["cross-site", "same-site-other-port", "old-browser-foreign-referer"])
def test_foreign_navigation_asks_first(env, headers):
    client, fake, _ = env
    r = client.get("/s/42", headers=headers)
    assert r.status_code == 200 and "Confirm this scan" in r.text and 'action="/s/42"' in r.text
    r = client.get("/t/sv08/5", headers=headers)
    assert "Confirm this scan" in r.text and "SV08 T5" in r.text
    assert lane(fake, 5) is None and fake.spools[42]["location"] == "Dry rack"
    # Confirming (a same-origin POST from that page) does the scan.
    assert "Now scan a tool" in client.post("/s/42", headers=SAME_ORIGIN_FORM).text
    assert "Loaded" in client.post("/t/sv08/5", headers=SAME_ORIGIN_FORM).text and lane(fake, 5) == 42


def test_cross_site_form_posts_refused(env):
    client, fake, app = env
    store = app.state.service.store
    attempts = [
        ("/settings/general", {**GENERAL, "spoolman_url": "http://x:1"}),
        ("/settings/printers", {"name": "Evil", "num_tools": "1", "moonraker_url": "http://x:1"}),
        ("/settings/printers/sv08/delete", {}),
        ("/settings/locations", {"name": "Evil"}),
        ("/settings/locations/dry-box-1/delete", {}),
        ("/settings/password", {"action": "set", "new": "hunter2hunter2", "confirm": "hunter2hunter2"}),
        ("/p/sv08/tools/1/clear", {}), ("/pending/clear", {}), ("/login", {"password": "x"}),
        ("/s/42", {}), ("/t/sv08/1", {}), ("/l/dry-box-1", {}),
    ]
    variants = [EVIL_FORM, {"Origin": "https://evil.example"}, {"Origin": "null"},
                {"Referer": "https://evil.example/page"}, {"Origin": "http://spools.lan:9999"}]
    for url, data in attempts:
        for headers in variants:
            r = client.post(url, data=data, headers=headers, follow_redirects=False)
            assert r.status_code == 403, (url, headers)
    assert app.state.service.s.spoolman_url == "http://spoolman:8000"
    assert store.printer("sv08") and store.location("dry-box-1") and not store.printer("evil")
    assert not auth.password_set(store) and lane(fake, 1) is None


def test_same_origin_posts_allowed(env):
    client, _, app = env
    r = client.post("/settings/locations", data={"name": "Shelf"}, headers=SAME_ORIGIN_FORM)
    assert "Shelf added." in r.text
    r = client.post("/settings/locations", data={"name": "Shelf 2"}, headers={"Origin": BASE})  # no fetch metadata
    assert "Shelf 2 added." in r.text


# M1: DNS rebinding (Host allowlist) and API key disclosure -------------------------------------

def test_foreign_host_names_refused(env):
    client, _, _ = env
    for url in ("/settings", "/", "/tags", "/s/42"):
        r = client.get(url, headers={"Host": "rebind.evil.example:7913"})
        assert r.status_code == 400 and "rebind.evil.example" in r.text
    assert client.get("/settings", headers={"Host": "10.10.2.176:7913"}).status_code == 200
    assert client.get("/settings", headers={"Host": "[fd00::5]:7913"}).status_code == 200
    assert client.get("/healthz", headers={"Host": "127.0.0.1:8000"}).status_code == 200
    assert client.get("/settings", headers={"Host": "localhost:7913"}).status_code == 200


def test_extra_and_env_host_names(tmp_path):
    app, _ = make(tmp_path, env_allowed_hosts=["rescue.lan"])
    with TestClient(app) as client:
        assert client.get("/", headers={"Host": "rescue.lan"}).status_code == 200
        assert client.get("/", headers={"Host": "spooltag.lan"}).status_code == 400
        client.post("/settings/general", data={**GENERAL, "extra_hosts": "spooltag.lan, Other.LAN:7913"})
        assert client.get("/", headers={"Host": "spooltag.lan"}).status_code == 200
        assert client.get("/", headers={"Host": "other.lan"}).status_code == 200
        assert client.post("/settings/general", data={**GENERAL, "extra_hosts": "bad host!"}).status_code == 400


def test_wildcard_env_disables_host_check(tmp_path):
    app, _ = make(tmp_path, env_allowed_hosts=["*"])
    with TestClient(app) as client:
        assert client.get("/", headers={"Host": "anything.example"}).status_code == 200


def test_saving_cannot_lock_you_out(env):
    client, _, app = env
    r = client.post("/settings/general", data={**GENERAL, "base_url": "http://10.10.2.176:7913"})
    assert r.status_code == 400 and "locked out" in r.text
    assert app.state.service.s.base_url == BASE


def test_api_keys_are_write_only(env):
    client, fake, app = env
    client.post("/settings/printers", data={"kind": "bambuddy", "name": "P1S", "slug": "p1s", "num_tools": "4",
                                            "moonraker_url": "http://bambuddy:8000", "api_key": "bb_test",
                                            "remote_id": "3"})
    page = client.get("/settings").text
    assert "bb_test" not in page and "a key is saved" in page
    edit = {"kind": "bambuddy", "name": "P1S AMS", "slug": "p1s", "mode": "edit", "num_tools": "4",
            "moonraker_url": "http://bambuddy:8000", "api_key": "", "remote_id": "3"}
    client.post("/settings/printers", data=edit)                       # blank keeps the key
    assert app.state.service.store.printer("p1s").api_key == "bb_test"
    client.get("/s/42"); client.get("/t/p1s/1")
    assert fake.ams[(3, 0, 0)] == 42                                   # the kept key still works
    client.post("/settings/printers", data={**edit, "clear_api_key": "on"})
    assert app.state.service.store.printer("p1s").api_key == ""


# M2: stored XSS through names ------------------------------------------------------------------

def test_names_cannot_inject_script(env):
    client, _, _ = env
    evil = "Box');alert(document.domain);('\"><script>alert(1)</script>"
    client.post("/settings/locations", data={"name": evil, "slug": "evil"})
    client.post("/settings/printers", data={"name": evil, "slug": "evilp", "num_tools": "1",
                                            "moonraker_url": "http://x:1"})
    for url in ("/settings", "/", "/tags", "/locations/evil"):
        page = client.get(url).text
        assert "<script>alert" not in page
        assert not re.search(r"<[a-z]+\b[^>]*\son[a-z]+\s*=", page), "event-handler attribute in markup"
        assert re.findall(r"<script[^>]*>", page) == ['<script src="/static/app.js" defer>']
    page = client.get("/settings").text
    assert 'data-confirm="Remove Box&#x27;);alert(document.domain);(&#x27;&quot;&gt;&lt;script&gt;' in page


def test_confirm_dialogs_use_data_attribute_only():
    js = __import__("app.pages", fromlist=["APP_JS"]).APP_JS
    assert "dataset.confirm" in js and "eval(" not in js and "innerHTML" not in js


# M3: scans from different phones don't mix; pairing is atomic -----------------------------------

def test_two_phones_scanning_at_once(env):
    client, fake, app = env
    alice, bob = TestClient(app), TestClient(app)
    alice.get("/s/42", headers=ANDROID_CHROME_NFC)       # Alice scans her spool...
    bob.get("/s/7", headers=IOS_SAFARI_NFC)              # ...Bob scans his at the same moment
    assert "Loaded" in alice.get("/t/sv08/1", headers=ANDROID_CHROME_NFC).text
    assert "Loaded" in bob.get("/t/sv08/2", headers=IOS_SAFARI_NFC).text
    assert lane(fake, 1) == 42 and lane(fake, 2) == 7
    # A third device with no scan pending just starts its own.
    assert "Now scan a spool" in TestClient(app).get("/t/sv08/3", headers=ANDROID_CHROME_NFC).text


def test_device_cookie_flags(env):
    client, _, app = env
    r = TestClient(app).get("/s/42", headers=ANDROID_CHROME_NFC)
    cookie = r.headers["set-cookie"]
    assert "spooltag_device=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")


def test_cancel_only_clears_own_scan(env):
    client, fake, app = env
    alice, bob = TestClient(app), TestClient(app)
    alice.get("/s/42"); bob.get("/s/7")
    bob.post("/pending/clear")
    assert "Loaded" in alice.get("/t/sv08/4").text and lane(fake, 4) == 42


def test_pending_scan_is_taken_exactly_once(tmp_path):
    store = Store(str(tmp_path))
    store.set_pending("phone", "spool", "42")
    results, barrier = [], threading.Barrier(16)

    def grab():
        barrier.wait()
        results.append(store.take_pending("phone", 120, ("tool", "spool")))

    threads = [threading.Thread(target=grab) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1


# M4: dependency versions -----------------------------------------------------------------------

def _ver(name):
    return tuple(int(x) for x in re.findall(r"\d+", importlib.metadata.version(name))[:3])


def test_patched_dependency_versions():
    assert _ver("starlette") >= (1, 3, 1)
    assert _ver("python-multipart") >= (0, 0, 31)
    assert _ver("h11") >= (0, 16, 0)


# L1: optional admin password --------------------------------------------------------------------

def set_password(client, pw="correct horse battery"):
    r = client.post("/settings/password", data={"action": "set", "new": pw, "confirm": pw})
    assert "Admin password saved." in r.text
    return pw


def test_admin_password_protects_settings_not_scans(env):
    client, fake, app = env
    pw = set_password(client)
    assert client.get("/settings").status_code == 200               # this browser just logged in
    other = TestClient(app)
    r = other.get("/settings", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fsettings"
    for url, data in (("/settings/locations", {"name": "X"}), ("/settings/general", GENERAL),
                      ("/settings/printers/sv08/delete", {}), ("/p/sv08/tools/1/clear", {}),
                      ("/tools/1/clear", {}), ("/settings/password", {"action": "remove"})):
        r = other.post(url, data=data, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), url
    # tag scanning needs no login
    other.get("/s/42", headers=ANDROID_CHROME_NFC)
    assert "Loaded" in other.get("/t/sv08/1", headers=ANDROID_CHROME_NFC).text and lane(fake, 1) == 42
    # wrong then right password
    assert other.post("/login", data={"password": "nope"}).status_code == 401
    r = other.post("/login", data={"password": pw, "next": "/settings"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=strict" in cookie.lower()
    assert other.get("/settings").status_code == 200
    other.post("/logout")
    assert other.get("/settings", follow_redirects=False).status_code == 303


def test_password_stored_as_scrypt_hash(env):
    client, _, app = env
    pw = set_password(client)
    stored = app.state.service.store.get_setting(auth.HASH_KEY)
    assert stored.startswith("scrypt$") and pw not in stored
    raw = Path(app.state.service.store.path).read_bytes()
    assert pw.encode() not in raw


def test_login_rate_limited(env):
    client, _, app = env
    pw = set_password(client)
    other = TestClient(app)
    for _ in range(5):
        assert other.post("/login", data={"password": "wrong"}).status_code == 401
    assert other.post("/login", data={"password": pw}).status_code == 429   # even the right one, for now


def test_login_has_no_open_redirect(env):
    client, _, app = env
    pw = set_password(client)
    for target in ("//evil.example", "https://evil.example", "/\\evil.example", "javascript:alert(1)", "\t/x"):
        r = TestClient(app).post("/login", data={"password": pw, "next": target}, follow_redirects=False)
        assert r.headers["location"] == "/settings", target


def test_password_change_and_removal_need_current_and_end_sessions(env):
    client, _, app = env
    pw = set_password(client)
    second = TestClient(app)
    second.post("/login", data={"password": pw})
    r = client.post("/settings/password", data={"action": "set", "current": "wrong", "new": "x" * 10,
                                                "confirm": "x" * 10})
    assert r.status_code == 401
    r = client.post("/settings/password", data={"action": "set", "current": pw, "new": "new password 1",
                                                "confirm": "new password 1"})
    assert "Admin password saved." in r.text
    assert second.get("/settings", follow_redirects=False).status_code == 303   # other sessions ended
    assert client.post("/settings/password", data={"action": "set", "current": "new password 1",
                                                   "new": "short", "confirm": "short"}).status_code == 400
    r = client.post("/settings/password", data={"action": "remove", "current": "new password 1"})
    assert "Admin password removed." in r.text and TestClient(app).get("/settings").status_code == 200


def test_admin_password_env_seed_and_cli_reset(tmp_path):
    app, _ = make(tmp_path, admin_password_seed="from the environment")
    with TestClient(app) as client:
        assert client.get("/settings", follow_redirects=False).status_code == 303
        assert client.post("/login", data={"password": "from the environment"}).status_code == 200
    env = {**os.environ, "DATA_DIR": str(tmp_path)}
    out = subprocess.run([sys.executable, "-m", "app.admin", "reset-password"], cwd=ROOT, env=env,
                         capture_output=True, text=True, check=True)
    assert "removed" in out.stdout and not auth.password_set(Store(str(tmp_path)))


# L2: SSRF guard --------------------------------------------------------------------------------

def test_service_urls_validated_on_save(env):
    client, _, _ = env
    for url in ("http://169.254.169.254/latest", "http://metadata.evil", "http://[fe80::1]:80",
                "http://0.0.0.0:7912", "http://public.example", "http://8.8.8.8",
                "file:///etc/passwd", "gopher://x", "http://user:pw@spoolman:8000"):
        assert client.post("/settings/general", data={**GENERAL, "spoolman_url": url}).status_code == 400, url
        r = client.post("/settings/printers", data={"name": "P", "num_tools": "1", "moonraker_url": url})
        assert r.status_code == 400, url
    for url in ("http://spoolman:8000", "http://10.10.2.176:7912", "http://192.168.1.5", "http://127.0.0.1:7912",
                "http://[fd00::5]:7912", "http://not-running-yet:7912"):
        assert client.post("/settings/general", data={**GENERAL, "spoolman_url": url}).status_code == 200, url
    ok = client.post("/settings/general", data={**GENERAL, "spoolman_url": "http://public.example",
                                                "allow_public_urls": "on"})
    assert ok.status_code == 200


def test_rebinding_blocked_at_request_time(tmp_path):
    """A name that was private at save time but now points at a metadata address is refused,
    and the request never leaves the guard."""
    fake = FakeBackends()
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return fake.handler(request)

    dns = {"spoolman": "10.0.0.10", "printer": "10.0.0.20"}

    async def resolver(host, port):
        return [dns[host]]

    from app.main import create_app as real_create
    app = real_create(settings(tmp_path), transport=httpx.MockTransport(handler), resolver=resolver)
    with TestClient(app) as client:
        dns["printer"] = "169.254.169.254"
        r = client.get("/")
        assert "blocked" in r.text and "169.254.169.254" not in seen
        dns["printer"] = "10.0.0.20"
        assert "unreachable" not in client.get("/").text


@pytest.mark.parametrize("address,kind", [("10.1.2.3", "local"), ("172.20.0.5", "local"), ("192.168.0.9", "local"),
                                          ("100.101.1.1", "local"), ("127.0.0.1", "local"), ("fd12::3", "local"),
                                          ("8.8.8.8", "public"), ("169.254.169.254", "blocked"),
                                          ("100.100.100.200", "blocked"), ("fd00:ec2::254", "blocked"),
                                          ("::ffff:169.254.169.254", "blocked"), ("0.0.0.0", "blocked"),
                                          ("fe80::1", "blocked"), ("224.0.0.251", "blocked"),
                                          ("192.0.2.10", "blocked")])
def test_address_classes(address, kind):
    assert classify(address) == kind


def test_connection_pinned_to_checked_address():
    """Real sockets: the guard connects to the address it checked, keeping the Host name."""
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            got["host"] = self.headers["Host"]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    async def resolver(host, p):
        return {"svc.test": ["127.0.0.1"], "meta.test": ["169.254.169.254"]}[host]

    async def run():
        guard = GuardedTransport(httpx.AsyncHTTPTransport(), lambda: False, resolver)
        async with httpx.AsyncClient(transport=guard) as c:
            r = await c.get(f"http://svc.test:{port}/")
            with pytest.raises(httpx.ConnectError, match="blocked"):
                await c.get(f"http://meta.test:{port}/")
        return r

    import asyncio
    r = asyncio.run(run())
    server.shutdown()
    assert r.text == "ok" and got["host"] == f"svc.test:{port}"


def test_proxy_env_vars_cannot_route_around_guard(env):
    _, _, app = env
    assert app.state.service.client.trust_env is False


def test_check_url_accepts_docker_names():
    import asyncio

    async def resolver(host, port):
        return {"spoolman": ["172.18.0.4"], "bambuddy.lan": ["192.168.1.20"]}[host]
    assert asyncio.run(check_url("http://spoolman:8000", False, resolver)) is None
    assert asyncio.run(check_url("http://bambuddy.lan:8000", False, resolver)) is None


# L3: odd digits don't crash --------------------------------------------------------------------

def test_unicode_digits_rejected_cleanly(env):
    client, _, _ = env
    for url in ("/t/sv08/%C2%B2", "/t/%C2%B2", "/t/sv08/%D9%A3", "/s/%C2%B2", "/s/-1", "/s/0",
                "/s/99999999999999999999"):
        assert client.get(url).status_code in (404, 422), url
    r = client.post("/settings/printers", data={"kind": "bambuddy", "name": "Q", "num_tools": "4",
                                                "moonraker_url": "http://b:1", "remote_id": "²"})
    assert r.status_code == 400
    assert client.post("/settings/general", data={**GENERAL, "pending_timeout": "²"}).status_code == 400
    assert client.post("/settings/general", data={**GENERAL, "pending_timeout": "٣٠"}).status_code == 400


# L4: security headers -------------------------------------------------------------------------

def test_security_headers(env):
    client, _, _ = env
    for url in ("/", "/settings", "/tags", "/s/42", "/static/app.js", "/nope"):
        h = client.get(url).headers
        assert "frame-ancestors 'none'" in h["content-security-policy"], url
        assert "script-src 'self'" in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "same-origin"
    assert client.get("/settings").headers["cache-control"] == "no-store"
    assert client.get("/static/app.js").headers["content-type"].startswith("text/javascript")


# L5: container entrypoint (needs root to change owners; CI's smoke job covers the real image) ----

@pytest.mark.skipif(os.geteuid() != 0, reason="needs root")
def test_entrypoint_migrates_root_owned_data():
    import shutil
    import tempfile
    parent = Path(tempfile.mkdtemp(dir="/tmp"))      # like /data: reachable by the service user
    os.chmod(parent, 0o755)
    data = parent / "data"
    data.mkdir()
    db = sqlite3.connect(data / "spooltag.db")
    db.execute("CREATE TABLE t (x)")
    db.commit()
    db.close()
    os.chmod(data / "spooltag.db", 0o644)
    os.symlink("/etc/hostname", data / "link")
    probe = ("import os; open(os.path.join(os.environ['DATA_DIR'], 'w'), 'w').write('x');"
             "print(os.getuid(), os.getgid(), os.getgroups())")
    env = {**os.environ, "DATA_DIR": str(data), "PUID": "10001", "PGID": "10001"}
    out = subprocess.run([sys.executable, "-m", "app.entrypoint", sys.executable, "-c", probe], cwd=ROOT,
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split()[:2] == ["10001", "10001"]
    st = os.stat(data / "spooltag.db")
    assert (st.st_uid, stat.S_IMODE(st.st_mode)) == (10001, 0o600)
    assert stat.S_IMODE(os.stat(data).st_mode) == 0o700
    assert os.lstat(data / "link").st_uid == 0 and os.stat("/etc/hostname").st_uid == 0   # symlink not followed
    assert stat.S_IMODE(os.stat(data / "w").st_mode) == 0o600                            # umask 077
    shutil.rmtree(parent)


def test_entrypoint_explains_unwritable_data(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    os.chmod(data, 0o500)
    env = {**os.environ, "DATA_DIR": str(data), "PUID": "0"}
    if os.geteuid() == 0:
        pytest.skip("root can write anyway")
    out = subprocess.run([sys.executable, "-m", "app.entrypoint", "true"], cwd=ROOT, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 78 and "chown" in out.stderr


# L6: CI workflow ------------------------------------------------------------------------------

def test_workflow_hardened():
    wf = (ROOT / ".github/workflows/docker.yml").read_text()
    uses = re.findall(r"uses:\s*(\S+)", wf)
    assert uses and all(re.search(r"@[0-9a-f]{40}$", u) for u in uses), uses
    assert re.search(r"^permissions:\n  contents: read\n", wf, re.M)
    assert wf.count("persist-credentials: false") == wf.count("actions/checkout@")
    publish = wf.split("\n  publish:", 1)[1]
    assert "packages: write" in publish and "packages: write" not in wf.split("\n  publish:", 1)[0]
    assert "if: github.event_name != 'pull_request'" in publish
    assert "pull_request_target" not in wf
    assert (ROOT / ".github/dependabot.yml").exists()


def test_requirements_pinned():
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", line), line


# L7: database file permissions ----------------------------------------------------------------

def test_database_private(tmp_path):
    old = os.umask(0o022)
    try:
        store = Store(str(tmp_path / "data"))
        store.save_settings({"k": "v"})
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "data").st_mode) == 0o700


def test_existing_database_made_private(tmp_path):
    Store(str(tmp_path))
    os.chmod(tmp_path / "spooltag.db", 0o644)
    Store(str(tmp_path))
    assert stat.S_IMODE(os.stat(tmp_path / "spooltag.db").st_mode) == 0o600
