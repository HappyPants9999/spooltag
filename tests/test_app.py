"""End-to-end tests against fake Spoolman and Moonraker servers (httpx MockTransport)."""
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import LegacyPrinter, Settings
from app.main import create_app
from app.store import Printer, Store


class FakeBackends:
    """Spoolman at host 'spoolman'; Moonraker at hosts 'printer' (SV08) and 'v0'; Bambuddy at 'bambuddy'."""

    def __init__(self):
        self.spools = {
            42: {"id": 42, "location": "Dry rack", "remaining_weight": 812.4,
                 "filament": {"name": "Galaxy Blue", "material": "PLA", "color_hex": "1E88E5",
                              "settings_extruder_temp": 210, "settings_bed_temp": 60,
                              "vendor": {"name": "Overture"}, "extra": {}},
                 "extra": {"orca_filament_id": '"P42abc"'}},
            7: {"id": 7, "location": "Shelf", "remaining_weight": 1000,
                "filament": {"name": "Black", "material": "ASA", "color_hex": "000000",
                             "vendor": {"name": "Polymaker"}}},
        }
        self.lanes: dict[str, dict] = {"printer": {}, "v0": {}}
        self.gcode: dict[str, list[str]] = {"printer": [], "v0": []}
        self.down: set[str] = set()
        self.ams: dict[tuple[int, int, int], int] = {}     # (printer_id, ams_id, tray_id) -> spool id
        self.bambuddy_calls: list[tuple[str, str, dict]] = []
        self.bambuddy_error: tuple[int, str] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "spoolman":
            m = re.fullmatch(r"/api/v1/spool/(\d+)", url.path)
            if m:
                sid = int(m.group(1))
                if sid not in self.spools:
                    return httpx.Response(404, json={"message": "not found"})
                if request.method == "PATCH":
                    self.spools[sid].update(json.loads(request.content))
                return httpx.Response(200, json=self.spools[sid])
            if url.path == "/api/v1/spool":
                spools = list(self.spools.values())
                if "location" in url.params:
                    spools = [s for s in spools if s.get("location") == url.params["location"]]
                return httpx.Response(200, json=spools)
        if url.host in self.lanes:
            if url.host in self.down:
                raise httpx.ConnectError("refused", request=request)
            lanes = self.lanes[url.host]
            if url.path == "/server/database/item":
                if request.method == "GET":
                    if not lanes:
                        return httpx.Response(404, json={"error": {"message": "Namespace not found"}})
                    return httpx.Response(200, json={"result": {"namespace": "lane_data", "value": lanes}})
                if request.method == "POST":
                    body = json.loads(request.content)
                    lanes[body["key"]] = body["value"]
                    return httpx.Response(200, json={"result": body})
                if request.method == "DELETE":
                    lanes.pop(url.params["key"], None)
                    return httpx.Response(200, json={"result": {}})
            if url.path == "/printer/gcode/script":
                self.gcode[url.host].append(json.loads(request.content)["script"])
                return httpx.Response(200, json={"result": "ok"})
        if url.host == "bambuddy":
            return self.bambuddy(request)
        return httpx.Response(500, json={"unexpected": str(url)})

    def bambuddy(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if "bambuddy" in self.down:
            raise httpx.ConnectError("refused", request=request)
        if request.headers.get("x-api-key") != "bb_test":
            return httpx.Response(401, json={"detail": "Invalid API key"})
        base = "/api/v1/spoolman/inventory/slot-assignments"
        body = json.loads(request.content) if request.content else {}
        self.bambuddy_calls.append((request.method, url.path, body))
        if request.method == "GET" and url.path == base + "/all":
            pid = int(url.params["printer_id"])
            return httpx.Response(200, json=[
                {"printer_id": p, "ams_id": a, "tray_id": t, "spoolman_spool_id": s,
                 "printer_name": "P1S", "ams_label": None}
                for (p, a, t), s in self.ams.items() if p == pid])
        if request.method == "POST" and url.path == base:
            if self.bambuddy_error:
                code, detail = self.bambuddy_error
                return httpx.Response(code, json={"detail": detail})
            assert body["move"] is True
            sid = body["spoolman_spool_id"]
            self.ams = {k: v for k, v in self.ams.items() if v != sid}
            self.ams[(body["printer_id"], body["ams_id"], body["tray_id"])] = sid
            return httpx.Response(200, json={"ok": True})
        m = re.fullmatch(base + r"/(\d+)", url.path)
        if request.method == "DELETE" and m:
            sid = int(m.group(1))
            self.ams = {k: v for k, v in self.ams.items() if v != sid}
            return httpx.Response(200, json={"id": sid})
        return httpx.Response(500, json={"unexpected": str(url)})


def settings(tmp_path, **kw) -> Settings:
    base = dict(spoolman_url="http://spoolman:8000", base_url="http://spools.lan:7913",
                location_format="{printer} T{tool}", unload_location="Dry rack", pending_timeout=120,
                data_dir=str(tmp_path),
                legacy_printer=LegacyPrinter("SV08", "http://printer:7125", "", 8, True, False))
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def env(tmp_path):
    fake = FakeBackends()
    app = create_app(settings(tmp_path), transport=httpx.MockTransport(fake.handler))
    with TestClient(app) as client:
        client.post("/settings/printers", data={"name": "Voron V0", "slug": "v0", "num_tools": "1",
                                                "moonraker_url": "http://v0:7125"})
        client.post("/settings/locations", data={"name": "Dry box 1"})
        yield client, fake


# Loading tools -------------------------------------------------------------------------------

def test_legacy_env_becomes_first_printer(env):
    client, _ = env
    page = client.get("/settings").text
    assert "SV08" in page and "/t/sv08/" in page and "Voron V0" in page


def test_spool_then_tool_loads(env):
    client, fake = env
    assert "Now scan a tool or storage tag" in client.get("/s/42").text
    r = client.get("/t/sv08/3")
    assert "✓ Loaded" in r.text
    lane = fake.lanes["printer"]["T3"]
    assert lane["lane"] == "3" and lane["spool_id"] == 42
    assert lane["material"] == "PLA" and lane["color"] == "#1E88E5"
    assert lane["nozzle_temp"] == 210 and lane["filament_id"] == "P42abc"
    assert fake.spools[42]["location"] == "SV08 T3"
    assert fake.gcode["printer"] == ["SAVE_VARIABLE VARIABLE=t3__spool_id VALUE=42"]


def test_tool_then_spool_and_legacy_url(env):
    client, fake = env
    assert "Now scan a spool tag" in client.get("/t/T5").text      # 0.1 tag → first printer
    assert "✓ Loaded" in client.get("/s/7").text
    assert fake.lanes["printer"]["T5"]["spool_id"] == 7
    assert "filament_id" not in fake.lanes["printer"]["T5"]


def test_second_printer(env):
    client, fake = env
    client.get("/s/7"); client.get("/t/v0/0")
    assert fake.lanes["v0"]["T0"]["spool_id"] == 7
    assert fake.spools[7]["location"] == "Voron V0 T0"
    assert fake.gcode["v0"] == []                                   # Klipper vars off for this printer


def test_move_between_printers_removes_old_lane(env):
    client, fake = env
    client.get("/s/42"); client.get("/t/sv08/2")
    client.get("/s/42"); client.get("/t/v0/0")
    assert "T2" not in fake.lanes["printer"] and fake.lanes["v0"]["T0"]["spool_id"] == 42
    assert "SAVE_VARIABLE VARIABLE=t2__spool_id VALUE=None" in fake.gcode["printer"]


def test_replacing_spool_moves_old_one(env):
    client, fake = env
    client.get("/s/7"); client.get("/t/sv08/2")
    client.get("/s/42"); client.get("/t/sv08/2")
    assert fake.lanes["printer"]["T2"]["spool_id"] == 42
    assert fake.spools[7]["location"] == "Dry rack"


def test_removes_duplicate_key_for_same_lane(env):
    client, fake = env
    fake.lanes["printer"]["lane3"] = {"lane": "3", "spool_id": 7, "material": "ASA"}
    client.get("/s/42"); client.get("/t/sv08/3")
    assert "lane3" not in fake.lanes["printer"] and fake.lanes["printer"]["T3"]["spool_id"] == 42


def test_offline_printer_is_partial_and_other_printer_offline_is_fine(env):
    client, fake = env
    fake.down.add("v0")
    client.get("/s/42")
    r = client.get("/t/sv08/1")
    assert "✓ Loaded" in r.text and "Voron V0 not reachable" in r.text
    fake.down = {"printer"}
    client.get("/s/7")
    r = client.get("/t/sv08/4")
    assert "Loaded with problems" in r.text and "is the printer on?" in r.text
    assert fake.spools[7]["location"] == "SV08 T4"


# Storage ----------------------------------------------------------------------------------

def test_store_spool_removes_it_from_printer(env):
    client, fake = env
    client.get("/s/42"); client.get("/t/sv08/6")
    assert "Scan a spool to store it here" in client.get("/l/dry-box-1").text
    r = client.get("/s/42")
    assert "✓ Stored" in r.text
    assert fake.spools[42]["location"] == "Dry box 1"
    assert "T6" not in fake.lanes["printer"]


def test_spool_then_location_and_contents(env):
    client, fake = env
    client.get("/s/7")
    assert "✓ Stored" in client.get("/l/dry-box-1").text
    page = client.get("/locations/dry-box-1").text
    assert "#7 Polymaker ASA Black" in page and "#42" not in page
    assert "1 spool" in client.get("/").text


# Errors, status, tags, settings ------------------------------------------------------------

def test_unknown_things(env):
    client, _ = env
    assert client.get("/s/999").status_code == 404
    assert client.get("/t/sv08/8").status_code == 404
    assert client.get("/t/v0/1").status_code == 404
    assert client.get("/t/nope/0").status_code == 404
    assert client.get("/l/nope").status_code == 404


def test_status_tags_and_clear(env):
    client, fake = env
    client.get("/s/42"); client.get("/t/sv08/0")
    page = client.get("/").text
    assert "SV08" in page and "Voron V0" in page and "#42" in page and "Dry box 1" in page
    tags = client.get("/tags").text
    for url in ("/t/sv08/7", "/t/v0/0", "/l/dry-box-1", "/s/42"):
        assert "http://spools.lan:7913" + url in tags
    r = client.post("/p/sv08/tools/0/clear", follow_redirects=False)
    assert r.status_code == 303 and "T0" not in fake.lanes["printer"]
    assert fake.spools[42]["location"] == "Dry rack"


def test_settings_validation_and_edit(env):
    client, _ = env
    bad = client.post("/settings/printers", data={"name": "X", "slug": "v0", "num_tools": "1",
                                                  "moonraker_url": "http://x:7125"})
    assert bad.status_code == 400 and "already used" in bad.text
    assert client.post("/settings/printers", data={"name": "X", "num_tools": "1",
                                                   "moonraker_url": "x:7125"}).status_code == 400
    assert client.post("/settings/locations", data={"name": "dry box 1"}).status_code == 400
    r = client.post("/settings/printers", data={"name": "Voron V0.2", "slug": "v0", "mode": "edit",
                                                "num_tools": "1", "moonraker_url": "http://v0:7125"})
    assert r.status_code == 200 and "Voron V0.2 saved." in r.text
    client.post("/settings/locations/dry-box-1/delete")
    assert client.get("/l/dry-box-1").status_code == 404


def test_upgrade_from_01_database(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "spooltag.db")
    db.execute("CREATE TABLE pending (id INTEGER PRIMARY KEY, kind TEXT, value INTEGER, ts REAL)")
    db.execute("CREATE TABLE log (ts REAL, spool_id INTEGER, tool INTEGER, action TEXT, detail TEXT)")
    db.execute("INSERT INTO log VALUES (1, 42, 3, 'assign', 'old')")
    db.commit(); db.close()
    store = Store(str(tmp_path))
    assert store.recent()[0][2] == "T3"
    store.set_pending("spool", "42")
    assert store.get_pending(60).ref == "42"


def test_label_skips_name_equal_to_material():
    from app.clients import spool_label
    spool = {"id": 1, "filament": {"name": "ABS", "material": "ABS", "vendor": {"name": "Bambu Lab"}}}
    assert spool_label(spool) == "Bambu Lab ABS"


def test_pending_expires(tmp_path):
    fake = FakeBackends()
    app = create_app(settings(tmp_path, pending_timeout=-1), transport=httpx.MockTransport(fake.handler))
    with TestClient(app) as client:
        client.get("/s/42")
        assert "Now scan a spool tag" in client.get("/t/sv08/3").text
        assert "T3" not in fake.lanes["printer"]


def test_no_printers(tmp_path):
    fake = FakeBackends()
    app = create_app(settings(tmp_path, legacy_printer=None), transport=httpx.MockTransport(fake.handler))
    with TestClient(app) as client:
        assert "Add one in Settings" in client.get("/").text
        assert client.get("/t/3").status_code == 404


# Bambu AMS via Bambuddy ------------------------------------------------------------------------

@pytest.fixture
def ams(env):
    client, fake = env
    r = client.post("/settings/printers", data={"kind": "bambuddy", "name": "P1S", "slug": "p1s",
                                                "num_tools": "4", "moonraker_url": "http://bambuddy:8000/",
                                                "api_key": "bb_test", "remote_id": "3"})
    assert r.status_code == 200 and "P1S added." in r.text
    return client, fake


def test_ams_slot_assigns_through_bambuddy(ams):
    client, fake = ams
    client.get("/s/42"); client.get("/t/sv08/1")                    # start in an SV08 tool
    client.get("/s/42")
    r = client.get("/t/p1s/2")
    assert "✓ Loaded" in r.text and "Bambuddy assigned #42 to P1S AMS 2" in r.text
    assert fake.ams == {(3, 0, 1): 42}
    assert fake.spools[42]["location"] == "P1S AMS 2"
    assert "T1" not in fake.lanes["printer"]                         # one place only
    assert not any(m == "DELETE" for m, _, _ in fake.bambuddy_calls)  # move=True handled the AMS


def test_ams_slot_then_spool_and_replace(ams):
    client, fake = ams
    client.get("/s/7"); client.get("/t/p1s/4")
    assert "Now scan a spool tag" in client.get("/t/p1s/4").text
    assert "✓ Loaded" in client.get("/s/42").text
    assert fake.ams == {(3, 0, 3): 42}
    assert fake.spools[7]["location"] == "Dry rack"                 # pushed out → UNLOAD_LOCATION


def test_ams_spool_moved_to_klipper_or_storage_leaves_ams(ams):
    client, fake = ams
    client.get("/s/42"); client.get("/t/p1s/1")
    r = (client.get("/s/42"), client.get("/t/v0/0"))[1]
    assert "Removed #42 from P1S AMS 1" in r.text and fake.ams == {}
    client.get("/s/7"); client.get("/t/p1s/3")
    client.get("/s/7"); client.get("/l/dry-box-1")
    assert fake.ams == {} and fake.spools[7]["location"] == "Dry box 1"


def test_ams_status_tags_clear_and_bounds(ams):
    client, fake = ams
    client.get("/s/42"); client.get("/t/p1s/1")
    page = client.get("/").text
    assert "AMS via Bambuddy" in page and "AMS 1" in page and "AMS 4" in page and "#42" in page
    tags = client.get("/tags").text
    assert all(f"http://spools.lan:7913/t/p1s/{n}" in tags for n in range(1, 5))
    assert "/t/p1s/0" not in tags
    assert client.get("/t/p1s/0").status_code == 404 and client.get("/t/p1s/5").status_code == 404
    client.post("/p/p1s/tools/1/clear")
    assert fake.ams == {} and fake.spools[42]["location"] == "Dry rack"


def test_ams_errors(ams):
    client, fake = ams
    fake.bambuddy_error = (400, "Spoolman integration is not enabled")
    client.get("/s/42")
    r = client.get("/t/p1s/1")
    assert "Loaded with problems" in r.text and "Spoolman integration is not enabled" in r.text
    fake.bambuddy_error = None
    fake.down.add("bambuddy")
    client.get("/s/7")
    r = client.get("/t/sv08/2")                                     # AMS offline doesn't block Klipper
    assert "✓ Loaded" in r.text and "P1S (Bambuddy) not reachable" in r.text
    assert "Bambuddy unreachable" in client.get("/").text


def test_ams_settings_need_bambuddy_id(env):
    client, _ = env
    r = client.post("/settings/printers", data={"kind": "bambuddy", "name": "P1S", "num_tools": "4",
                                                "moonraker_url": "http://bambuddy:8000"})
    assert r.status_code == 400 and "id in Bambuddy" in r.text


def test_upgrade_adds_printer_kind(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "spooltag.db")
    db.execute("CREATE TABLE printers (slug TEXT PRIMARY KEY, name TEXT NOT NULL, moonraker_url TEXT NOT NULL,"
               " api_key TEXT NOT NULL DEFAULT '', num_tools INTEGER NOT NULL, save_variables INTEGER NOT NULL"
               " DEFAULT 0, macro_variable INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0)")
    db.execute("INSERT INTO printers VALUES ('sv08', 'SV08', 'http://printer:7125', '', 8, 1, 0, 1)")
    db.commit(); db.close()
    p = Store(str(tmp_path)).printer("sv08")
    assert p.kind == "moonraker" and not p.is_ams and list(p.tool_numbers()) == list(range(8))
