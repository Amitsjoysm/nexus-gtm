# tests/test_signal_preferences.py
"""Per-workspace control over which signal kinds are collected.

A tester asked why signals they had never enabled were appearing, and — the sharper question —
whether they were being billed for them. `signal_sources` is a **deployment-global** setting naming
which *collectors* run; there was no per-tenant control over what gets kept at all.

The absence of a row means enabled, exactly as `notification_preferences` works. Adding this table
must mute nobody: a preferences table that silences signals by existing is a regression for every
customer who never opens the screen.
"""
from __future__ import annotations

from sqlalchemy import select

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account
from nexus.models.identity import Tenant
from nexus.models.signal import SignalEvent


async def _ts(session, slug: str = "sig") -> TenantSession:
    tenant = Tenant(name=slug.upper(), slug=slug)
    session.add(tenant)
    await session.flush()
    return TenantSession(session, tenant.id)


def _raw(kind: str, key: str):
    from nexus.ingestion.sources import RawSignal

    return RawSignal(kind=kind, source="test", title=f"a {kind} signal", dedupe_key=key)


# ---- the compatibility line ------------------------------------------------------------------

async def test_no_preference_row_means_every_kind(fresh_db):
    """THE regression guard. Every existing workspace has no rows and must keep every signal."""
    from nexus.core.db import get_sessionmaker
    from nexus.ingestion.preferences import kind_enabled

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        for kind in ("funding", "hiring", "news", "tech_install", "website_change"):
            assert await kind_enabled(ts, kind) is True


async def test_an_unknown_kind_is_allowed(fresh_db):
    """Matches this codebase's standing bias that unknown resolves permissive. A kind added in a
    later release must not be silently dropped for every workspace until someone writes a row —
    that failure is invisible, because signals simply stop, which looks like a quiet market."""
    from nexus.core.db import get_sessionmaker
    from nexus.ingestion.preferences import kind_enabled

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        assert await kind_enabled(ts, "a_kind_invented_next_year") is True


async def test_ingestion_is_unchanged_when_nothing_is_configured(fresh_db):
    """The gate sits on the hot ingestion path, so this asserts it costs nothing by default."""
    from nexus.core.db import get_sessionmaker
    from nexus.ingestion.service import IngestionService

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        account = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(account)
        await ts.flush()
        created = await IngestionService().ingest(
            ts, account, [_raw("funding", "f1"), _raw("hiring", "h1"), _raw("news", "n1")]
        )
        assert len(created) == 3


# ---- switching a kind off ---------------------------------------------------------------------

async def test_disabling_one_kind_leaves_the_others(fresh_db):
    from nexus.core.db import get_sessionmaker
    from nexus.ingestion.preferences import kind_enabled, set_kind

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await set_kind(ts, "news", enabled=False)
        assert await kind_enabled(ts, "news") is False
        assert await kind_enabled(ts, "funding") is True


async def test_a_disabled_kind_is_not_persisted(fresh_db):
    """The point of the feature: a workspace is not charged to collect and store what it muted."""
    from nexus.core.db import get_sessionmaker
    from nexus.ingestion.preferences import set_kind
    from nexus.ingestion.service import IngestionService

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        account = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(account)
        await ts.flush()
        await set_kind(ts, "news", enabled=False)

        created = await IngestionService().ingest(
            ts, account, [_raw("funding", "f1"), _raw("news", "n1"), _raw("hiring", "h1")]
        )
        assert {c.kind for c in created} == {"funding", "hiring"}
        stored = (await s.scalars(select(SignalEvent))).all()
        assert "news" not in {r.kind for r in stored}


async def test_re_enabling_restores_it(fresh_db):
    from nexus.core.db import get_sessionmaker
    from nexus.ingestion.preferences import kind_enabled, set_kind

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await set_kind(ts, "news", enabled=False)
        await set_kind(ts, "news", enabled=True)
        assert await kind_enabled(ts, "news") is True


async def test_preferences_are_per_workspace(fresh_db):
    """One workspace muting news must not mute it for anybody else."""
    from nexus.core.db import get_sessionmaker
    from nexus.ingestion.preferences import kind_enabled, set_kind

    # Separate sessions per workspace: the tenancy guard tracks ONE active tenant per context, so
    # two TenantSessions sharing a session is a TenancyViolation by design — which is the guard
    # doing its job, not something to work around inside the product.
    async with get_sessionmaker()() as s:
        a = await _ts(s, "wsa")
        await set_kind(a, "news", enabled=False)
        assert await kind_enabled(a, "news") is False
        await s.commit()

    async with get_sessionmaker()() as s:
        b = await _ts(s, "wsb")
        assert await kind_enabled(b, "news") is True, "one workspace muted news for another"


# ---- the catalogue view -----------------------------------------------------------------------

async def test_the_listing_covers_every_known_kind(fresh_db):
    """Built from the catalogue and overlaid with rows, never from the rows alone — a screen
    listing only what somebody already toggled cannot be used to toggle anything the first time."""
    from nexus.core.db import get_sessionmaker
    from nexus.ingestion.preferences import current_preferences, set_kind
    from nexus.ingestion.service import SIGNAL_KINDS

    async with get_sessionmaker()() as s:
        ts = await _ts(s)
        await set_kind(ts, "news", enabled=False)
        prefs = await current_preferences(ts)
        assert set(prefs) == set(SIGNAL_KINDS), "every known kind must be listed"
        assert prefs["news"] is False
        assert all(v for k, v in prefs.items() if k != "news")


# ---- the endpoints ----------------------------------------------------------------------------

async def test_the_endpoints_round_trip(client, fresh_db):
    from tests.conftest import auth, signup

    token = await signup(client, slug="sigpref", email="a@sigpref.com", company="SigPref")

    listed = await client.get("/api/signals/preferences", headers=auth(token))
    assert listed.status_code == 200, listed.text
    assert all(row["enabled"] for row in listed.json()), "everything on by default"

    put = await client.put("/api/signals/preferences/news", headers=auth(token),
                           json={"enabled": False})
    assert put.status_code == 200, put.text
    assert put.json() == {"kind": "news", "enabled": False}

    again = await client.get("/api/signals/preferences", headers=auth(token))
    news = next(r for r in again.json() if r["kind"] == "news")
    assert news["enabled"] is False


async def test_an_unknown_kind_is_refused_not_stored(client, fresh_db):
    """A row for a kind nothing emits is invisible configuration that reads as active and never
    applies — the trap `runtime_config` avoids by skipping keys that left the catalogue."""
    from tests.conftest import auth, signup

    token = await signup(client, slug="sigpref2", email="a@sigpref2.com", company="SigPref2")
    r = await client.put("/api/signals/preferences/not_a_real_kind", headers=auth(token),
                         json={"enabled": False})
    assert r.status_code == 400
    assert "unknown signal kind" in r.text
