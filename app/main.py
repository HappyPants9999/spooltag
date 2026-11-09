"""spooltag: scan a spool tag and a tool tag, and the printer knows what's loaded.

Routes
  GET  /s/{spool_id}     spool tag  (e.g. /s/42)
  GET  /t/{tool}         tool tag   (e.g. /t/3 or /t/T3)
  GET  /                 status of every tool
  GET  /tags             tag URLs + QR codes for writing NFC tags
  POST /tools/{n}/clear  unload a tool
  GET  /healthz          liveness
Scans pair up in either order within PENDING_TIMEOUT seconds.
"""
import re
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from . import pages
from .clients import Moonraker, Spoolman
from .config import Settings, load_settings
from .service import Service
from .store import Store


def create_app(settings: Optional[Settings] = None,
               transport: Optional[httpx.AsyncBaseTransport] = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = httpx.AsyncClient(timeout=8.0, transport=transport)
        app.state.service = Service(
            settings, Store(settings.data_dir), Spoolman(client, settings.spoolman_url),
            Moonraker(client, settings.moonraker_url, settings.moonraker_api_key)
            if settings.moonraker_url else None)
        yield
        await client.aclose()

    app = FastAPI(title="spooltag", lifespan=lifespan, docs_url=None, redoc_url=None)

    def svc(request: Request) -> Service:
        return request.app.state.service

    def parse_tool(raw: str) -> Optional[int]:
        m = re.fullmatch(r"[tT]?(\d{1,2})", raw.strip())
        return int(m.group(1)) if m else None

    @app.get("/s/{spool_id}", response_class=HTMLResponse)
    async def scan_spool(spool_id: int, request: Request):
        service = svc(request)
        pending = service.store.get_pending(settings.pending_timeout)
        if pending and pending.kind == "tool":
            service.store.clear_pending()
            return pages.assigned(await service.assign(spool_id, pending.value))
        try:
            spool = await service.spoolman.spool(spool_id)
        except httpx.HTTPError as e:
            return HTMLResponse(pages.message("Spoolman unreachable", str(e) or type(e).__name__, bad=True), 502)
        if spool is None:
            return HTMLResponse(pages.message("Unknown spool", f"Spool #{spool_id} isn't in Spoolman.", bad=True), 404)
        service.store.set_pending("spool", spool_id)
        return pages.scanned_spool(spool, settings.pending_timeout)

    @app.get("/t/{tool}", response_class=HTMLResponse)
    async def scan_tool(tool: str, request: Request):
        service = svc(request)
        n = parse_tool(tool)
        if n is None or not service.valid_tool(n):
            return HTMLResponse(pages.message("Unknown tool", f"'{tool}' isn't T0–T{settings.num_tools - 1}.", bad=True), 404)
        pending = service.store.get_pending(settings.pending_timeout)
        if pending and pending.kind == "spool":
            service.store.clear_pending()
            return pages.assigned(await service.assign(pending.value, n))
        service.store.set_pending("tool", n)
        lanes, _ = await service.tools()
        return pages.scanned_tool(n, lanes.get(n), settings.pending_timeout)

    @app.get("/", response_class=HTMLResponse)
    async def status(request: Request):
        service = svc(request)
        lanes, error = await service.tools()
        return pages.status(settings.num_tools, lanes, error,
                            service.store.get_pending(settings.pending_timeout), service.store.recent())

    @app.post("/tools/{tool}/clear")
    async def clear_tool(tool: int, request: Request):
        service = svc(request)
        if service.valid_tool(tool):
            await service.clear(tool)
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
        return pages.tags(base, settings.num_tools, spools, error)

    @app.get("/healthz", response_class=PlainTextResponse)
    async def healthz():
        return "ok"

    return app


app = create_app()
