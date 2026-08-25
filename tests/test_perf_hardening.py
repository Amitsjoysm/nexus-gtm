"""Performance hardening: bounded connector buffers, single-round-trip overview."""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup

# `test_migration_0010_chains_from_0009` lived here; revisions 0001-0020 are now squashed into
# the frozen `0020_baseline_schema`. tests/test_migrations_replay.py supersedes it by replaying
# the whole chain onto an empty database and diffing the result against Base.metadata.


@pytest.mark.asyncio
async def test_crm_connector_recording_buffers_are_bounded():
    """The connector is a long-lived singleton; its push history must not grow unbounded."""
    from nexus.ingestion.crm import StubCRMConnector
    from nexus.models.account import Account

    conn = StubCRMConnector()
    conn.MAX_RECORDED_PUSHES = 25  # small cap to keep the test fast
    acct = Account(tenant_id="t", name="A", domain="a.x")
    for _ in range(60):
        await conn.push_account(acct)
        await conn.push_activity(account_id="x", kind="signal")
    assert len(conn.pushed_accounts) == 25
    assert len(conn.pushed_activities) == 25
    # The retained window is the most recent pushes, list semantics intact.
    assert isinstance(conn.pushed_accounts, list)


@pytest.mark.asyncio
async def test_sep_connector_recording_buffer_is_bounded():
    from nexus.integrations.sep import StubSEPConnector

    conn = StubSEPConnector()
    conn.MAX_RECORDED_PUSHES = 25
    for i in range(60):
        await conn.push_contact(sequence="default", email=f"u{i}@x.y", payload={})
    assert len(conn.pushed) == 25
    assert conn.pushed[-1]["email"] == "u59@x.y"  # newest retained, oldest evicted


@pytest.mark.asyncio
async def test_overview_counts_are_correct_after_single_query_rewrite(client):
    """Functional contract for the one-round-trip overview: same keys, correct values."""
    token = await signup(client, slug="ovw", email="o@ovw.x", company="OvwCo")
    r = await client.post(
        "/api/accounts", headers=auth(token), json={"name": "Acme", "domain": "acme.ovw"}
    )
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    r = await client.post(f"/api/agents/pipeline/{aid}", headers=auth(token))
    assert r.status_code in (200, 201), r.text

    r = await client.get("/api/analytics/overview", headers=auth(token))
    assert r.status_code == 200, r.text
    data = r.json()
    assert set(data) == {
        "accounts", "contacts", "signals", "open_tasks",
        "agent_actions", "agent_failures", "plays_executed", "avg_composite_score",
    }
    assert data["accounts"] == 1
    assert data["signals"] > 0          # pipeline ingested demo signals
    # Individual agent EXECUTIONS, not orchestrator sessions — the two are different
    # tables and the tile used to be labelled as though they were the same.
    assert data["agent_actions"] >= 1   # scoring agent ran
    assert data["agent_failures"] == 0
    assert data["avg_composite_score"] > 0


@pytest.mark.asyncio
async def test_overview_empty_workspace_is_all_zeros(client):
    token = await signup(client, slug="ovw0", email="o@ovw0.x", company="Ovw0Co")
    r = await client.get("/api/analytics/overview", headers=auth(token))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["accounts"] == 0
    assert data["avg_composite_score"] == 0.0
