"""FastAPI application factory for NEXUS GTM."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from nexus.api.routers import all_routers
from nexus.billing.errors import BillingThrottled, QuotaExceeded
from nexus.core.config import get_settings
from nexus.core.db import dispose_db, get_sessionmaker, init_db
from nexus.core.middleware import (
    IdempotencyMiddleware,
    MaxBodySizeMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)

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

    # Resolve the telephony provider once, at boot, so a name with no implementation behind it is
    # a startup error rather than a permanent silence.
    #
    # `build_call_provider` returned the offline stub for EVERY input, so
    # `NEXUS_TELEPHONY_PROVIDER=twilio` behaved exactly like leaving it blank — and because
    # `get_call_provider()` has no callers anywhere in the product, the setting did nothing twice
    # over. An operator who set it, saw click-to-dial working and concluded Twilio was placing the
    # calls would never be corrected by anything in the system. Failing loudly here costs one clear
    # message on a config that could never have worked.
    from nexus.calling.provider import get_call_provider

    get_call_provider()

    # Runtime setting overrides, applied onto the live Settings object before anything reads one.
    # Non-fatal for the same reason as the seed below: a configuration read that fails must leave
    # the process running on the environment values it already had, not refuse to start.
    try:
        from nexus.runtime_config.service import apply_overrides

        applied = await apply_overrides()
        if applied:
            logger.info("runtime overrides applied: %s", sorted(applied))
    except Exception:
        logger.warning("could not apply runtime overrides at startup", exc_info=True)

    # Billing catalog/plan seed: idempotent, additive, and non-fatal. A seed failure must never
    # stop the API from serving (docs/billing/15-Migration-Strategy.md).
    if get_settings().billing_seed_on_startup:
        try:
            from nexus.billing.catalog import sync_catalog
            from nexus.billing.plans import sync_plans
            from nexus.billing.rates import sync_rates

            await sync_catalog()
            await sync_plans()
            await sync_rates()

            # Every tenant must hold a subscription before enforcement can ever be armed;
            # an un-subscribed tenant would fall through to catalog defaults and be mis-gated.
            # Idempotent and additive — a paying tenant is never touched.
            from nexus.billing.subscriptions import backfill_subscriptions

            await backfill_subscriptions()
        except Exception:
            logging.getLogger("nexus.main").warning(
                "billing seed sync failed; continuing without it", exc_info=True
            )

    yield
    await dispose_db()


def _maybe_enable_metrics(app: FastAPI) -> None:
    """Expose Prometheus ``/metrics``. On by default since M15.

    The instrumentator wraps every request, so a version mismatch between FastAPI and the
    instrumentator turns a metrics bug into a 500 on *every* endpoint — it did exactly that once,
    when an unpinned build pulled a FastAPI whose router objects it could not introspect and
    logins started 500ing. That is why this whole call is wrapped: an incompatible install
    degrades to "no metrics" rather than breaking the app, and pyproject now pins both sides.

    Under ``PROMETHEUS_MULTIPROC_DIR`` the instrumentator serves an aggregate of every uvicorn
    worker's mmap files. That variable is not optional in production: the app runs 2 workers, so
    without it a scrape hits one registry at random and reports roughly half the traffic. The
    domain counters in ``nexus/core/metrics.py`` ride the same mechanism. Gauges and custom
    collectors do NOT — they live in the worker (``nexus/workers/state_metrics.py``)."""
    if not get_settings().metrics_enabled:
        return
    try:
        _instrument(app)
    except Exception:  # ImportError, or an instrumentator/FastAPI incompatibility
        logging.getLogger("nexus.main").warning("metrics disabled: instrumentation failed", exc_info=True)


def _instrument(app: FastAPI) -> None:
    """The only line that can fail. Split out so the degradation path above is testable — the
    protection against the original incident is untested otherwise."""
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


def create_app() -> FastAPI:
    settings = get_settings()
    # The interactive docs and the raw schema are a complete map of every endpoint, parameter and
    # model. That is exactly what makes them useful in development and exactly what makes them a
    # reconnaissance gift in production — an audit found both served 200 unauthenticated against a
    # prod deployment. Off outside local/test; the OpenAPI spec is still generated in-process, so
    # the test client and any internal tooling that introspects `app.openapi()` are unaffected.
    _expose_docs = get_settings().env in ("local", "test")
    app = FastAPI(
        title="InfoJoy GTM",
        description="AI-powered Go-To-Market intelligence platform.",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if _expose_docs else None,
        redoc_url="/redoc" if _expose_docs else None,
        openapi_url="/openapi.json" if _expose_docs else None,
    )

    app.add_middleware(RequestContextMiddleware)

    if settings.max_request_body_bytes > 0:
        # Outermost guard: refuse oversized bodies before anything else touches them.
        app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_request_body_bytes)

    if settings.security_headers_enabled:
        # HSTS only outside local/test — never force HTTPS on a plain-HTTP localhost.
        app.add_middleware(
            SecurityHeadersMiddleware, enable_hsts=settings.env in ("staging", "prod")
        )

    if settings.idempotency_enabled:
        # Opt-in: de-duplicate mutating POSTs that carry an Idempotency-Key. Inert otherwise.
        app.add_middleware(IdempotencyMiddleware)

    if settings.cors_origin_list:
        # Origins are an explicit allowlist (never "*") because credentials are allowed. Methods
        # and headers are scoped to what the SPA actually sends rather than "*", to keep the
        # credentialed CORS surface as narrow as the contract requires.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            # Exactly the headers the client sends: bearer auth, JSON content-type, the request-id
            # correlation header, and Last-Event-ID (SSE stream resume for chat/run progress).
            allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Last-Event-ID"],
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

    @app.exception_handler(QuotaExceeded)
    async def _quota_exceeded(request: Request, exc: QuotaExceeded) -> JSONResponse:
        """402 Payment Required, carrying what the UI needs to render an upsell.

        A bare 500 teaches the customer nothing; the payload names the capability, the limit
        they hit, and where to upgrade.
        """
        return JSONResponse(status_code=402, content=exc.to_payload())

    @app.exception_handler(BillingThrottled)
    async def _billing_throttled(request: Request, exc: BillingThrottled) -> JSONResponse:
        """429 with Retry-After. A rate limit is not an upsell — the fix is to wait, not pay."""
        return JSONResponse(
            status_code=429,
            content={
                "error": "throttled",
                "capability": exc.capability_id,
                "retry_after_s": exc.retry_after_s,
            },
            headers={"Retry-After": str(exc.retry_after_s)},
        )

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
