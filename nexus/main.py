"""FastAPI application factory for NEXUS GTM."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from nexus.api.routers import all_routers
from nexus.core.config import get_settings
from nexus.core.db import dispose_db, get_sessionmaker, init_db
from nexus.core.middleware import RequestContextMiddleware

logging.basicConfig(level=logging.INFO)

# The compiled React SPA (Vite build output). See frontend/vite.config.ts.
_DIST_DIR = Path(__file__).parent / "web" / "dist"


class SPAStaticFiles(StaticFiles):
    """Static files with single-page-app fallback.

    Serves hashed build assets directly; for any unmatched path that is not an
    API call, returns ``index.html`` so the client-side router can handle deep
    links (e.g. ``/accounts/123``) on a hard refresh.
    """

    async def get_response(self, path: str, scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # Don't mask API/asset 404s with the HTML shell — only fall back for
            # navigation routes the client router owns. Normalize the separator
            # because StaticFiles resolves paths with the OS-native one.
            normalized = path.replace("\\", "/")
            if exc.status_code == 404 and not normalized.startswith(("api/", "assets/")):
                return await super().get_response("index.html", scope)
            raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    from nexus.ingestion.crm_sync import register_crm_sync_subscribers

    register_crm_sync_subscribers()
    yield
    await dispose_db()


def _maybe_enable_metrics(app: FastAPI) -> None:
    """Expose Prometheus ``/metrics`` if the optional ``metrics`` extra is installed."""
    try:
        from prometheus_fastapi_instrumentator import Instrumentator
    except ImportError:
        return
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="NEXUS GTM",
        description="AI-powered Go-To-Market intelligence platform (Pocus-style MVP).",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(RequestContextMiddleware)

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        """Liveness: the process is up and serving."""
        return {"status": "ok", "env": settings.env}

    @app.get("/ready", tags=["meta"])
    async def ready() -> JSONResponse:
        """Readiness: dependencies (the database) are reachable."""
        try:
            async with get_sessionmaker()() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"status": "unavailable", "db": "down"}, status_code=503)
        return JSONResponse({"status": "ready", "db": "up"})

    _maybe_enable_metrics(app)

    api = "/api"
    for router in all_routers:
        app.include_router(router, prefix=api)

    # Serve the built SPA last so it acts as the catch-all: API routes,
    # /health, /ready, /docs and /metrics are registered above and take
    # precedence over this "/" mount.
    if (_DIST_DIR / "index.html").exists():
        app.mount("/", SPAStaticFiles(directory=_DIST_DIR, html=True), name="web")

    return app


app = create_app()
