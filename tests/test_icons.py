"""Favicon and web-app icons: bundled, served from the root without login, linked from every page."""
import json
import re
import struct
from pathlib import Path

import httpx
import pytest

from app.main import ICON_FILES, STATIC_DIR
from app.pages import ICON_LINKS
from tests.test_app import FakeBackends, TestClient, create_app, settings
from tests.test_security import ANDROID_CHROME_NFC

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "correct horse battery"


@pytest.fixture
def env(tmp_path):
    fake = FakeBackends()
    app = create_app(settings(tmp_path), transport=httpx.MockTransport(fake.handler))
    with TestClient(app) as client:
        client.post("/settings/locations", data={"name": "Dry box 1"})
        yield client, app


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_all_icon_files_bundled():
    for name in ICON_FILES:
        path = Path(STATIC_DIR) / name
        assert path.is_file() and path.stat().st_size > 0, name
    # the image build copies app/ (with app/static/) and nothing excludes it
    assert "COPY app ./app" in (ROOT / "Dockerfile").read_text()
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert not any(p in ignored for p in ("app/static", "static", "*.png", "*.svg", "*.ico", "*.webmanifest"))


@pytest.mark.parametrize("name", list(ICON_FILES))
def test_icons_served_without_login(env, name):
    client, app = env
    client.post("/settings/password", data={"action": "set", "new": PASSWORD, "confirm": PASSWORD})
    stranger = TestClient(app)
    assert stranger.get("/settings", follow_redirects=False).status_code == 303     # settings still locked
    r = stranger.get(f"/{name}")
    assert r.status_code == 200
    assert r.headers["content-type"].split(";")[0] == ICON_FILES[name]
    assert r.content == (Path(STATIC_DIR) / name).read_bytes()
    assert r.headers["cache-control"] == "public, max-age=86400"
    # security headers still on every response
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in r.headers["content-security-policy"]


def test_content_types():
    assert ICON_FILES["favicon.svg"] == "image/svg+xml"
    assert ICON_FILES["favicon.ico"] in ("image/x-icon", "image/vnd.microsoft.icon")
    assert ICON_FILES["site.webmanifest"] == "application/manifest+json"
    assert all(ICON_FILES[n] == "image/png" for n in ICON_FILES if n.endswith(".png"))


def test_image_files_are_what_they_claim():
    static = Path(STATIC_DIR)
    assert png_size((static / "favicon-96x96.png").read_bytes()) == (96, 96)
    assert png_size((static / "apple-touch-icon.png").read_bytes()) == (180, 180)
    assert png_size((static / "web-app-manifest-192x192.png").read_bytes()) == (192, 192)
    assert png_size((static / "web-app-manifest-512x512.png").read_bytes()) == (512, 512)
    ico = (static / "favicon.ico").read_bytes()
    assert ico[:4] == b"\x00\x00\x01\x00" and struct.unpack("<H", ico[4:6])[0] >= 1


def test_svg_has_no_script_or_external_references():
    svg = (Path(STATIC_DIR) / "favicon.svg").read_text(encoding="utf-8")
    assert "<svg" in svg
    assert not re.search(r"<script|<foreignObject|\son[a-z]+\s*=|javascript:", svg, re.I)
    for ref in re.findall(r'(?:href|src)\s*=\s*["\']([^"\']*)', svg):
        assert ref.startswith(("#", "data:")), ref
    assert not re.search(r"url\(\s*['\"]?(?:https?:)?//", svg, re.I)


def test_manifest(env):
    client, _ = env
    m = json.loads((Path(STATIC_DIR) / "site.webmanifest").read_text(encoding="utf-8"))
    assert m["name"] == "Spooltag" and m["short_name"] == "Spooltag"
    assert set(m) <= {"name", "short_name", "icons", "theme_color", "background_color", "display",
                      "start_url", "scope", "id"}                 # nothing that asks for capabilities
    assert m["theme_color"].lower() == "#f6f6f4" and m["background_color"].lower() == "#f6f6f4"   # page bg
    assert m.get("start_url", "/") == "/" and m.get("scope", "/") == "/"
    assert "//" not in json.dumps(m)                               # no external URLs
    sizes = set()
    for icon in m["icons"]:
        assert icon["src"].startswith("/") and not icon["src"].startswith("//")
        r = client.get(icon["src"])
        assert r.status_code == 200 and r.headers["content-type"] == icon["type"]
        w, h = png_size(r.content)
        assert icon["sizes"] == f"{w}x{h}"
        sizes.add(icon["sizes"])
    assert {"192x192", "512x512"} <= sizes


def _head(html: str) -> str:
    return html.split("</head>", 1)[0]


def test_every_page_links_the_icons_once(env):
    client, app = env
    pages = {
        "status": client.get("/").text,
        "settings": client.get("/settings").text,
        "tags": client.get("/tags").text,
        "location": client.get("/locations/dry-box-1").text,
        "scan spool": client.get("/s/42", headers=ANDROID_CHROME_NFC).text,
        "scan result": client.get("/t/sv08/1", headers=ANDROID_CHROME_NFC).text,
        "confirm scan": client.get("/s/42", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                                                    "Sec-Fetch-Dest": "document"}).text,
        "error": client.get("/l/nowhere").text,
    }
    assert "Loaded" in pages["scan result"] and "Confirm this scan" in pages["confirm scan"]
    client.post("/settings/password", data={"action": "set", "new": PASSWORD, "confirm": PASSWORD})
    pages["login"] = TestClient(app).get("/login").text
    assert "Admin login" in pages["login"]
    for name, html in pages.items():
        head = _head(html)
        assert head.count(ICON_LINKS) == 1, name
        for href in ("/favicon-96x96.png", "/favicon.svg", "/favicon.ico", "/apple-touch-icon.png",
                     "/site.webmanifest"):
            assert html.count(f'href="{href}"') == 1, (name, href)


def test_markup_matches_requested_links():
    for tag in ('<link rel="icon" type="image/png" href="/favicon-96x96.png" sizes="96x96">',
                '<link rel="icon" type="image/svg+xml" href="/favicon.svg">',
                '<link rel="shortcut icon" href="/favicon.ico">',
                '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">',
                '<link rel="manifest" href="/site.webmanifest">'):
        assert tag in ICON_LINKS


def test_csp_not_weakened(env):
    client, _ = env
    csp = client.get("/").headers["content-security-policy"]
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";"))
    assert directives == {"default-src": "'none'", "script-src": "'self'", "style-src": "'unsafe-inline'",
                          "img-src": "'self' data:", "manifest-src": "'self'", "form-action": "'self'",
                          "frame-ancestors": "'none'", "base-uri": "'none'"}


def test_icons_only_from_the_fixed_list(env):
    client, _ = env
    for url in ("/favicon.png", "/static/favicon.svg", "/favicon.svg/../main.py", "/main.py", "/app.js",
                "/static/", "/%2e%2e/app/main.py", "/web-app-manifest-1024x1024.png"):
        assert client.get(url).status_code in (404, 405), url
    assert client.get("/favicon.ico", headers={"Host": "rebind.evil.example"}).status_code == 400   # Host check


def test_icons_stay_small():
    """Every page links these, so keep them light (the generated SVG was a 2.4 MB embedded raster)."""
    sizes = {name: (Path(STATIC_DIR) / name).stat().st_size for name in ICON_FILES}
    assert sizes["favicon.svg"] < 256 * 1024
    assert sum(sizes.values()) < 1024 * 1024
