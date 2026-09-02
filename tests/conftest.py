"""Shared test fixtures. Everything runs offline: SQLite + stub LLM + demo signals."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import pytest
import pytest_asyncio

# Windows-only: use the Selector event loop, not the default Proactor loop. The Proactor loop's
# IOCP poller (``_poll``/``GetQueuedCompletionStatus``) can wedge a fresh loop on the first
# in-process httpx-ASGI test and block forever — the exact cause of the suite "hanging" locally on
# Windows. Linux/CI uses the selector loop already, so this just makes the dev box match prod. Must
# run at import time, before pytest-asyncio creates any loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Point the DB at a temp file and force test env BEFORE importing app config (lru_cache).
_TMPDIR = tempfile.mkdtemp(prefix="nexus_test_")
# Per-xdist-worker DB file so `pytest -n auto` is safe: each worker is its own process that drops
# and recreates every table before each test (see ``fresh_db``), so a shared SQLite file would let
# one worker wipe another's tables mid-test. ``PYTEST_XDIST_WORKER`` is unset on a serial run →
# a single "test_main.db"; under xdist each worker gets "test_gw0.db", "test_gw1.db", ….
_WORKER_ID = os.environ.get("PYTEST_XDIST_WORKER", "main")
os.environ["NEXUS_DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMPDIR}/test_{_WORKER_ID}.db"
os.environ["NEXUS_ENV"] = "test"
os.environ["NEXUS_LLM_PROVIDER"] = "stub"
# Hermetic offline guarantee: pin the web-search backend to keyless DuckDuckGo (which, in tests,
# wraps the injected FakeBrowser) so a developer's local .env — e.g. NEXUS_SEARCH_PROVIDER=exa
# with a real key — can never make the suite reach the network. Env vars outrank the .env file.
os.environ["NEXUS_SEARCH_PROVIDER"] = "duckduckgo"
# Same reasoning for the hosted-engine API keys: blank them so a real key in a developer's local
# .env can never be picked up by ``get_settings()`` and turn a "keyless -> DuckDuckGo fallback"
# assertion into a live ExaSearchProvider. An empty env var outranks the .env value.
os.environ["NEXUS_EXA_API_KEY"] = ""
os.environ["NEXUS_EXA_API_KEYS"] = ""
os.environ["NEXUS_BRAVE_API_KEY"] = ""
os.environ["NEXUS_SERPER_API_KEY"] = ""
os.environ["NEXUS_GROQ_API_KEY"] = ""
os.environ["NEXUS_GROQ_API_KEYS"] = ""
# Pin the offline defaults for the provider/automation switches too: a developer's local .env
# (which enables the live app — real contact search, web enrichment, the automation heartbeat and
# daily ICP discovery) must never flip the suite's assumed-default behaviour. These assert the
# shipped defaults, so tests that check "default is stub / automation off" stay deterministic.
os.environ["NEXUS_CONTACT_SEARCH_SOURCES"] = "stub"
os.environ["NEXUS_ACCOUNT_ENRICH_ENABLED"] = "false"
os.environ["NEXUS_AUTOMATION_ENABLED"] = "false"
os.environ["NEXUS_ICP_DISCOVERY_ENABLED"] = "false"

from nexus.core.db import Base, get_engine  # noqa: E402
from nexus.core.tenancy import TenantSession  # noqa: E402
import nexus.models  # noqa: E402,F401  (register mappers)
from nexus.workers.tasks import tenant_session  # noqa: E402


class FakeBrowser:
    """A deterministic browser provider so enrichment/ingestion never hit the network."""

    def __init__(self, results: list[dict] | None = None):
        self.results = results or []

    async def search(self, query: str, *, limit: int = 5) -> list[dict]:
        return self.results[:limit]

    async def aclose(self) -> None:
        return None


@pytest_asyncio.fixture(autouse=True)
async def fresh_db():
    """Recreate all tables before each test for isolation."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture(autouse=True)
def offline_services():
    """Demo-only ingestion + reset the agent runtime so the stub LLM is used."""
    from nexus.ingestion.service import IngestionService, set_ingestion_service
    from nexus.ingestion.sources import DemoSignalSource
    from nexus.agents.runtime import reset_agent_runtime

    set_ingestion_service(IngestionService(sources=[DemoSignalSource()]))
    reset_agent_runtime()
    yield
    set_ingestion_service(IngestionService(sources=[DemoSignalSource()]))
    reset_agent_runtime()


async def make_tenant(
    slug: str = "t1", name: str = "Tenant One", *, pre_billing: bool = False
) -> str:
    """Create a tenant via a raw session and return its id.

    ``pre_billing=True`` backdates it to before the earliest ``billing_plans`` row, which is what
    `backfill_subscriptions` uses to tell a genuine legacy tenant from one created under billing.
    Tests that exercise the BACKFILL need this: a tenant made after the plans were seeded is, quite
    correctly, no longer claimed by it.
    """
    from datetime import timedelta

    from nexus.core.db import get_sessionmaker, utcnow
    from nexus.models.identity import Tenant

    async with get_sessionmaker()() as s:
        t = Tenant(name=name, slug=slug)
        if pre_billing:
            from sqlalchemy import select

            from nexus.models.billing import BillingPlan

            earliest = (await s.scalars(select(BillingPlan.created_at))).first()
            t.created_at = (earliest or utcnow()) - timedelta(days=365)
        s.add(t)
        await s.commit()
        return t.id


