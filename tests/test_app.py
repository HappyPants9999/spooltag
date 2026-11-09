"""End-to-end tests against fake Spoolman and Moonraker servers (httpx MockTransport)."""
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class FakeBackends:
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
        self.lanes: dict = {}
        self.gcode: list[str] = []
        self.moonraker_up = True

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
                return httpx.Response(200, json=list(self.spools.values()))
        if url.host == "printer":
            if not self.moonraker_up:
                raise httpx.ConnectError("refused", request=request)
            if url.path == "/server/database/item":
                if request.method == "GET":
                    if not self.lanes:
                        return httpx.Response(404, json={"error": {"message": "Namespace not found"}})
                    return httpx.Response(200, json={"result": {"namespace": "lane_data", "value": self.lanes}})
                if request.method == "POST":
                    body = json.loads(request.content)
                    self.lanes[body["key"]] = body["value"]
                    return httpx.Response(200, json={"result": body})
                if request.method == "DELETE":
                    self.lanes.pop(url.params["key"], None)
                    return httpx.Response(200, json={"result": {}})
            if url.path == "/printer/gcode/script":
                self.gcode.append(json.loads(request.content)["script"])
                return httpx.Response(200, json={"result": "ok"})
        return httpx.Response(500, json={"unexpected": str(url)})


def settings(tmp_path, **kw) -> Settings:
    base = dict(spoolman_url="http://spoolman:8000", moonraker_url="http://printer:7125",
                moonraker_api_key="", base_url="http://spools.lan:8090", num_tools=8,
                printer_name="SV08", location_format="{printer} T{tool}", unload_location="Dry rack",
                pending_timeout=120, klipper_save_variables=True, klipper_macro_variable=False,
                data_dir=str(tmp_path))
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def env(tmp_path):
    fake = FakeBackends()
    app = create_app(settings(tmp_path), transport=httpx.MockTransport(fake.handler))
    with TestClient(app) as client:
        yield client, fake


def test_spool_then_tool_assigns(env):
    client, fake = env
    r = client.get("/s/42")
    assert r.status_code == 200 and "Now scan a tool tag" in r.text
    r = client.get("/t/3")
    assert "✓ Assigned" in r.text
    lane = fake.lanes["T3"]
    assert lane["lane"] == "3" and lane["spool_id"] == 42
    assert lane["material"] == "PLA" and lane["color"] == "#1E88E5"
    assert lane["nozzle_temp"] == 210 and lane["filament_id"] == "P42abc"
    assert fake.spools[42]["location"] == "SV08 T3"
    assert fake.gcode == ["SAVE_VARIABLE VARIABLE=t3__spool_id VALUE=42"]


def test_tool_then_spool_assigns(env):
    client, fake = env
    assert "Now scan a spool tag" in client.get("/t/T5").text
    assert "✓ Assigned" in client.get("/s/7").text
    assert fake.lanes["T5"]["spool_id"] == 7
    assert "filament_id" not in fake.lanes["T5"]


def test_replacing_spool_moves_old_one_and_dedupes(env):
    client, fake = env
    client.get("/s/7"); client.get("/t/2")          # 7 in T2
    client.get("/s/42"); client.get("/t/2")         # 42 replaces it
    assert fake.lanes["T2"]["spool_id"] == 42
    assert fake.spools[7]["location"] == "Dry rack"
    client.get("/s/42"); client.get("/t/4")         # move 42 to T4
    assert "T2" not in fake.lanes and fake.lanes["T4"]["spool_id"] == 42


def test_removes_duplicate_key_for_same_lane(env):
    client, fake = env
    fake.lanes["lane3"] = {"lane": "3", "spool_id": 7, "material": "ASA"}  # e.g. written by another tool
    client.get("/s/42"); client.get("/t/3")
    assert "lane3" not in fake.lanes and fake.lanes["T3"]["spool_id"] == 42
    assert fake.spools[7]["location"] == "Dry rack"


def test_printer_offline_is_partial_not_crash(env):
    client, fake = env
    fake.moonraker_up = False
    client.get("/s/42")
    r = client.get("/t/1")
    assert r.status_code == 200 and "Assigned with problems" in r.text and "is the printer on?" in r.text
    assert fake.spools[42]["location"] == "SV08 T1"   # Spoolman still updated


def test_unknown_spool_and_tool(env):
    client, _ = env
    assert client.get("/s/999").status_code == 404
    assert client.get("/t/8").status_code == 404
    assert client.get("/t/x1").status_code == 404


def test_status_tags_and_clear(env):
    client, fake = env
    client.get("/s/42"); client.get("/t/0")
    page = client.get("/").text
    assert "T0" in page and "#42" in page
    tags = client.get("/tags").text
    assert "http://spools.lan:8090/t/7" in tags and "http://spools.lan:8090/s/42" in tags and "<svg" in tags
    r = client.post("/tools/0/clear", follow_redirects=False)
    assert r.status_code == 303 and "T0" not in fake.lanes
    assert fake.spools[42]["location"] == "Dry rack"


def test_label_skips_name_equal_to_material():
    from app.clients import spool_label
    spool = {"id": 1, "filament": {"name": "ABS", "material": "ABS", "vendor": {"name": "Bambu Lab"}}}
    assert spool_label(spool) == "Bambu Lab ABS"


def test_pending_expires(tmp_path):
    fake = FakeBackends()
    app = create_app(settings(tmp_path, pending_timeout=-1), transport=httpx.MockTransport(fake.handler))
    with TestClient(app) as client:
        client.get("/s/42")
        assert "Now scan a spool tag" in client.get("/t/3").text   # expired, so the tool starts a new pairing
        assert "T3" not in fake.lanes
