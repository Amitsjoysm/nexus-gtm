"""SDR adoption features: attribution, SLA aging, CRM trust fields, daily digest."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, signup


def test_migration_0011_chains_from_0010():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations" / "versions" / "0011_sdr_adoption.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0011", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0011_sdr_adoption"
    assert mod.down_revision == "0010_perf_indexes"


async def _seed_campaign(client, token) -> tuple[str, str]:
    """Account + saved list + campaign (drafts inline to the approval gate)."""
    acct = await client.post(
        "/api/accounts", headers=auth(token), json={"name": "Acme", "domain": "acme.sdr"}
    )
    assert acct.status_code == 201, acct.text
    lst = await client.post(
        "/api/lists", headers=auth(token), json={"name": "All", "filter": {}}
    )
    assert lst.status_code == 201, lst.text
    camp = await client.post(
        "/api/campaigns",
        headers=auth(token),
        json={"name": "Q3 push", "list_id": lst.json()["id"]},
    )
    assert camp.status_code == 201, camp.text
    return acct.json()["id"], camp.json()["id"]


@pytest.mark.asyncio
async def test_outcome_attributes_to_campaign_and_rolls_up(client):
    token = await signup(client, slug="attr", email="o@attr.x", company="AttrCo")
    account_id, campaign_id = await _seed_campaign(client, token)

    r = await client.post(
        "/api/outcomes",
        headers=auth(token),
        json={"stage": "replied", "account_id": account_id, "campaign_id": campaign_id},
    )
    assert r.status_code == 201, r.text
    assert r.json()["campaign_id"] == campaign_id
    r = await client.post(
        "/api/outcomes",
        headers=auth(token),
        json={"stage": "meeting", "account_id": account_id, "campaign_id": campaign_id},
    )
    assert r.status_code == 201, r.text

    detail = await client.get(f"/api/campaigns/{campaign_id}", headers=auth(token))
    assert detail.status_code == 200, detail.text
    assert detail.json()["outcomes"] == {"replied": 1, "meeting": 1}


@pytest.mark.asyncio
async def test_outcome_with_unknown_campaign_404s(client):
    token = await signup(client, slug="attr404", email="o@attr404.x", company="A404Co")
    r = await client.post(
        "/api/outcomes",
        headers=auth(token),
        json={"stage": "replied", "campaign_id": "nope"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_inbox_tasks_carry_sla_age(client):
    token = await signup(client, slug="sla", email="o@sla.x", company="SlaCo")
    acct = await client.post(
        "/api/accounts", headers=auth(token), json={"name": "Aged", "domain": "aged.sla"}
    )
    r = await client.post(f"/api/agents/pipeline/{acct.json()['id']}", headers=auth(token))
    assert r.status_code in (200, 201), r.text

    r = await client.get("/api/inbox", headers=auth(token))
    assert r.status_code == 200, r.text
    tasks = r.json()
    assert len(tasks) > 0
    for t in tasks:
        assert t["created_at"] is not None
        assert t["age_hours"] is not None and t["age_hours"] >= 0


@pytest.mark.asyncio
async def test_account_payload_carries_crm_trust_fields(client):
    token = await signup(client, slug="trust", email="o@trust.x", company="TrustCo")
    r = await client.post(
        "/api/integrations/crm/sync",
        headers=auth(token),
        json={"source": "salesforce", "accounts": [{"external_id": "sf1", "name": "SyncedCo"}]},
    )
    assert r.status_code == 200, r.text
    aid = r.json()["account_ids"][0]
    acct = (await client.get(f"/api/accounts/{aid}", headers=auth(token))).json()
    assert acct["crm_source"] == "salesforce"
    assert "crm_synced_at" in acct  # null until the first outbound push; key always present


@pytest.mark.asyncio
async def test_daily_digest_is_idempotent_per_interval(client, monkeypatch):
    from nexus.workers.tasks import handle_send_daily_digests

    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    token = await signup(client, slug="dig", email="o@dig.x", company="DigCo")
    await client.patch(
        "/api/workspace/automation", headers=auth(token), json={"automation_enabled": True}
    )
    acct = await client.post(
        "/api/accounts", headers=auth(token), json={"name": "Busy", "domain": "busy.dig"}
    )
    r = await client.post(f"/api/agents/pipeline/{acct.json()['id']}", headers=auth(token))
    assert r.status_code in (200, 201), r.text

    first = await handle_send_daily_digests({})
    assert first["digests"] == 1
    second = await handle_send_daily_digests({})
    assert second["digests"] == 0  # same interval: already sent

    alerts = (await client.get("/api/alerts", headers=auth(token))).json()
    digests = [a for a in alerts if a["source"] == "digest"]
    assert len(digests) == 1
    assert digests[0]["channel"] == "email"
    assert "signals" in digests[0]["meta"]


@pytest.mark.asyncio
async def test_digest_skips_quiet_and_opted_out_tenants(client, monkeypatch):
    from nexus.workers.tasks import handle_send_daily_digests

    monkeypatch.setattr(get_settings(), "automation_enabled", True)
    # Opted in but nothing happened: no digest.
    quiet = await signup(client, slug="quiet", email="o@quiet.x", company="QuietCo")
    await client.patch(
        "/api/workspace/automation", headers=auth(quiet), json={"automation_enabled": True}
    )
    # Active but NOT opted in: no digest either.
    out = await signup(client, slug="optout", email="o@optout.x", company="OptOutCo")
    acct = await client.post(
        "/api/accounts", headers=auth(out), json={"name": "A", "domain": "a.optout"}
    )
    await client.post(f"/api/agents/pipeline/{acct.json()['id']}", headers=auth(out))

    res = await handle_send_daily_digests({})
    assert res["digests"] == 0


@pytest.mark.asyncio
async def test_manual_crm_push_stamps_synced_at(client):
    """Regression: a rep's manual 'Sync to CRM' must update the trust chip — the manual
    push routes through the shared sync unit and stamps crm_synced_at like auto-sync."""
    token = await signup(client, slug="mpush", email="o@mpush.x", company="MpushCo")
    r = await client.post(
        "/api/integrations/crm/sync",
        headers=auth(token),
        json={"source": "salesforce", "accounts": [{"external_id": "m1", "name": "ManualCo"}]},
    )
    aid = r.json()["account_ids"][0]
    before = (await client.get(f"/api/accounts/{aid}", headers=auth(token))).json()
    assert before["crm_synced_at"] is None

    push = await client.post(f"/api/integrations/crm/push/{aid}", headers=auth(token))
    assert push.status_code == 200 and push.json()["ok"] is True, push.text

    after = (await client.get(f"/api/accounts/{aid}", headers=auth(token))).json()
    assert after["crm_synced_at"] is not None
