"""Per-tenant CRM credentials: sealing, storage, resolution, endpoints, and the worker fix.

Offline throughout. The recurring theme: a stored token must never leave the server — several
tests assert against the *raw response text* rather than a parsed model, because a parsed model
can only prove the fields we thought to check.
"""
from __future__ import annotations

import pytest

from nexus.ingestion.crm_crypto import seal_crm_secret, unseal_crm_secret
from tests.conftest import auth, make_tenant, principal_from_token, signup, tenant_session


@pytest.fixture(autouse=True)
def no_connector_override():
    """Clear any installed connector before and after every test in this module.

    `tests/test_crm_push.py` tears down with `set_crm_connector(StubCRMConnector())` — a fresh
    stub, not None — so an override can still be installed process-wide when this module runs.
    Under resolution precedence an override wins over every tenant credential, which would make
    the fallback tests below pass vacuously against someone else's leftover stub.
    """
    from nexus.ingestion.crm import set_crm_connector

    set_crm_connector(None)
    yield
    set_crm_connector(None)


def test_seal_unseal_round_trip():
    blob = seal_crm_secret({"access_token": "pat-secret-123"})
    assert set(blob) == {"enc"}
    assert "pat-secret-123" not in blob["enc"]
    assert unseal_crm_secret(blob) == {"access_token": "pat-secret-123"}


def test_unseal_is_tolerant_of_garbage():
    """A corrupt or key-rotated blob means 'reconnect', never a 500."""
    for bad in (None, {}, {"enc": ""}, {"enc": "not-a-fernet-token"}, {"nope": "x"}):
        assert unseal_crm_secret(bad) == {}


def test_crm_secret_survives_a_multi_field_bundle():
    """The envelope holds a dict, not a bare string, so an OAuth token set (access + refresh +
    expiry) can be stored later without a migration."""
    bundle = {"access_token": "at-1", "refresh_token": "rt-1", "expires_at": 1234567890}
    assert unseal_crm_secret(seal_crm_secret(bundle)) == bundle


def test_audit_emits_one_structured_line(caplog):
    from nexus.core.audit import audit

    with caplog.at_level("INFO", logger="nexus.audit"):
        audit("crm.connection.set", tenant_id="t-1", actor="u-9",
              provider="hubspot", token_set=True)

    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    for fragment in ("action=crm.connection.set", "tenant=t-1", "actor=u-9",
                     "provider=hubspot", "token_set=true"):
        assert fragment in msg


def test_audit_omits_empty_actor_and_quotes_spaces():
    from nexus.core.audit import _format  # noqa: PLC2701 - unit-testing the formatter

    line = _format("crm.connection.test", "t-1", None, {"detail": "two words", "ok": False})
    assert "actor=" not in line
    assert 'detail="two words"' in line
    assert "ok=false" in line


async def test_crm_connection_is_tenant_scoped_and_stores_ciphertext():
    from nexus.models.integration import CrmConnection

    tid_a = await make_tenant(slug="ta", name="A")
    tid_b = await make_tenant(slug="tb", name="B")

    async with tenant_session(tid_a) as ts:
        ts.add(CrmConnection(tenant_id=tid_a, provider="hubspot",
                             secret=seal_crm_secret({"access_token": "pat-A"})))
        await ts.flush()

    async with tenant_session(tid_a) as ts:
        row = await ts.first(CrmConnection)
        assert row is not None
        assert row.provider == "hubspot"
        assert row.status == "unverified"
        assert row.api_base == ""
        assert unseal_crm_secret(row.secret) == {"access_token": "pat-A"}
        assert "pat-A" not in str(row.secret)

    async with tenant_session(tid_b) as ts:
        assert await ts.first(CrmConnection) is None


# ---- connector health + the globals split ------------------------------------------------
def _fixed_response(status: int, body: dict):
    """A stand-in for HubSpotConnector._request that always answers the same way."""

    async def _req(method: str, path: str, request_body: dict | None = None):
        return status, body

    return _req


async def test_stub_connector_test_connection_ok():
    from nexus.ingestion.crm import StubCRMConnector

    res = await StubCRMConnector().test_connection()
    assert res.ok is True
    assert res.label == "stub"


