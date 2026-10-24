from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.templating import Jinja2Templates

from . import __version__
from .access import parse_countries
from .config import Config
from .database import connect, init_db
from .deps import RedirectToLogin
from .duplicates import asset_location
from .geo import GeoResolver
from .i18n import request_language, translate
from .routes_admin import router as admin_router
from .routes_api import router as api_router
from .routes_public import router as public_router
from .storage import Storage
from .trash import purge_expired
from .web import (
    asset_url,
    download_url,
    external_image_url,
    format_datetime,
    format_size,
    intro_style,
    json_pretty,
    media_url,
    render,
)

ADMIN_RESERVED = {
    "api",
    "media",
    "static",
    "images",
    "albums",
    "boxes",
    "download",
    "embed",
    "view",
    "unlock",
    "search",
    "browse",
    "favicon.ico",
    "healthz",
    "about",
    "branding",
    "feed",
    "upload",
    "stats",
    "language",
}


class AdminRewriteMiddleware:
    """Map the operator-configured admin prefix onto the internal /admin routes."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        state = scope.setdefault("state", {})
        state["external_path"] = path
        configured = "/" + getattr(scope["app"].state, "admin_path", "admin")
        configured = configured.rstrip("/") or "/admin"
        if configured != "/admin" and (path == "/admin" or path.startswith("/admin/")):
            response = PlainTextResponse("Not Found", status_code=404)
            await response(scope, receive, send)
            return
        if path == configured or path.startswith(configured + "/"):
            new_path = "/admin" + path[len(configured) :]
            scope = dict(scope)
            scope["path"] = new_path
            scope["raw_path"] = new_path.encode("utf-8")
            scope["state"] = state
            state["admin_rewritten"] = True
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob: https:; media-src 'self' https:; "
            "style-src 'self' 'unsafe-inline' https:; font-src 'self' data: https:; "
            "script-src 'self' 'unsafe-inline'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        )
        if request.url.path.startswith("/admin") or getattr(request.state, "admin_rewritten", False):
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.url.path.startswith("/static/") and request.query_params.get("v"):
            # Versioned asset URLs are immutable; without this the browser
            # revalidates them and the access log fills with harmless 304s.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif request.url.path == "/static/favicon.svg":
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        return response


def _admin_path(db_path: Path) -> str:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'admin_path'").fetchone()
        value = (row["value"] if row else "admin").strip().strip("/")
        if not value or any(ch in value for ch in "/\\"):
            return "admin"
        return value
    finally:
        conn.close()


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()
    init_db(config)
    admin_path = _admin_path(config.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        def sweep_trash() -> None:
            db = connect(config.db_path)
            try:
                purge_expired(db, app.state.storage)
                db.commit()
            finally:
                db.close()

        async def trash_janitor() -> None:
            while True:
                await asyncio.sleep(3600)
                try:
                    await asyncio.to_thread(sweep_trash)
                except Exception:
                    # A temporary database/remote-storage failure must not kill
                    # the hourly cleanup loop.
                    continue

        await asyncio.to_thread(sweep_trash)
        janitor = asyncio.create_task(trash_janitor())
        try:
            yield
        finally:
            janitor.cancel()
            with suppress(asyncio.CancelledError):
                await janitor
            app.state.geo.close()

    app = FastAPI(
        title="Ostensory",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.storage = Storage(config)
    app.state.geo = GeoResolver(config)
    app.state.admin_path = admin_path
    config.static_dir.mkdir(parents=True, exist_ok=True)

    templates = Jinja2Templates(directory=str(config.templates_dir))
    templates.env.filters["filesize"] = format_size
    templates.env.filters["datetime"] = format_datetime
    templates.env.filters["json_pretty"] = json_pretty
    templates.env.filters["country_list"] = lambda value: ", ".join(parse_countries(value))
    templates.env.globals.update(
        asset_url=asset_url,
        asset_location=asset_location,
        media_url=media_url,
        download_url=download_url,
        external_image_url=external_image_url,
        intro_style=intro_style,
        format_size=format_size,
        format_datetime=format_datetime,
    )
    app.state.templates = templates

    app.add_middleware(AdminRewriteMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(config.static_dir)), name="static")
    app.include_router(public_router)
    app.include_router(api_router)
    app.include_router(admin_router, prefix="/admin")

    @app.exception_handler(RedirectToLogin)
    async def redirect_to_login(request: Request, exc: RedirectToLogin):
        return PlainTextResponse("", status_code=303, headers={"Location": exc.url})

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers or None,
            )
        if exc.status_code in {301, 302, 303, 307, 308}:
            return PlainTextResponse("", status_code=exc.status_code, headers=exc.headers or {})
        db = connect(config.db_path)
        try:
            template = "451.html" if exc.status_code == 451 else "error.html"
            language = request_language(request)
            key = {
                403: "error.forbidden",
                404: "error.page_missing",
                405: "error.method",
                429: "error.rate_limit",
                451: "region.title",
            }.get(exc.status_code, "error.request_failed")
            title = translate(language, key)
            detail = str(exc.detail or "").strip()
            if exc.status_code == 451 or detail in {"Not Found", "Forbidden", "Method Not Allowed"}:
                message = title
            else:
                message = detail or title
            return render(
                request,
                db,
                template,
                {
                    "status_code": exc.status_code,
                    "title": title,
                    "message": message,
                    "admin": None,
                },
                status_code=exc.status_code,
            )
        finally:
            db.close()

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "服务器内部错误"}, status_code=500)
        db = connect(config.db_path)
        try:
            return render(
                request,
                db,
                "error.html",
                {
                    "status_code": 500,
                    "title": translate(request_language(request), "error.server"),
                    "message": translate(request_language(request), "error.server_hint"),
                    "admin": None,
                },
                status_code=500,
            )
        finally:
            db.close()

    return app


app = create_app()