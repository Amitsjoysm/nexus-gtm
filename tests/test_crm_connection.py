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