@pytest_asyncio.fixture
async def client():
    """An ASGI httpx client bound to a fresh app (offline)."""
    import httpx

    from nexus.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def signup(client, *, slug="acme", email="rep@acme.com", company="Acme") -> str:
    """Provision a tenant + owner and return the access token."""
    r = await client.post(
        "/api/auth/signup",
        json={
            "company_name": company,
            "company_slug": slug,
            "full_name": "Rep",
            "email": email,
            "password": "password123",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def principal_from_token(token: str):
    """Decode a JWT into a Principal exactly as ``get_principal`` does (claims sub/tid/role)."""
    from nexus.api.deps import Principal
    from nexus.core.security import decode_access_token

    payload = decode_access_token(token) or {}
    return Principal(
        user_id=payload["sub"],
        tenant_id=payload["tid"],
        role=payload.get("role", "rep"),
    )


__all__ = [
    "FakeBrowser",
    "make_tenant",
    "tenant_session",
    "TenantSession",
    "client",
    "signup",
    "auth",
    "principal_from_token",
]


@pytest.fixture(autouse=True)
def _reset_auth_rate_limit():
    """Clear the in-process rate-limit window between tests.

    The limiter keeps a module-level dict of (bucket, ip) -> hit timestamps. Without this, one
    chatty test can push a later test over the threshold and produce a failure that has nothing
    to do with the code under test.
    """
    from nexus.core import ratelimit

    ratelimit._HITS.clear()
    yield
    ratelimit._HITS.clear()


@pytest.fixture
def no_auth_rate_limit(monkeypatch):
    """Opt out of auth rate limiting for a test that legitimately makes many rapid auth calls.

    Rate limiting is ON by default in production (M13) — an unthrottled login endpoint is a
    credential-stuffing target. A chatty test is not a reason to weaken that default for
    everyone, so such tests declare the exemption explicitly:

        pytestmark = pytest.mark.usefixtures("no_auth_rate_limit")
    """
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "auth_rate_limit_enabled", False)


async def seed_relevance_profile(ts, **overrides):
    """Give a test tenant something to sell.

    The copy agents (`messaging`, `call_script`) REFUSE to run for a workspace with no
    `product_context` and no `value_props` — see RelevanceContext.is_configured. Without that
    guard they pitched an unnamed product and, given a `hiring` signal, generated a job
    application; measured in production 2026-08-31.

    So any test that exercises drafting or sending needs a configured profile, exactly as a real
    workspace does. This is deliberately NOT folded into `make_tenant`: `RelevanceProfile.tenant_id`
    is UNIQUE, and several suites (test_icp_auto_discovery, test_outcomes) add their own row, so a
    profile created for all 399 `make_tenant` call sites would collide with them.
    """
    from nexus.models.relevance import RelevanceProfile

    fields = {
        "icp": {"industries": ["Software"], "employee_min": 50, "employee_max": 5000},
        "value_props": [
            {"name": "Faster GTM", "description": "Signal to action in one move.",
             "pains_solved": ["slow pipeline generation"]}
        ],
        "product_context": "A GTM intelligence platform for B2B revenue teams.",
    }
    fields.update(overrides)
    ts.add(RelevanceProfile(tenant_id=ts.tenant_id, **fields))
    await ts.flush()


def assert_staff_surface_hidden(response) -> None:
    """A non-staff caller must not be able to tell an admin route from a missing one.

    The status is 404, not 403, and that is the point: a 403 answers the attacker's question.
    Enumerating `/api/admin/...` against a deployment that returns 403 for real paths and 404 for
    invented ones yields a complete map of the staff surface — provider keys, payment credentials,
    the runtime panel — with no valid credential at all.

    401 is accepted for an anonymous caller: "authenticate" is a statement about the CALLER, not
    about which routes exist, so it leaks nothing an attacker could enumerate with.

    A platform admin who merely lacks one permission still gets 403 — they have proven who they
    are, already know the surface exists, and a 404 would turn an authorisation problem into a hunt
    for a missing deployment.
    """
    assert response.status_code in (401, 404), (
        f"expected the staff surface to be hidden, got {response.status_code}: "
        f"a 403 confirms the route exists and lets it be enumerated"
    )


async def put_on_plan(tenant_id: str, plan_id: str, *, status: str = "active"):
    """Move a tenant onto ``plan_id``, SWITCHING its subscription rather than adding one.

    Tests used to do `ts.add(BillingSubscription(plan_id=...))` after signing up. That worked only
    while signup created no subscription; now that a new workspace starts on `free`, adding a
    second row leaves TWO live subscriptions — and "one subscription per tenant" is what makes
    rating and entitlement resolution unambiguous in the first place. `change_plan` exists to
    switch in place for exactly this reason, and the admin endpoints already use it.
    """
    from nexus.billing.subscriptions import change_plan, ensure_subscription
    from nexus.workers.tasks import tenant_session

    async with tenant_session(tenant_id) as ts:
        created = await ensure_subscription(ts, plan_id=plan_id, status=status)
        sub = created if created is not None else await change_plan(ts, plan_id, actor="test")
        if status != "active":
            sub.status = status
        await ts.flush()
        return sub
