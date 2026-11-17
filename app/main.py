"""spooltag: scan a spool tag and a tool or storage tag, and everything knows where the spool is.

Tag routes (opened by tapping an NFC tag; never need a login)
  GET  /s/{spool_id}              spool tag            e.g. /s/42
  GET  /t/{printer}/{tool}        printer tool tag     e.g. /t/sv08/3, /t/v0/0
                                  or AMS slot tag      e.g. /t/p1s/1 .. /t/p1s/4 (via Bambuddy)
  GET  /t/{tool}                  tool of the first printer (0.1 tags)
  GET  /l/{location}              storage tag          e.g. /l/dry-box-1
A spool scan pairs with a tool or storage scan from the same phone (browser cookie), in either
order, within the scan timeout set in Settings. A scan URL opened by another website instead of
a tag shows a confirm page (POST to the same URL) instead of acting; see websec.py.

Pages
  GET  /                          every printer's tools, storage, recent scans
  GET  /locations/{location}      what's stored in a location
  GET  /tags                      tag URLs + QR codes
  GET  /settings                  general settings, printers, storage, admin password (admin)
  GET  /login                     only when an admin password is set
"""
import re
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, Form, Path, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from . import pages
from .auth import (HASH_KEY, MIN_LENGTH, SESSION_COOKIE, SESSION_DAYS, LoginLimiter, hash_password, is_admin,
                   new_session, password_set, safe_next, token_hash, verify_password)
from .clients import Spoolman
from .config import Settings, load_settings, split_hosts, valid_timezone
from .netguard import GuardedTransport, Resolver, check_url, system_resolve, url_shape_error
from .service import Service
from .store import BAMBUDDY, KLIPPER, SLUG_RE, Location, Printer, Store, slugify
from .websec import (DEVICE_COOKIE, SECURITY_HEADERS, UNSAFE_METHODS, device_id, existing_device_id,
                     host_allowed, host_only, is_https, is_ip, is_top_level_navigation, scan_is_direct,
                     unsafe_request_allowed)

TOOL_RE = re.compile(r"^[0-9]{1,3}$")
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?)*$")
MAX_SPOOL_ID = 2_147_483_647
MAX_NAME = 100


class LoginRequired(Exception):
    pass