async def test_salesforce_test_connection_is_honest_about_not_being_live():
    from nexus.ingestion.crm import SalesforceConnector

    res = await SalesforceConnector().test_connection()
    assert res.ok is False
    assert "not available yet" in res.detail


async def test_hubspot_test_connection_maps_statuses():
    from nexus.ingestion.crm import HubSpotConnector

    conn = HubSpotConnector(access_token="pat-x")

    conn._request = _fixed_response(200, {"portalId": 12345678})  # type: ignore[method-assign]
    ok = await conn.test_connection()
    assert ok.ok is True and "12345678" in ok.label

    conn._request = _fixed_response(401, {})  # type: ignore[method-assign]
    assert "Invalid or expired" in (await conn.test_connection()).detail

    conn._request = _fixed_response(429, {})  # type: ignore[method-assign]
    assert "rate limit" in (await conn.test_connection()).detail

    conn._request = _fixed_response(500, {})  # type: ignore[method-assign]
    assert "HTTP 500" in (await conn.test_connection()).detail


async def test_hubspot_test_connection_without_a_token():
    from nexus.ingestion.crm import HubSpotConnector

    res = await HubSpotConnector(access_token="").test_connection()
    assert res.ok is False and "No access token" in res.detail


async def test_hubspot_test_connection_never_raises():
    """A flaky CRM is a failed result, never an exception across the boundary."""
    from nexus.ingestion.crm import HubSpotConnector

    conn = HubSpotConnector(access_token="pat-x")

    async def boom(method, path, body=None):
        raise RuntimeError("socket exploded")

    conn._request = boom  # type: ignore[method-assign]
    res = await conn.test_connection()
    assert res.ok is False
    assert "socket exploded" not in res.detail  # internals never surface


async def test_hubspot_falls_back_when_account_info_is_forbidden():
    """Private apps often lack the `oauth` scope account-info needs; the fallback uses the
    companies scope we already require for syncing."""
    from nexus.ingestion.crm import HubSpotConnector

    conn = HubSpotConnector(access_token="pat-x")
    calls: list[str] = []

    async def _req(method, path, body=None):
        calls.append(path)
        return (403, {}) if path.startswith("/account-info") else (200, {"results": []})

    conn._request = _req  # type: ignore[method-assign]
    res = await conn.test_connection()
    assert res.ok is True
    assert any(p.startswith("/crm/v3/objects/companies") for p in calls)


def test_override_is_distinguishable_from_the_memoized_env_connector():
    """The bug this guards: `_connector` used to hold both the test override and the memoized env
    instance, so 'is an override installed?' was unanswerable — and per-tenant resolution would
    skip tenant credentials on any env-configured deployment."""
    from nexus.ingestion.crm import (
        StubCRMConnector,
        get_crm_connector,
        get_crm_connector_override,
        set_crm_connector,
    )

    set_crm_connector(None)
    assert get_crm_connector_override() is None
    memoized = get_crm_connector()
    assert memoized is get_crm_connector()
    assert get_crm_connector_override() is None  # memoized, NOT an override

    installed = StubCRMConnector()
    set_crm_connector(installed)
    assert get_crm_connector_override() is installed
    assert get_crm_connector() is installed

    set_crm_connector(None)
    assert get_crm_connector_override() is None


# ---- per-tenant storage + resolution ------------------------------------------------------
async def _store(tid: str, token: str, provider: str = "hubspot", api_base: str = ""):
    from nexus.ingestion.crm_credentials import store_credentials

    async with tenant_session(tid) as ts:
        await store_credentials(ts, provider=provider, access_token=token, api_base=api_base)


async def test_resolve_falls_back_to_env_when_tenant_has_no_credential():
    """The non-regression core: an env-only deployment behaves exactly as before."""
    from nexus.ingestion.crm import get_crm_connector
    from nexus.ingestion.crm_credentials import resolve_crm_connector

    tid = await make_tenant(slug="tf", name="F")
    async with tenant_session(tid) as ts:
        assert await resolve_crm_connector(ts) is get_crm_connector()


