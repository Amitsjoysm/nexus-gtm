"""'Add to cadence' from a discovery selection: build an ad-hoc list + cadence + gated campaign
in one call. Offline/deterministic (stub LLM drafts; nothing sends without approval)."""
from __future__ import annotations

from nexus.core.security import decode_access_token
from nexus.models.account import Account, Contact
from nexus.models.cadence import Cadence
from nexus.models.campaign import Campaign, CampaignTarget
from tests.conftest import auth, signup, tenant_session


async def _seed_accounts(ts, names: list[str]) -> list[str]:
    ids = []
    for n in names:
        acc = Account(tenant_id=ts.tenant_id, name=n, domain=f"{n.lower()}.com")
        ts.add(acc)
        await ts.flush()
        ids.append(acc.id)
    return ids


async def test_launch_new_cadence_from_account_selection(client):
    token = await signup(client, slug="lf1", email="o@lf1.com", company="LF1")
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        acc_ids = await _seed_accounts(ts, ["Northwind", "Globex"])

    resp = await client.post(
        "/api/campaigns/launch-from-selection",
        json={"name": "Logistics Q3", "account_ids": acc_ids, "mode": "new_cadence"},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "awaiting_approval"  # held at the gate; nothing sent

    async with tenant_session(tid) as ts:
        # A fresh cadence was auto-built (default 3-touch), and a target per account exists.
        cadences = await ts.list(Cadence)
        assert len(cadences) == 1
        camp = (await ts.list(Campaign))[0]
        assert camp.cadence_id == cadences[0].id
        targets = await ts.list(CampaignTarget, CampaignTarget.campaign_id == camp.id)
        assert {t.account_id for t in targets} == set(acc_ids)


async def test_launch_existing_cadence_from_contact_selection(client):
    token = await signup(client, slug="lf2", email="o@lf2.com", company="LF2")
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        (acc_id,) = await _seed_accounts(ts, ["Initech"])
        contact = Contact(tenant_id=ts.tenant_id, account_id=acc_id, full_name="Jane Buyer",
                          title="VP Sales")
        ts.add(contact)
        await ts.flush()
        contact_id = contact.id

    # Build an existing cadence to enroll into.
    cad = await client.post(
        "/api/cadences",
        json={"name": "Evergreen", "steps": [{"channel": "email", "delay_days": 0, "angle": "intro"}]},
        headers=auth(token),
    )
    assert cad.status_code == 201, cad.text
    cadence_id = cad.json()["id"]

    resp = await client.post(
        "/api/campaigns/launch-from-selection",
        json={"name": "Existing run", "contact_ids": [contact_id],
              "mode": "existing_cadence", "cadence_id": cadence_id},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text

    async with tenant_session(tid) as ts:
        # No NEW cadence created; the contact's account became the target.
        assert len(await ts.list(Cadence)) == 1
        camp = (await ts.list(Campaign))[0]
        assert camp.cadence_id == cadence_id
        targets = await ts.list(CampaignTarget, CampaignTarget.campaign_id == camp.id)
        assert [t.account_id for t in targets] == [acc_id]


async def test_launch_empty_selection_is_400(client):
    token = await signup(client, slug="lf3", email="o@lf3.com", company="LF3")
    resp = await client.post(
        "/api/campaigns/launch-from-selection",
        json={"name": "Empty", "account_ids": [], "mode": "new_cadence"},
        headers=auth(token),
    )
    assert resp.status_code == 400, resp.text


async def test_launch_existing_cadence_missing_id_is_404(client):
    token = await signup(client, slug="lf4", email="o@lf4.com", company="LF4")
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        (acc_id,) = await _seed_accounts(ts, ["Acme"])
    resp = await client.post(
        "/api/campaigns/launch-from-selection",
        json={"name": "x", "account_ids": [acc_id], "mode": "existing_cadence",
              "cadence_id": "does-not-exist"},
        headers=auth(token),
    )
    assert resp.status_code == 404, resp.text
