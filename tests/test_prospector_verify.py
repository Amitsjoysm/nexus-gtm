"""Verify, one contact or a selection (decisions D19, D20).

Prospector keeps the provider's email unverified and holds it out of campaigns; Verify is how it is
released. Verifying an address Prospector delivered is free (the owner's correction); verifying any
other contact charges `verify.email`, as everywhere else. Up to 25 are checked inline; more go to a
durable background job. The save policy still decides what is kept: an address proven invalid is
removed and remembered; an unknown verdict keeps it, still held.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, principal_from_token, signup, tenant_session


@pytest.fixture
def verdicts(monkeypatch):
    """A verifier double: email -> status, defaulting to valid."""
    from nexus.verification import EmailVerification

    table: dict[str, str] = {}

    def fresh():
        async def verify(email: str):
            status = table.get(email, "valid")
            return EmailVerification(email=email, status=status,
                                     confidence=0.9 if status == "valid" else 0.3,
                                     source="reacher")
        return verify

    monkeypatch.setattr("nexus.enrichment.reverify.fresh_verify", fresh)
    return table


@pytest.fixture
def queued(monkeypatch):
    jobs: list[dict] = []

    async def fake(tenant_id, user_id, contact_ids, *, queue=None):
        jobs.append({"tenant_id": tenant_id, "user_id": user_id, "contact_ids": contact_ids})

    import nexus.workers.tasks as tasks

    monkeypatch.setattr(tasks, "enqueue_prospector_verify", fake)
    return jobs


async def _workspace(client, slug):
    token = await signup(client, slug=slug, email=f"owner@{slug}.io", company=slug.upper())
    return auth(token), principal_from_token(token).tenant_id


async def _people(tid: str, specs: list[tuple[str, str | None]]) -> list[str]:
    from nexus.models.account import Account, Contact

    async with tenant_session(tid) as ts:
        a = Account(name="Acme Example Inc", domain="acme-example.com")
        ts.add(a)
        await ts.flush()
        ids = []
        for email, source in specs:
            c = Contact(account_id=a.id, full_name=email.split("@")[0], email=email,
                        enrichment_source=source)
            ts.add(c)
            await ts.flush()
            ids.append(c.id)
        return ids


async def _verify_usage(tid: str) -> float:
    from nexus.models.billing import BillingUsageEvent

    async with tenant_session(tid) as ts:
        events = await ts.list(BillingUsageEvent, BillingUsageEvent.capability_id == "verify.email")
    return sum(float(e.quantity) for e in events)


async def test_prospector_addresses_verify_free_and_are_released(client, verdicts):
    from nexus.models.account import Contact
    from nexus.prospector.hold import held_until_verified

    h, tid = await _workspace(client, "pv1")
    ids = await _people(tid, [("a@acme-example.com", "sourcing:prospector"),
                              ("b@acme-example.com", "sourcing:prospector")])
    r = await client.post("/api/prospector/verify", headers=h, json={"contact_ids": ids})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "inline" and body["charged"] == 0
    assert {x["status"] for x in body["results"]} == {"valid"}
    async with tenant_session(tid) as ts:
        assert not any(held_until_verified(c) for c in await ts.list(Contact))
    assert await _verify_usage(tid) == 0


async def test_any_other_contact_is_charged_one_check(client, verdicts):
    h, tid = await _workspace(client, "pv2")
    ids = await _people(tid, [("c@acme-example.com", "crm:hubspot"),
                              ("d@acme-example.com", "sourcing:prospector")])
    body = (await client.post("/api/prospector/verify", headers=h,
                              json={"contact_ids": ids})).json()
    assert body["charged"] == 1
    assert await _verify_usage(tid) == 1


async def test_invalid_removes_the_address_and_unknown_keeps_it(client, verdicts):
    from nexus.models.account import Contact

    verdicts["dead@acme-example.com"] = "invalid"
    verdicts["maybe@acme-example.com"] = "unknown"
    h, tid = await _workspace(client, "pv3")
    ids = await _people(tid, [("dead@acme-example.com", "sourcing:prospector"),
                              ("maybe@acme-example.com", "sourcing:prospector")])
    body = (await client.post("/api/prospector/verify", headers=h,
                              json={"contact_ids": ids})).json()

    results = {x["contact_id"]: x for x in body["results"]}
    assert results[ids[0]] == {"contact_id": ids[0], "status": "invalid", "email_kept": False}
    assert results[ids[1]] == {"contact_id": ids[1], "status": "unknown", "email_kept": True}
    async with tenant_session(tid) as ts:
        dead = await ts.get(Contact, ids[0])
        maybe = await ts.get(Contact, ids[1])
    assert dead.email is None
    assert "dead@acme-example.com" in dead.custom_fields.get("rejected_emails", [])
    assert maybe.email == "maybe@acme-example.com" and maybe.email_status == "unknown"


async def test_more_than_25_go_to_a_background_job(client, verdicts, queued):
    h, tid = await _workspace(client, "pv4")
    ids = await _people(tid, [(f"p{i}@acme-example.com", "sourcing:prospector")
                              for i in range(26)])
    body = (await client.post("/api/prospector/verify", headers=h,
                              json={"contact_ids": ids})).json()
    assert body == {"mode": "queued", "count": 26, "charged": 0, "results": []}
    assert len(queued) == 1 and sorted(queued[0]["contact_ids"]) == sorted(ids)


async def test_the_job_verifies_and_charges_what_it_checked(verdicts):
    from nexus.models.account import Contact
    from nexus.workers.tasks import handle_prospector_verify_contacts
    from tests.conftest import make_tenant

    tid = await make_tenant("pv5")
    ids = await _people(tid, [("e@acme-example.com", "sourcing:prospector"),
                              ("f@acme-example.com", None)])
    out = await handle_prospector_verify_contacts(
        {"tenant_id": tid, "user_id": None, "contact_ids": ids})
    assert out["checked"] == 2 and out["charged"] == 1
    async with tenant_session(tid) as ts:
        assert {c.email_status for c in await ts.list(Contact)} == {"valid"}


async def test_another_workspaces_contacts_are_ignored(client, verdicts):
    _h1, t1 = await _workspace(client, "pv6a")
    ids = await _people(t1, [("g@acme-example.com", "sourcing:prospector")])
    h2, _ = await _workspace(client, "pv6b")
    body = (await client.post("/api/prospector/verify", headers=h2,
                              json={"contact_ids": ids})).json()
    assert body["results"] == [] and body["charged"] == 0