async def test_stored_credential_beats_env():
    from nexus.ingestion.crm import HubSpotConnector
    from nexus.ingestion.crm_credentials import resolve_crm_connector

    tid = await make_tenant(slug="ts1", name="S")
    await _store(tid, "pat-tenant")
    async with tenant_session(tid) as ts:
        conn = await resolve_crm_connector(ts)
    assert isinstance(conn, HubSpotConnector)
    assert conn.source == "hubspot"


async def test_two_tenants_resolve_to_different_connectors():
    from nexus.ingestion.crm_credentials import resolve_crm_connector

    tid_a = await make_tenant(slug="ra", name="RA")
    tid_b = await make_tenant(slug="rb", name="RB")
    await _store(tid_a, "pat-AAA")
    await _store(tid_b, "pat-BBB")

    async with tenant_session(tid_a) as ts:
        ca = await resolve_crm_connector(ts)
    async with tenant_session(tid_b) as ts:
        cb = await resolve_crm_connector(ts)

    assert ca is not cb
    assert ca._token == "pat-AAA"
    assert cb._token == "pat-BBB"


async def test_installed_override_wins_over_a_stored_credential():
    from nexus.ingestion.crm import StubCRMConnector, set_crm_connector
    from nexus.ingestion.crm_credentials import resolve_crm_connector

    tid = await make_tenant(slug="ov", name="OV")
    await _store(tid, "pat-ignored")
    installed = StubCRMConnector()
    set_crm_connector(installed)
    async with tenant_session(tid) as ts:
        assert await resolve_crm_connector(ts) is installed


async def test_resolution_caches_the_instance_but_notices_a_changed_token():
    """The cache keeps recording buffers stable across pushes; the fingerprint stops it serving a
    credential another process has since replaced."""
    from nexus.ingestion.crm_credentials import resolve_crm_connector

    tid = await make_tenant(slug="cc", name="CC")
    await _store(tid, "pat-1")
    async with tenant_session(tid) as ts:
        first = await resolve_crm_connector(ts)
        assert await resolve_crm_connector(ts) is first  # buffers survive

    await _store(tid, "pat-2")
    async with tenant_session(tid) as ts:
        rebuilt = await resolve_crm_connector(ts)
    assert rebuilt is not first
    assert rebuilt._token == "pat-2"


async def test_blank_token_keeps_the_stored_secret():
    from nexus.ingestion.crm_credentials import get_connection, store_credentials

    tid = await make_tenant(slug="bk", name="BK")
    await _store(tid, "pat-keep")
    async with tenant_session(tid) as ts:
        await store_credentials(ts, provider="hubspot", access_token=None,
                                api_base="https://eu1.hubapi.com")
        row = await get_connection(ts)
        assert unseal_crm_secret(row.secret) == {"access_token": "pat-keep"}
        assert row.api_base == "https://eu1.hubapi.com"


async def test_clearing_a_credential_falls_back_to_env():
    from nexus.ingestion.crm import get_crm_connector
    from nexus.ingestion.crm_credentials import clear_credentials, resolve_crm_connector

    tid = await make_tenant(slug="cl", name="CL")
    await _store(tid, "pat-gone")
    async with tenant_session(tid) as ts:
        assert await clear_credentials(ts) is True
        assert await resolve_crm_connector(ts) is get_crm_connector()


async def test_undecryptable_secret_falls_back_instead_of_crashing():
    """A key rotation must degrade to 'reconnect', not a 500."""
    from nexus.ingestion.crm import get_crm_connector
    from nexus.ingestion.crm_credentials import has_credentials, resolve_crm_connector
    from nexus.models.integration import CrmConnection

    tid = await make_tenant(slug="rot", name="ROT")
    async with tenant_session(tid) as ts:
        ts.add(CrmConnection(tenant_id=tid, provider="hubspot",
                             secret={"enc": "garbage-from-an-old-key"}))
        await ts.flush()

    async with tenant_session(tid) as ts:
        row = await ts.first(CrmConnection)
        assert has_credentials(row) is False
        assert await resolve_crm_connector(ts) is get_crm_connector()