def create_app(settings: Optional[Settings] = None,
               transport: Optional[httpx.AsyncBaseTransport] = None,
               resolver: Optional[Resolver] = None) -> FastAPI:
    settings = settings or load_settings()
    resolver = resolver or system_resolve
    limiter = LoginLimiter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        guarded = GuardedTransport(transport or httpx.AsyncHTTPTransport(),
                                   lambda: settings.allow_public_urls, resolver)
        # trust_env=False: proxy env vars would route around the guard, and nothing here needs them.
        client = httpx.AsyncClient(timeout=8.0, transport=guarded, trust_env=False)
        store = Store(settings.data_dir)
        # Env vars (or the passed-in settings) only seed what the database doesn't have yet.
        store.save_settings(settings.general(), only_missing=True)
        settings.apply(store.settings())
        if settings.admin_password_seed and not password_set(store):
            store.save_settings({HASH_KEY: hash_password(settings.admin_password_seed)})
        legacy = settings.legacy_printer
        if legacy and not store.printers():
            store.save_printer(Printer(slugify(legacy.name) or "printer", legacy.name, legacy.moonraker_url,
                                       legacy.api_key, legacy.num_tools, legacy.save_variables,
                                       legacy.macro_variable))
        app.state.service = Service(settings, store, Spoolman(client, settings.spoolman_url), client)
        yield
        await client.aclose()

    app = FastAPI(title="spooltag", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    # Request guards ----------------------------------------------------------------------

    @app.middleware("http")
    async def guards(request: Request, call_next):
        if not host_allowed(request.headers.get("host", ""), settings.allowed_hosts()):
            response: Response = PlainTextResponse(
                f"spooltag doesn't answer to the host name '{host_only(request.headers.get('host', ''))}'. "
                "Open it by IP address and add the name under Settings → General, or set ALLOWED_HOSTS.", 400)
        elif request.method in UNSAFE_METHODS and not unsafe_request_allowed(request):
            response = PlainTextResponse("Cross-site request refused.", 403)
        else:
            response = await call_next(request)
            if getattr(request.state, "new_device", False):
                response.set_cookie(DEVICE_COOKIE, request.state.device, max_age=400 * 86400, httponly=True,
                                    samesite="lax", secure=is_https(request), path="/")
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def require_admin(request: Request) -> None:
        if not is_admin(request, svc(request).store):
            raise LoginRequired()

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, _exc: LoginRequired):
        path = request.url.path
        if request.method != "GET":
            path = "/settings" if path.startswith("/settings") else "/"
        return RedirectResponse("/login?" + urlencode({"next": path}), status_code=303)

    admin = [Depends(require_admin)]

    def svc(request: Request) -> Service:
        return request.app.state.service

    def bad(title: str, text: str, code: int = 404) -> HTMLResponse:
        return HTMLResponse(pages.message(title, text, bad=True), code)

    def tool_ref(service: Service, printer_slug: str, tool: str) -> tuple[Optional[Printer], Optional[int], str]:
        """(printer, tool, error) for a tool tag."""
        printer = service.store.printer(printer_slug)
        if printer is None:
            return None, None, f"No printer '{printer_slug}' in Settings."
        raw = tool.strip().lstrip("tT")
        numbers = printer.tool_numbers()
        if not TOOL_RE.match(raw) or int(raw) not in numbers:
            what = "an AMS slot" if printer.is_ams else "a tool"
            span = f"{printer.tool_label(numbers[0])}–{printer.tool_label(numbers[-1])}"
            return None, None, f"'{tool}' isn't {what} of {printer.name} ({span})."
        return printer, int(raw), ""

    def pending_text(request: Request) -> str:
        service = svc(request)
        device = existing_device_id(request)
        pending = service.store.get_pending(device, settings.pending_timeout) if device else None
        if not pending:
            return ""
        if pending.kind == "spool":
            return f"spool #{pending.ref}"
        if pending.kind == "tool":
            slug, _, tool = pending.ref.partition(":")
            printer = service.store.printer(slug)
            return service.target_name(printer, int(tool)) if printer else pending.ref
        loc = service.store.location(pending.ref)
        return loc.name if loc else pending.ref

    async def location_spools(service: Service, location: Location) -> tuple[list[dict], Optional[str]]:
        try:
            return await service.spoolman.spools(location=location.name), None
        except httpx.HTTPError as e:
            return [], f"Spoolman unreachable ({type(e).__name__})"

    def scan_guard(request: Request, what: str) -> Optional[Response]:
        """None if this GET scan may act now; otherwise a confirm page or a refusal."""
        if scan_is_direct(request):
            return None
        if not is_top_level_navigation(request):
            return PlainTextResponse("Scan links only work when opened directly.", 403)
        return HTMLResponse(pages.confirm_scan(what, request.url.path))

    # Tag scans ---------------------------------------------------------------------------

    async def _scan_spool(request: Request, spool_id: int) -> HTMLResponse:
        service = svc(request)
        device = device_id(request)
        pending = service.store.take_pending(device, settings.pending_timeout, ("tool", "location"))
        if pending and pending.kind == "tool":
            slug, _, tool = pending.ref.partition(":")
            printer = service.store.printer(slug)
            if printer is not None:
                return HTMLResponse(pages.done(await service.assign(spool_id, printer, int(tool)), "Loaded"))
        if pending and pending.kind == "location":
            location = service.store.location(pending.ref)
            if location is not None:
                return HTMLResponse(pages.done(await service.store_at(spool_id, location), "Stored"))
        try:
            spool = await service.spoolman.spool(spool_id)
        except httpx.HTTPError as e:
            return bad("Spoolman unreachable", str(e) or type(e).__name__, 502)
        if spool is None:
            return bad("Unknown spool", f"Spool #{spool_id} isn't in Spoolman.")
        service.store.set_pending(device, "spool", str(spool_id))
        return HTMLResponse(pages.scanned_spool(spool, settings.pending_timeout))

    async def _scan_tool(request: Request, printer: Printer, tool: int) -> HTMLResponse:
        service = svc(request)
        device = device_id(request)
        pending = service.store.take_pending(device, settings.pending_timeout, ("spool",))
        if pending:
            return HTMLResponse(pages.done(await service.assign(int(pending.ref), printer, tool), "Loaded"))
        service.store.set_pending(device, "tool", f"{printer.slug}:{tool}")
        lanes, _ = await service.tools(printer)
        return HTMLResponse(pages.scanned_tool(service.target_name(printer, tool), lanes.get(tool),
                                               settings.pending_timeout))

    async def _scan_location(request: Request, location: Location) -> HTMLResponse:
        service = svc(request)
        device = device_id(request)
        pending = service.store.take_pending(device, settings.pending_timeout, ("spool",))
        if pending:
            return HTMLResponse(pages.done(await service.store_at(int(pending.ref), location), "Stored"))
        service.store.set_pending(device, "location", location.slug)
        spools, error = await location_spools(service, location)
        return HTMLResponse(pages.location_contents(location, spools, error, waiting=settings.pending_timeout))

    def _tool_target(request: Request, printer_slug: Optional[str], tool: str):
        service = svc(request)
        if printer_slug is None:
            first = service.default_printer()
            if first is None:
                return None, None, bad("No printers", "Add a printer in Settings first.")
            printer_slug = first.slug
        printer, n, error = tool_ref(service, printer_slug, tool)
        if printer is None:
            return None, None, bad("Unknown tool", error)
        return printer, n, None

    SpoolId = Path(ge=1, le=MAX_SPOOL_ID)

    @app.get("/s/{spool_id}", response_class=HTMLResponse)
    async def scan_spool(request: Request, spool_id: int = SpoolId):
        return scan_guard(request, f"Spool #{spool_id}") or await _scan_spool(request, spool_id)

    @app.post("/s/{spool_id}", response_class=HTMLResponse)
    async def scan_spool_confirmed(request: Request, spool_id: int = SpoolId):
        return await _scan_spool(request, spool_id)

    async def _tool_route(request: Request, printer_slug: Optional[str], tool: str, confirmed: bool):
        printer, n, error = _tool_target(request, printer_slug, tool)
        if printer is None:
            return error
        if not confirmed and (guard := scan_guard(request, svc(request).target_name(printer, n))):
            return guard
        return await _scan_tool(request, printer, n)

    @app.get("/t/{printer_slug}/{tool}", response_class=HTMLResponse)
    async def scan_tool(printer_slug: str, tool: str, request: Request):
        return await _tool_route(request, printer_slug, tool, confirmed=False)

    @app.post("/t/{printer_slug}/{tool}", response_class=HTMLResponse)
    async def scan_tool_confirmed(printer_slug: str, tool: str, request: Request):
        return await _tool_route(request, printer_slug, tool, confirmed=True)

    @app.get("/t/{tool}", response_class=HTMLResponse)
    async def scan_tool_legacy(tool: str, request: Request):
        return await _tool_route(request, None, tool, confirmed=False)

    @app.post("/t/{tool}", response_class=HTMLResponse)
    async def scan_tool_legacy_confirmed(tool: str, request: Request):
        return await _tool_route(request, None, tool, confirmed=True)

    async def _location_route(request: Request, slug: str, confirmed: bool):
        location = svc(request).store.location(slug)
        if location is None:
            return bad("Unknown location", f"No storage location '{slug}' in Settings.")
        if not confirmed and (guard := scan_guard(request, location.name)):
            return guard
        return await _scan_location(request, location)

    @app.get("/l/{slug}", response_class=HTMLResponse)
    async def scan_location(slug: str, request: Request):
        return await _location_route(request, slug, confirmed=False)

    @app.post("/l/{slug}", response_class=HTMLResponse)
    async def scan_location_confirmed(slug: str, request: Request):
        return await _location_route(request, slug, confirmed=True)

    # Pages -------------------------------------------------------------------------------

    @app.get("/static/app.js")
    async def app_js():
        return Response(pages.APP_JS, media_type="text/javascript",
                        headers={"Cache-Control": "max-age=3600"})

    @app.get("/", response_class=HTMLResponse)
    async def status(request: Request):
        service = svc(request)
        grids = []
        for printer in service.store.printers():
            lanes, error = await service.tools(printer)
            grids.append(pages.printer_grid(printer, lanes, error))
        locations = service.store.locations()
        counts: dict[str, int] = {}
        if locations:
            try:
                for spool in await service.spoolman.spools():
                    counts[spool.get("location") or ""] = counts.get(spool.get("location") or "", 0) + 1
            except httpx.HTTPError:
                counts = {l.name: -1 for l in locations}
        return pages.status(grids, [(l, counts.get(l.name, 0)) for l in locations],
                            pending_text(request), service.store.recent(), settings.timezone)

    @app.get("/locations/{slug}", response_class=HTMLResponse)
    async def view_location(slug: str, request: Request):
        service = svc(request)
        location = service.store.location(slug)
        if location is None:
            return bad("Unknown location", f"No storage location '{slug}'.")
        spools, error = await location_spools(service, location)
        return pages.location_contents(location, spools, error)

    @app.post("/p/{printer_slug}/tools/{tool}/clear", dependencies=admin)
    async def clear_tool(printer_slug: str, tool: str, request: Request):
        printer, n, _ = tool_ref(svc(request), printer_slug, tool)
        if printer is not None:
            await svc(request).clear(printer, n)
        return RedirectResponse("/", status_code=303)

    @app.post("/tools/{tool}/clear", dependencies=admin)
    async def clear_tool_legacy(tool: str, request: Request):
        printer = svc(request).default_printer()
        if printer is not None:
            return await clear_tool(printer.slug, tool, request)
        return RedirectResponse("/", status_code=303)

    @app.post("/pending/clear")
    async def clear_pending(request: Request):
        device = existing_device_id(request)
        if device:
            svc(request).store.clear_pending(device)
        return RedirectResponse("/", status_code=303)

    @app.get("/tags", response_class=HTMLResponse)
    async def tags(request: Request):
        service = svc(request)
        base = settings.base_url or str(request.base_url).rstrip("/")
        try:
            spools, error = await service.spoolman.spools(), None
        except httpx.HTTPError as e:
            spools, error = [], f"Spoolman unreachable ({type(e).__name__})"
        return pages.tags(base, service.store.printers(), service.store.locations(), spools, error)

    # Login -------------------------------------------------------------------------------

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def with_session(response: Response, request: Request, token: Optional[str]) -> Response:
        if token is None:
            response.delete_cookie(SESSION_COOKIE, path="/")
        else:
            response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                                samesite="strict", secure=is_https(request), path="/")
        return response

    async def check_password(request: Request, password: str) -> Optional[str]:
        """None if right; otherwise an error message (and the attempt counts against the limit)."""
        key = client_key(request)
        if limiter.blocked(key):
            return "Too many wrong passwords. Wait 15 minutes and try again."
        if await run_in_threadpool(verify_password, password[:1024], svc(request).store.get_setting(HASH_KEY)):
            limiter.reset(key)
            return None
        limiter.fail(key)
        return "Wrong password."

    @app.get("/login", response_class=HTMLResponse)
    async def login_view(request: Request, next: str = "/settings"):  # noqa: A002
        if not password_set(svc(request).store):
            return RedirectResponse(safe_next(next), status_code=303)
        return pages.login(safe_next(next))

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request, password: str = Form(...), next: str = Form("/settings")):  # noqa: A002
        store = svc(request).store
        target = safe_next(next)
        if not password_set(store):
            return RedirectResponse(target, status_code=303)
        if error := await check_password(request, password):
            code = 429 if "Too many" in error else 401
            return HTMLResponse(pages.login(target, error), code)
        return with_session(RedirectResponse(target, status_code=303), request, new_session(store))

    @app.post("/logout")
    async def logout(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            svc(request).store.delete_session(token_hash(token))
        return with_session(RedirectResponse("/", status_code=303), request, None)

    # Settings ----------------------------------------------------------------------------

    def settings_page(request: Request, notice: str = "", error: str = "", code: int = 200) -> HTMLResponse:
        store = svc(request).store
        return HTMLResponse(pages.settings(settings, store.printers(), store.locations(), notice, error,
                                           has_password=password_set(store)), code)

    @app.get("/settings", response_class=HTMLResponse, dependencies=admin)
    async def settings_view(request: Request, saved: str = ""):
        return settings_page(request, notice=saved[:200])

    def saved(text: str) -> RedirectResponse:
        return RedirectResponse("/settings?" + urlencode({"saved": text}), status_code=303)

    @app.post("/settings/general", dependencies=admin)
    async def save_general(request: Request, spoolman_url: str = Form(...), base_url: str = Form(""),
                           location_format: str = Form(...), unload_location: str = Form(""),
                           pending_timeout: str = Form(...), timezone: str = Form(""),
                           extra_hosts: str = Form(""), allow_public_urls: Optional[str] = Form(None)):
        spoolman_url, base_url = spoolman_url.strip().rstrip("/"), base_url.strip().rstrip("/")
        location_format, timezone = location_format.strip(), timezone.strip()
        allow_public = allow_public_urls is not None
        if err := await check_url(spoolman_url, allow_public, resolver):
            return settings_page(request, error=f"The Spoolman URL {err}.", code=400)
        if base_url and (err := url_shape_error(base_url)):
            return settings_page(request, error=f"The tag base URL {err}.", code=400)
        try:
            sample = location_format.format(printer="SV08", tool=3)
        except (KeyError, IndexError, ValueError, AttributeError):
            sample = ""
        if not sample.strip() or len(location_format) > MAX_NAME:
            return settings_page(request, error="The tool location format may only use {printer} and {tool}, "
                                                "e.g. {printer} T{tool}.", code=400)
        if len(unload_location.strip()) > MAX_NAME:
            return settings_page(request, error="The unload location is too long.", code=400)
        pending_timeout = pending_timeout.strip()
        if not re.fullmatch(r"[0-9]{1,4}", pending_timeout) or not 10 <= int(pending_timeout) <= 3600:
            return settings_page(request, error="The scan timeout must be 10 to 3600 seconds.", code=400)
        if timezone and not valid_timezone(timezone):
            return settings_page(request, error=f"'{timezone}' isn't a time zone name "
                                                f"(use one like America/Denver).", code=400)
        hosts = split_hosts(extra_hosts)
        if bad_hosts := [h for h in hosts if not (HOSTNAME_RE.match(h) or is_ip(h))]:
            return settings_page(request, error=f"Not a host name: {', '.join(bad_hosts)}", code=400)
        values = {"spoolman_url": spoolman_url, "base_url": base_url, "location_format": location_format,
                  "unload_location": unload_location.strip(), "pending_timeout": str(int(pending_timeout)),
                  "timezone": timezone, "extra_hosts": " ".join(hosts),
                  "allow_public_urls": "1" if allow_public else "0"}
        # Don't let a save lock out the page it was made from.
        trial = Settings(**{**settings.__dict__})
        trial.apply(values)
        if not host_allowed(request.headers.get("host", ""), trial.allowed_hosts()):
            return settings_page(request, error=f"You opened this page as '{host_only(request.headers['host'])}'; "
                                                f"add that name under 'Other host names' or you'd be locked out.",
                                 code=400)
        svc(request).store.save_settings(values)
        settings.apply(values)
        svc(request).spoolman.base = settings.spoolman_url
        return saved("General settings saved.")

    @app.post("/settings/printers", dependencies=admin)
    async def save_printer(request: Request, name: str = Form(...), num_tools: int = Form(...),
                           moonraker_url: str = Form(...), api_key: str = Form(""), slug: str = Form(""),
                           mode: str = Form("add"), save_variables: Optional[str] = Form(None),
                           macro_variable: Optional[str] = Form(None), kind: str = Form(KLIPPER),
                           remote_id: str = Form(""), clear_api_key: Optional[str] = Form(None)):
        store = svc(request).store
        name, url = name.strip(), moonraker_url.strip().rstrip("/")
        if kind not in (KLIPPER, BAMBUDDY):
            return settings_page(request, error="Unknown printer type.", code=400)
        rid = 0
        if kind == BAMBUDDY:
            if not re.fullmatch(r"[0-9]{1,9}", remote_id.strip()) or int(remote_id) < 1:
                return settings_page(request, error="Give the printer's id in Bambuddy (a number, "
                                                    "shown by /api/v1/printers/).", code=400)
            rid = int(remote_id)
        slug = slug.strip().lower() or slugify(name)
        if not name or len(name) > MAX_NAME:
            return settings_page(request, error="Give the printer a name (up to 100 characters).", code=400)
        if err := await check_url(url, settings.allow_public_urls, resolver):
            return settings_page(request, error=f"The URL {err}.", code=400)
        if not 1 <= num_tools <= 32:
            return settings_page(request, error="Tools must be between 1 and 32.", code=400)
        if not SLUG_RE.match(slug):
            return settings_page(request, error="Short id: lowercase letters, digits and dashes only.", code=400)
        if len(api_key) > 512 or any(ord(c) < 0x20 for c in api_key):
            return settings_page(request, error="That API key doesn't look right.", code=400)
        existing = store.printer(slug)
        if mode == "edit" and existing is None:
            return settings_page(request, error=f"Printer '{slug}' no longer exists.", code=404)
        if mode != "edit" and existing is not None:
            return settings_page(request, error=f"The id '{slug}' is already used by {existing.name}; "
                                                f"pick another short id.", code=400)
        key = api_key.strip()
        if mode == "edit" and not key and clear_api_key is None:
            key = existing.api_key            # write-only field: blank keeps the saved key
        ams = kind == BAMBUDDY
        store.save_printer(Printer(slug, name, url, key, num_tools,
                                   save_variables is not None and not ams,
                                   macro_variable is not None and not ams, kind, rid))
        return saved(f"{name} {'saved' if mode == 'edit' else 'added'}.")

    @app.post("/settings/printers/{slug}/delete", dependencies=admin)
    async def delete_printer(slug: str, request: Request):
        printer = svc(request).store.printer(slug)
        svc(request).store.delete_printer(slug)
        return saved(f"{printer.name if printer else slug} removed.")

    @app.post("/settings/locations", dependencies=admin)
    async def add_location(request: Request, name: str = Form(...), slug: str = Form("")):
        store = svc(request).store
        name = name.strip()
        slug = slug.strip().lower() or slugify(name)
        if not name or len(name) > MAX_NAME:
            return settings_page(request, error="Give the location a name (up to 100 characters).", code=400)
        if not SLUG_RE.match(slug):
            return settings_page(request, error="Short id: lowercase letters, digits and dashes only.", code=400)
        if store.location(slug) or any(l.name.casefold() == name.casefold() for l in store.locations()):
            return settings_page(request, error=f"A location named '{name}' or with id '{slug}' already exists.",
                                 code=400)
        store.add_location(Location(slug, name))
        return saved(f"{name} added.")

    @app.post("/settings/locations/{slug}/delete", dependencies=admin)
    async def delete_location(slug: str, request: Request):
        loc = svc(request).store.location(slug)
        svc(request).store.delete_location(slug)
        return saved(f"{loc.name if loc else slug} removed.")

    @app.post("/settings/password", dependencies=admin)
    async def change_password(request: Request, action: str = Form(...), current: str = Form(""),
                              new: str = Form(""), confirm: str = Form("")):
        store = svc(request).store
        if password_set(store) and (error := await check_password(request, current)):
            return settings_page(request, error=error, code=429 if "Too many" in error else 401)
        if action == "remove":
            store.delete_setting(HASH_KEY)
            store.delete_all_sessions()
            return with_session(saved("Admin password removed."), request, None)
        if action != "set":
            return settings_page(request, error="Unknown action.", code=400)
        if len(new) < MIN_LENGTH or len(new) > 1024:
            return settings_page(request, error=f"Use at least {MIN_LENGTH} characters.", code=400)
        if new != confirm:
            return settings_page(request, error="The two new passwords don't match.", code=400)
        store.save_settings({HASH_KEY: await run_in_threadpool(hash_password, new)})
        store.delete_all_sessions()            # sign out every other browser
        return with_session(saved("Admin password saved."), request, new_session(store))

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz():
        return "ok"

    return app


app = create_app()
