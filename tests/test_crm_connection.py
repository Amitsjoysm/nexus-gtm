"""Per-tenant CRM credentials: sealing, storage, resolution, endpoints, and the worker fix.

Offline throughout. The recurring theme: a stored token must never leave the server — several
tests assert against the *raw response text* rather than a parsed model, because a parsed model
can only prove the fields we thought to check.
"""
from __future__ import annotations

import pytest

from nexus.ingestion.crm_crypto import seal_crm_secret, unseal_crm_secret
from tests.conftest import auth, make_tenant, principal_from_token, signup, tenant_session


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
