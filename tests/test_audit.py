"""The workspace audit trail: a readable table alongside the operator-facing log line.

The contract under test is that auditing is *additive* — it records what happened without ever
becoming a reason the recorded action fails, and without ever holding a secret.
"""
from __future__ import annotations

from tests.conftest import auth, make_tenant, signup, tenant_session


async def test_audit_row_is_written_and_scoped_to_the_tenant():
    from nexus.core.audit import record_audit
    from nexus.models.audit import AuditLog

    tid = await make_tenant(slug="aud", name="AUD")
    async with tenant_session(tid) as ts:
        await record_audit(ts, "crm.connection.set", actor_user_id="u-1",
                           target_type="crm_connection", target_id="c-1",
                           meta={"provider": "hubspot", "token_set": True})
        await ts.flush()

    async with tenant_session(tid) as ts:
        rows = await ts.list(AuditLog)
        assert len(rows) == 1
        assert rows[0].action == "crm.connection.set"
        assert rows[0].actor_user_id == "u-1"
        assert rows[0].target_type == "crm_connection"
        assert rows[0].meta["provider"] == "hubspot"
        assert rows[0].meta["token_set"] is True

    other = await make_tenant(slug="aud2", name="AUD2")
    async with tenant_session(other) as ts:
        assert await ts.list(AuditLog) == []


async def test_record_audit_also_emits_the_log_line(caplog):
    """The table is for the workspace admin; the line is for the operator. Both, not either."""
    from nexus.core.audit import record_audit

    tid = await make_tenant(slug="aud3", name="AUD3")
    with caplog.at_level("INFO", logger="nexus.audit"):
        async with tenant_session(tid) as ts:
            await record_audit(ts, "crm.connection.clear", actor_user_id="u-2")
            await ts.flush()
    assert any("action=crm.connection.clear" in r.getMessage() for r in caplog.records)


async def test_audit_write_never_breaks_the_action_it_records():
    """An audit failure must not roll back the thing being audited — losing a credential change
    to save its audit row is exactly backwards. An unserializable meta must not raise."""
    from nexus.core.audit import record_audit
    from nexus.models.audit import AuditLog

    tid = await make_tenant(slug="aud4", name="AUD4")
    async with tenant_session(tid) as ts:
        await record_audit(ts, "x.y", actor_user_id="u", meta={"bad": object()})
        await ts.flush()
        # It degraded rather than raising; whether a row landed is secondary to not exploding.
        rows = await ts.list(AuditLog)
        assert len(rows) <= 1


async def test_audit_meta_is_coerced_to_something_json_can_hold():
    from nexus.core.audit import record_audit
    from nexus.models.audit import AuditLog
    from datetime import datetime, timezone

    tid = await make_tenant(slug="aud5", name="AUD5")
    when = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    async with tenant_session(tid) as ts:
        await record_audit(ts, "x.y", meta={"when": when, "n": 3})
        await ts.flush()
        row = (await ts.list(AuditLog))[0]
        assert row.meta["n"] == 3
        assert "2026-08-21" in row.meta["when"]


async def test_crm_connection_changes_land_in_the_audit_table(client):
    """The four CRM endpoints are the first real callers."""
    h = auth(await signup(client))
    await client.put("/api/integrations/crm/connection", headers=h,
                     json={"provider": "hubspot", "access_token": "pat-audit-secret"})
    await client.delete("/api/integrations/crm/connection", headers=h)

    r = await client.get("/api/workspace/audit", headers=h)
    assert r.status_code == 200, r.text
    rows = r.json()
    actions = [x["action"] for x in rows]
    assert "crm.connection.set" in actions
    assert "crm.connection.clear" in actions
    # Newest first.
    assert actions.index("crm.connection.clear") < actions.index("crm.connection.set")
    # The secret is nowhere in the audit trail.
    assert "pat-audit-secret" not in r.text


async def test_audit_endpoint_is_admin_only(client):
    owner_h = auth(await signup(client, slug="ar", email="owner@ar.com", company="AR"))
    invite = await client.post("/api/workspace/members", headers=owner_h, json={
        "email": "rep@ar.com", "full_name": "Rep", "role": "rep", "password": "password123"})
    assert invite.status_code in (200, 201), invite.text
    login = await client.post("/api/auth/login",
                              json={"email": "rep@ar.com", "password": "password123"})
    rep_h = auth(login.json()["access_token"])
    assert (await client.get("/api/workspace/audit", headers=rep_h)).status_code == 403


async def test_audit_endpoint_filters_and_caps(client):
    h = auth(await signup(client, slug="af", email="o@af.com", company="AF"))
    await client.put("/api/integrations/crm/connection", headers=h,
                     json={"provider": "hubspot", "access_token": "pat-1"})
    await client.delete("/api/integrations/crm/connection", headers=h)

    only_set = (await client.get("/api/workspace/audit?action=crm.connection.set",
                                 headers=h)).json()
    assert only_set and all(x["action"] == "crm.connection.set" for x in only_set)

    capped = await client.get("/api/workspace/audit?limit=1", headers=h)
    assert len(capped.json()) == 1


async def test_one_tenant_cannot_read_anothers_audit_trail(client):
    h_a = auth(await signup(client, slug="ta1", email="a@ta1.com", company="TA1"))
    h_b = auth(await signup(client, slug="tb1", email="b@tb1.com", company="TB1"))
    await client.put("/api/integrations/crm/connection", headers=h_a,
                     json={"provider": "hubspot", "access_token": "pat-a-only"})

    rows_b = (await client.get("/api/workspace/audit", headers=h_b)).json()
    assert all(x["action"] != "crm.connection.set" for x in rows_b)
