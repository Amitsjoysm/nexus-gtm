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


async def make_tenant(slug: str = "t1", name: str = "Tenant One") -> str:
    """Create a tenant via a raw session and return its id."""
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    async with get_sessionmaker()() as s:
        t = Tenant(name=name, slug=slug)
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
