"""spooltag: scan a spool tag and a tool or storage tag, and everything knows where the spool is.

Tag routes (opened by tapping an NFC tag)
  GET  /s/{spool_id}              spool tag            e.g. /s/42
  GET  /t/{printer}/{tool}        printer tool tag     e.g. /t/sv08/3, /t/v0/0
                                  or AMS slot tag      e.g. /t/p1s/1 .. /t/p1s/4 (via Bambuddy)
  GET  /t/{tool}                  tool of the first printer (0.1 tags)
  GET  /l/{location}              storage tag          e.g. /l/dry-box-1
A spool scan pairs with a tool or storage scan, in either order, within the scan timeout set in Settings.

Pages
  GET  /                          every printer's tools, storage, recent scans
  GET  /locations/{location}      what's stored in a location
  GET  /tags                      tag URLs + QR codes
  GET  /settings                  general settings; add/edit/remove printers and storage locations
"""
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from . import pages
from .clients import Spoolman
from .config import Settings, load_settings, valid_timezone
from .service import Service
from .store import BAMBUDDY, KLIPPER, SLUG_RE, Location, Printer, Store, slugify


def create_app(settings: Optional[Settings] = None,
               transport: Optional[httpx.AsyncBaseTransport] = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = httpx.AsyncClient(timeout=8.0, transport=transport)
        store = Store(settings.data_dir)
        # Env vars (or the passed-in settings) only seed what the database doesn't have yet.
        store.save_settings(settings.general(), only_missing=True)
        settings.apply(store.settings())
        legacy = settings.legacy_printer
        if legacy and not store.printers():
            store.save_printer(Printer(slugify(legacy.name) or "printer", legacy.name, legacy.moonraker_url,
                                       legacy.api_key, legacy.num_tools, legacy.save_variables,
                                       legacy.macro_variable))
        app.state.service = Service(settings, store, Spoolman(client, settings.spoolman_url), client)
        yield
        await client.aclose()

    app = FastAPI(title="spooltag", lifespan=lifespan, docs_url=None, redoc_url=None)

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
        if not raw.isdigit() or int(raw) not in numbers:
            what = "an AMS slot" if printer.is_ams else "a tool"
            span = f"{printer.tool_label(numbers[0])}–{printer.tool_label(numbers[-1])}"
            return None, None, f"'{tool}' isn't {what} of {printer.name} ({span})."
        return printer, int(raw), ""

    def pending_text(service: Service) -> str:
        pending = service.store.get_pending(settings.pending_timeout)
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

    # Tag scans ---------------------------------------------------------------------------

    @app.get("/s/{spool_id}", response_class=HTMLResponse)
    async def scan_spool(spool_id: int, request: Request):
        service = svc(request)
        pending = service.store.get_pending(settings.pending_timeout)
        if pending and pending.kind == "tool":
            service.store.clear_pending()
            slug, _, tool = pending.ref.partition(":")
            printer = service.store.printer(slug)
            if printer is not None:
                return pages.done(await service.assign(spool_id, printer, int(tool)), "Loaded")
        if pending and pending.kind == "location":
            service.store.clear_pending()
            location = service.store.location(pending.ref)
            if location is not None:
                return pages.done(await service.store_at(spool_id, location), "Stored")
        try:
            spool = await service.spoolman.spool(spool_id)
        except httpx.HTTPError as e:
            return bad("Spoolman unreachable", str(e) or type(e).__name__, 502)
        if spool is None:
            return bad("Unknown spool", f"Spool #{spool_id} isn't in Spoolman.")
        service.store.set_pending("spool", str(spool_id))
        return pages.scanned_spool(spool, settings.pending_timeout)

    async def _scan_tool(request: Request, printer: Printer, tool: int):
        service = svc(request)
        pending = service.store.get_pending(settings.pending_timeout)
        if pending and pending.kind == "spool":
            service.store.clear_pending()
            return pages.done(await service.assign(int(pending.ref), printer, tool), "Loaded")
        service.store.set_pending("tool", f"{printer.slug}:{tool}")
        lanes, _ = await service.tools(printer)
        return pages.scanned_tool(service.target_name(printer, tool), lanes.get(tool), settings.pending_timeout)

    @app.get("/t/{printer_slug}/{tool}", response_class=HTMLResponse)
    async def scan_tool(printer_slug: str, tool: str, request: Request):
        printer, n, error = tool_ref(svc(request), printer_slug, tool)
        if printer is None:
            return bad("Unknown tool", error)
        return await _scan_tool(request, printer, n)

    @app.get("/t/{tool}", response_class=HTMLResponse)
    async def scan_tool_legacy(tool: str, request: Request):
        printer = svc(request).default_printer()
        if printer is None:
            return bad("No printers", "Add a printer in Settings first.")
        printer, n, error = tool_ref(svc(request), printer.slug, tool)
        if printer is None:
            return bad("Unknown tool", error)
        return await _scan_tool(request, printer, n)

    @app.get("/l/{slug}", response_class=HTMLResponse)
    async def scan_location(slug: str, request: Request):
        service = svc(request)
        location = service.store.location(slug)
        if location is None:
            return bad("Unknown location", f"No storage location '{slug}' in Settings.")
        pending = service.store.get_pending(settings.pending_timeout)
        if pending and pending.kind == "spool":
            service.store.clear_pending()
            return pages.done(await service.store_at(int(pending.ref), location), "Stored")
        service.store.set_pending("location", slug)
        spools, error = await location_spools(service, location)
        return pages.location_contents(location, spools, error, waiting=settings.pending_timeout)

    # Pages -------------------------------------------------------------------------------

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
                            pending_text(service), service.store.recent(), settings.timezone)

    @app.get("/locations/{slug}", response_class=HTMLResponse)
    async def view_location(slug: str, request: Request):
        service = svc(request)
        location = service.store.location(slug)
        if location is None:
            return bad("Unknown location", f"No storage location '{slug}'.")
        spools, error = await location_spools(service, location)
        return pages.location_contents(location, spools, error)

    @app.post("/p/{printer_slug}/tools/{tool}/clear")
    async def clear_tool(printer_slug: str, tool: str, request: Request):
        printer, n, _ = tool_ref(svc(request), printer_slug, tool)
        if printer is not None:
            await svc(request).clear(printer, n)
        return RedirectResponse("/", status_code=303)

    @app.post("/tools/{tool}/clear")
    async def clear_tool_legacy(tool: str, request: Request):
        printer = svc(request).default_printer()
        if printer is not None:
            return await clear_tool(printer.slug, tool, request)
        return RedirectResponse("/", status_code=303)

    @app.post("/pending/clear")
    async def clear_pending(request: Request):
        svc(request).store.clear_pending()
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

    # Settings ----------------------------------------------------------------------------

    def settings_page(request: Request, notice: str = "", error: str = "", code: int = 200) -> HTMLResponse:
        store = svc(request).store
        return HTMLResponse(pages.settings(settings, store.printers(), store.locations(), notice, error), code)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_view(request: Request, saved: str = ""):
        return settings_page(request, notice=saved)

    def saved(text: str) -> RedirectResponse:
        return RedirectResponse("/settings?" + urlencode({"saved": text}), status_code=303)

    @app.post("/settings/general")
    async def save_general(request: Request, spoolman_url: str = Form(...), base_url: str = Form(""),
                           location_format: str = Form(...), unload_location: str = Form(""),
                           pending_timeout: str = Form(...), timezone: str = Form("")):
        spoolman_url, base_url = spoolman_url.strip().rstrip("/"), base_url.strip().rstrip("/")
        location_format, timezone = location_format.strip(), timezone.strip()
        if not spoolman_url.startswith(("http://", "https://")):
            return settings_page(request, error="The Spoolman URL must start with http:// or https://", code=400)
        if base_url and not base_url.startswith(("http://", "https://")):
            return settings_page(request, error="The tag base URL must start with http:// or https://", code=400)
        try:
            sample = location_format.format(printer="SV08", tool=3)
        except (KeyError, IndexError, ValueError):
            sample = ""
        if not sample.strip():
            return settings_page(request, error="The tool location format may only use {printer} and {tool}, "
                                                "e.g. {printer} T{tool}.", code=400)
        if not pending_timeout.strip().isdigit() or not 10 <= int(pending_timeout) <= 3600:
            return settings_page(request, error="The scan timeout must be 10 to 3600 seconds.", code=400)
        if timezone and not valid_timezone(timezone):
            return settings_page(request, error=f"'{timezone}' isn't a time zone name "
                                                f"(use one like America/Denver).", code=400)
        values = {"spoolman_url": spoolman_url, "base_url": base_url, "location_format": location_format,
                  "unload_location": unload_location.strip(), "pending_timeout": str(int(pending_timeout)),
                  "timezone": timezone}
        svc(request).store.save_settings(values)
        settings.apply(values)
        svc(request).spoolman.base = settings.spoolman_url
        return saved("General settings saved.")

    @app.post("/settings/printers")
    async def save_printer(request: Request, name: str = Form(...), num_tools: int = Form(...),
                           moonraker_url: str = Form(...), api_key: str = Form(""), slug: str = Form(""),
                           mode: str = Form("add"), save_variables: Optional[str] = Form(None),
                           macro_variable: Optional[str] = Form(None), kind: str = Form(KLIPPER),
                           remote_id: str = Form("")):
        store = svc(request).store
        name, url = name.strip(), moonraker_url.strip().rstrip("/")
        if kind not in (KLIPPER, BAMBUDDY):
            return settings_page(request, error="Unknown printer type.", code=400)
        rid = 0
        if kind == BAMBUDDY:
            if not remote_id.strip().isdigit() or int(remote_id) < 1:
                return settings_page(request, error="Give the printer's id in Bambuddy (a number, "
                                                    "shown by /api/v1/printers/).", code=400)
            rid = int(remote_id)
        slug = slug.strip().lower() or slugify(name)
        if not name:
            return settings_page(request, error="Give the printer a name.", code=400)
        if not url.startswith(("http://", "https://")):
            return settings_page(request, error="The URL must start with http:// or https://", code=400)
        if not 1 <= num_tools <= 32:
            return settings_page(request, error="Tools must be between 1 and 32.", code=400)
        if not SLUG_RE.match(slug):
            return settings_page(request, error="Short id: lowercase letters, digits and dashes only.", code=400)
        existing = store.printer(slug)
        if mode == "edit" and existing is None:
            return settings_page(request, error=f"Printer '{slug}' no longer exists.", code=404)
        if mode != "edit" and existing is not None:
            return settings_page(request, error=f"The id '{slug}' is already used by {existing.name}; "
                                                f"pick another short id.", code=400)
        ams = kind == BAMBUDDY
        store.save_printer(Printer(slug, name, url, api_key.strip(), num_tools,
                                   save_variables is not None and not ams,
                                   macro_variable is not None and not ams, kind, rid))
        return saved(f"{name} {'saved' if mode == 'edit' else 'added'}.")

    @app.post("/settings/printers/{slug}/delete")
    async def delete_printer(slug: str, request: Request):
        printer = svc(request).store.printer(slug)
        svc(request).store.delete_printer(slug)
        return saved(f"{printer.name if printer else slug} removed.")

    @app.post("/settings/locations")
    async def add_location(request: Request, name: str = Form(...), slug: str = Form("")):
        store = svc(request).store
        name = name.strip()
        slug = slug.strip().lower() or slugify(name)
        if not name:
            return settings_page(request, error="Give the location a name.", code=400)
        if not SLUG_RE.match(slug):
            return settings_page(request, error="Short id: lowercase letters, digits and dashes only.", code=400)
        if store.location(slug) or any(l.name.casefold() == name.casefold() for l in store.locations()):
            return settings_page(request, error=f"A location named '{name}' or with id '{slug}' already exists.",
                                 code=400)
        store.add_location(Location(slug, name))
        return saved(f"{name} added.")

    @app.post("/settings/locations/{slug}/delete")
    async def delete_location(slug: str, request: Request):
        loc = svc(request).store.location(slug)
        svc(request).store.delete_location(slug)
        return saved(f"{loc.name if loc else slug} removed.")

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz():
        return "ok"

    return app


app = create_app()
