"""People the platform found are kept for the next workspace, and only those (2026-10-02).

The shared ``people`` store held phone lookups and nothing else: empty on the local deploy while
workspaces had paid LinkedIn for dozens of buyers, so a second workspace asking Find contacts about
the same company bought the same people again.

Decided with the product owner, "only what we found": people a LinkedIn employees run proved work at
the company, and addresses our finder found and a mailbox verifier proved. A customer's own upload
stays in its workspace. Writes go through a worker job, never the request's tenant transaction.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.models import Account, Contact
from nexus.models.person import Person
from tests.conftest import make_tenant, tenant_session
from tests.test_prospect_contacts import Actors, _account, _person, _source, web  # noqa: F401

VANTA_URL = "linkedin.com/in/acwright1"


@pytest.fixture
def queue(monkeypatch):
    from nexus.workers.queue import InMemoryTaskQueue

    q = InMemoryTaskQueue()
    monkeypatch.setattr("nexus.workers.queue._queue", q)
    return q


async def _drain(q) -> list[dict]:
    """Run every queued pool job, as the worker would."""
    from nexus.workers.tasks import HANDLERS

    reports = []
    while (job := await q.dequeue(timeout=0)) is not None:
        if job.name == "pool_found_people":
            reports.append(await HANDLERS[job.name](job.payload))
    return reports


async def _people() -> list[Person]:
    async with get_platform_sessionmaker()() as s:
        return list((await s.scalars(select(Person).order_by(Person.full_name))).all())


def _employees_runs(actors) -> int:
    return sum(1 for actor, _ in actors.runs if actor == "linkedin_company_employees")


# ---- what a LinkedIn run proves is pooled -------------------------------------------------------

async def test_every_proven_row_is_pooled_even_the_ones_this_workspace_did_not_take(web, queue):
    tid = await make_tenant("pool1")
    aid = await _account(tid)
    actors = Actors(people=[
        _person("ACwRight1", "Dana", "Reed", "VP of Sales"),
        _person("ACwRight2", "Lee", "Park", "Head of Sales"),
        _person("ACwWrong1", "Fleet", "Coordinator", "VP of Sales", company="Flex E Lease"),
    ])
    created = await _source(tid, aid, actors, limit=1)
    assert len(created) == 1
    assert queue._q.qsize() == 1                  # queued, not written inside the request
    assert await _people() == []

    await _drain(queue)
    people = await _people()
    assert [(p.full_name, p.title, p.company_domain, p.source) for p in people] == [
        ("Dana Reed", "VP of Sales", "vanta.com", "linkedin_employees"),
        ("Lee Park", "Head of Sales", "vanta.com", "linkedin_employees"),
    ]
    # A row that named another company proved nothing about this one.
    assert all("acwwrong1" not in (p.linkedin_url or "") for p in people)


async def test_the_next_workspace_gets_them_without_buying_a_run(web, queue):
    first = await make_tenant("pool2a")
    actors = Actors(people=[_person("ACwRight1", "Dana", "Reed", "VP of Sales")])
    await _source(first, await _account(first), actors, limit=1)
    await _drain(queue)

    second = await make_tenant("pool2b")
    aid = await _account(second, stored_page=False)
    again = Actors(people=[_person("ACwRight9", "Not", "Needed", "VP of Sales")])
    created = await _source(second, aid, again, limit=1)
    assert [name for name, _title, _url in created] == ["Dana Reed"]
    assert again.runs == []                       # nothing bought: no page lookup, no employees run
    async with tenant_session(second) as ts:
        contact = await ts.first(Contact, Contact.account_id == aid)
        assert contact.linkedin_url and VANTA_URL in contact.linkedin_url.lower()


async def test_a_short_pool_buys_only_the_rest(web, queue):
    first = await make_tenant("pool3a")
    await _source(first, await _account(first),
                  Actors(people=[_person("ACwRight1", "Dana", "Reed", "VP of Sales")]), limit=1)
    await _drain(queue)

    second = await make_tenant("pool3b")
    aid = await _account(second, stored_page=False)
    actors = Actors(people=[_person("ACwRight1", "Dana", "Reed", "VP of Sales"),
                            _person("ACwRight2", "Lee", "Park", "Head of Sales")])
    created = await _source(second, aid, actors, limit=2)
    assert sorted(name for name, _t, _u in created) == ["Dana Reed", "Lee Park"]
    assert _employees_runs(actors) == 1


async def test_the_workspaces_own_titles_still_decide(web, queue):
    first = await make_tenant("pool4a")
    await _source(first, await _account(first),
                  Actors(people=[_person("ACwRight1", "Dana", "Reed", "VP of Sales")]), limit=1)
    await _drain(queue)

    second = await make_tenant("pool4b")
    aid = await _account(second, titles=("Chief Financial Officer",), stored_page=False)
    actors = Actors(people=[])
    created = await _source(second, aid, actors, limit=1)
    assert all(name != "Dana Reed" for name, _t, _u in created)


async def test_someone_not_seen_on_linkedin_for_six_months_is_not_offered(web, queue):
    first = await make_tenant("pool5a")
    await _source(first, await _account(first),
                  Actors(people=[_person("ACwRight1", "Dana", "Reed", "VP of Sales")]), limit=1)
    await _drain(queue)
    async with get_platform_sessionmaker()() as s:
        for person in (await s.scalars(select(Person))).all():
            person.updated_at = utcnow() - timedelta(days=200)
        await s.commit()

    second = await make_tenant("pool5b")
    actors = Actors(people=[])
    await _source(second, await _account(second, stored_page=False), actors, limit=1)
    assert _employees_runs(actors) == 1            # asked LinkedIn: the pooled row was too old


async def test_a_person_seen_at_a_new_company_moves_with_them():
    from nexus.people.pool import pool_found_people

    fact = {"linkedin": "https://www.linkedin.com/in/mover", "name": "Mo Ver", "title": "VP Sales"}
    await pool_found_people("t-any", people=[{**fact, "domain": "old.io"}])
    await pool_found_people("t-any", people=[{**fact, "title": "CRO", "domain": "new.io"}])
    (person,) = await _people()
    assert (person.company_domain, person.title) == ("new.io", "CRO")


# ---- addresses: found and proven only -----------------------------------------------------------

async def _contact(tid, *, email="dana.reed@vanta.com", status="valid",
                   linkedin="https://www.linkedin.com/in/ACwRight1", name="Dana Reed"):
    async with tenant_session(tid) as ts:
        account = await ts.first(Account, Account.domain == "vanta.com")
        if account is None:
            account = Account(tenant_id=tid, name="Vanta", domain="vanta.com")
            ts.add(account)
            await ts.flush()
        contact = Contact(tenant_id=tid, account_id=account.id, full_name=name, title="VP Sales",
                          email=email, email_status=status, linkedin_url=linkedin)
        ts.add(contact)
        await ts.flush()
        return contact.id


async def test_a_proven_address_is_pooled_and_answers_only_for_that_company(monkeypatch):
    from nexus.people.pool import pool_found_people, pooled_email_for

    monkeypatch.setattr("nexus.enrichment.policy.verification_configured", lambda: True)
    tid = await make_tenant("mail1")
    cid = await _contact(tid)
    report = await pool_found_people(tid, verified=[cid])
    assert report["emails"] == 1
    (person,) = await _people()
    # The address is ours; the name and title were the workspace's, so they are not shared, and a
    # row made this way is never offered by Find contacts.
    assert person.full_name == "" and person.title is None and person.email_status == "valid"
    assert person.source != "linkedin_employees"

    url = "https://www.linkedin.com/in/ACwRight1"
    assert await pooled_email_for(url, domains={"vanta.com"}) == "dana.reed@vanta.com"
    # At another company that address is a previous employer's.
    assert await pooled_email_for(url, domains={"newco.io"}) is None


async def test_a_stub_valid_or_an_unproven_address_is_never_pooled(monkeypatch):
    from nexus.people.pool import pool_found_people

    tid = await make_tenant("mail2")
    cid = await _contact(tid)
    monkeypatch.setattr("nexus.enrichment.policy.verification_configured", lambda: False)
    assert (await pool_found_people(tid, verified=[cid]))["emails"] == 0

    monkeypatch.setattr("nexus.enrichment.policy.verification_configured", lambda: True)
    risky = await _contact(tid, email="d.reed@vanta.com", status="risky",
                           linkedin="https://www.linkedin.com/in/other")
    elsewhere = await _contact(tid, email="dana@gmail.com",
                               linkedin="https://www.linkedin.com/in/third")
    assert (await pool_found_people(tid, verified=[risky, elsewhere]))["emails"] == 0
    assert await _people() == []


async def test_the_waterfall_offers_a_pooled_address_without_a_verdict(monkeypatch):
    from nexus.enrichment.providers import SharedPeopleProvider
    from nexus.people.pool import pool_found_people

    monkeypatch.setattr("nexus.enrichment.policy.verification_configured", lambda: True)
    tid = await make_tenant("mail3")
    await pool_found_people(tid, verified=[await _contact(tid)])

    other = await make_tenant("mail4")
    cid = await _contact(other, email=None, status=None)
    async with tenant_session(other) as ts:
        contact = await ts.get(Contact, cid)
        account = await ts.get(Account, contact.account_id)
        result = await SharedPeopleProvider().enrich(account, contact)
    assert result.found and result.email == "dana.reed@vanta.com"
    # No verdict: the final pass verifies it live before the save policy can keep it.
    assert result.email_status is None and SharedPeopleProvider.costs_money is False


async def test_the_waterfall_queues_an_address_it_found_and_proved_but_not_one_supplied(queue):
    from nexus.enrichment.providers import EnrichmentProvider, EnrichmentResult
    from nexus.enrichment.waterfall import WaterfallEnricher

    class Proving(EnrichmentProvider):
        name = "finder"

        async def enrich(self, account, contact):
            return EnrichmentResult(found=True, email="dana.reed@vanta.com", email_confidence=0.95,
                                    email_status="valid", email_check="mailbox", source="finder")

    enricher = WaterfallEnricher([Proving()])
    tid = await make_tenant("mail5")
    found = await _contact(tid, email=None, status=None)
    supplied = await _contact(tid, linkedin="https://www.linkedin.com/in/supplied")
    async with tenant_session(tid) as ts:
        for cid in (found, supplied):
            contact = await ts.get(Contact, cid)
            await enricher.enrich_contact(ts, contact, await ts.get(Account, contact.account_id))
    jobs = []
    while (job := await queue.dequeue(timeout=0)) is not None:
        jobs.append(job)
    assert [j.payload.get("verified") for j in jobs if j.name == "pool_found_people"] == [[found]]
