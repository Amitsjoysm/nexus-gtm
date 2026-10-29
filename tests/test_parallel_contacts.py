"""Find contacts: pool, InfoJoy, LinkedIn, then Parallel people search, then web (2026-10-29).

One Entity Search request per account asks for the 100 people its price includes, and every person
must prove themselves here: the description names THIS company, the title read from it passes
`title_fits`, and the link is the person's own LinkedIn profile or a page on the company's own
site. Proven people with a profile go to the shared pool, under their own source, so the next
workspace does not pay again.
"""
from __future__ import annotations

from sqlalchemy import func, select

from nexus.core.db import get_platform_sessionmaker
from nexus.integrations.parallel import Entity, EntityOutcome
from nexus.models.billing import BillingUsageEvent
from tests.conftest import make_tenant, seed_relevance_profile, tenant_session
from tests.test_find_contacts_infojoy import ICP, _LinkedInMustNotBeRead

def _record(company, title, *, extra=""):
    """A person's description in the shape the first real answers had; synthetic values."""
    return (f"Connection Count: 500 | Follower Count: 900 | Company Name: {company} | "
            f"Title: {title} | Position: {company} {title}  2024-03-01 Houston, Texas{extra}")


PEOPLE = [
    Entity("Ana Example", "https://www.linkedin.com/in/ana-example",
           _record("Acme Ops", "Senior Operations Manager")),
    Entity("Ben Example", "https://www.linkedin.com/in/ben-example",
           _record("Other Corp", "Operations Manager",
                   extra=" | Experience: Acme Ops 2019-01-01 2023-01-01 Operations Manager")),
    Entity("Cy Example", "https://www.linkedin.com/in/cy-example",
           _record("Acme Ops", "Payroll Clerk")),
    Entity("Di Example", "https://news.example.com/2026/acme-hires",
           _record("Acme Ops", "Operations Manager")),
    # Prose, read for "<title>, <company>".
    Entity("Ed Example", "https://acme-ops.io/team/ed", "Ed Example, Operations Manager, Acme Ops"),
]


class FakePeople:
    def __init__(self, people=PEOPLE, status="ok"):
        self.people, self.status, self.searches = people, status, []

    async def entity_search(self, entity_type, objective, *, match_limit=100):
        self.searches.append((entity_type, objective, match_limit))
        if self.status != "ok":
            return EntityOutcome(self.status, detail="no")
        return EntityOutcome("ok", entities=list(self.people))


async def _account(tid, ts):
    from nexus.models.account import Account

    await seed_relevance_profile(ts, icp=ICP)
    account = Account(tenant_id=tid, name="Acme Ops", domain="acme-ops.io")
    ts.add(account)
    await ts.flush()
    return account


async def test_each_person_proves_they_work_here_in_a_buyer_title():
    from nexus.prospecting.parallel_contacts import parallel_people

    tid = await make_tenant("pp1")
    fake = FakePeople()
    async with tenant_session(tid) as ts:
        account = await _account(tid, ts)
        step = await parallel_people(ts, account, ICP, want=5, seen_urls=set(), seen_names=set(),
                                     client=fake)

    assert [(p.full_name, p.title, p.linkedin_url) for p in step.people] == [
        ("Ana Example", "Senior Operations Manager", "https://www.linkedin.com/in/ana-example"),
        ("Ed Example", "Operations Manager", "")]
    assert dict(step.discarded) == {"wrong_company": 1, "wrong_title": 1, "no_proof": 1}, \
        "Ben's record names another company: a past role at this one does not count"
    (kind, objective, limit), = fake.searches
    assert kind == "people" and limit == 100, "one request, the 100 people its price includes"
    assert "Acme Ops" in objective and "acme-ops.io" in objective
    assert "Operations Manager" in objective


async def test_a_position_given_as_json_or_a_headline_gives_the_title():
    from nexus.prospecting.parallel_contacts import parallel_people

    tid = await make_tenant("pp1b")
    people = [
        Entity("Fay Example", "https://linkedin.com/in/fay-example",
               'Connection Count: 80 | Follower Count: 90 | Company Name: Acme Ops | '
               'Position: {"company_name":"Acme Ops","title":"Operations Manager"}'),
        Entity("Gus Example", "https://linkedin.com/in/gus-example",
               "Headline: Plant Operations Manager at Acme Ops | Connection Count: 40"),
        Entity("Hal Example", "https://linkedin.com/in/hal-example",
               'Connection Count: 80 | Position: {"company_name":"Bolt Ops","title":'
               '"Operations Manager"}'),
    ]
    async with tenant_session(tid) as ts:
        account = await _account(tid, ts)
        step = await parallel_people(ts, account, ICP, want=5, seen_urls=set(), seen_names=set(),
                                     client=FakePeople(people))

    assert [(p.full_name, p.title) for p in step.people] == [
        ("Fay Example", "Operations Manager"), ("Gus Example", "Plant Operations Manager")]
    assert dict(step.discarded) == {"wrong_company": 1}


async def test_find_contacts_asks_parallel_after_linkedin_and_says_so(monkeypatch):
    from nexus.contacts.sourcing import find_contacts
    from nexus.people import pool

    pooled: list = []

    async def capture(tenant_id, *, people=(), verified=()):
        pooled.extend(people)

    monkeypatch.setattr(pool, "enqueue_pool", capture)
    tid = await make_tenant("pp2")
    async with tenant_session(tid) as ts:
        account = await _account(tid, ts)
        result = await find_contacts(ts, account, limit=5, linkedin_client=_LinkedInMustNotBeRead(),
                                     parallel_client=FakePeople())
        people = [(c.full_name, c.enrichment_source, dict(c.field_sources or {}))
                  for c in result.contacts]

    assert [(n, s) for n, s, _ in people] == [("Ana Example", "sourcing:parallel"),
                                              ("Ed Example", "sourcing:parallel")]
    assert people[0][2]["person"]["source"] == "parallel"
    assert people[0][2]["person"]["ref"] == "https://www.linkedin.com/in/ana-example"
    assert result.outcome["parallel"]["status"] == "found"
    assert "company research" in result.message
    assert [p["linkedin"] for p in pooled] == ["https://www.linkedin.com/in/ana-example"], \
        "only a person with a profile has an identity the pool can key on"
    assert pooled[0]["source"] == pool.SOURCE_PARALLEL

    async with get_platform_sessionmaker()() as s:
        cost = await s.scalar(select(func.sum(BillingUsageEvent.quantity)).where(
            BillingUsageEvent.tenant_id == tid,
            BillingUsageEvent.capability_id == "search.entity"))
    assert cost == 1


async def test_unkeyed_parallel_is_silent():
    from nexus.contacts.sourcing import find_contacts

    tid = await make_tenant("pp3")
    async with tenant_session(tid) as ts:
        account = await _account(tid, ts)
        result = await find_contacts(ts, account, limit=2, linkedin_client=_LinkedInMustNotBeRead(),
                                     parallel_client=FakePeople(status="not_configured"))
    assert result.outcome["parallel"]["status"] == "not_configured"
    assert "company research" not in result.message


async def test_pooled_parallel_people_are_offered_and_never_downgrade_a_linkedin_read():
    from nexus.lookalike.contacts import icp_title_targets
    from nexus.models.person import Person
    from nexus.people import pool

    tid = await make_tenant("pp4")
    await pool.pool_found_people(tid, people=[
        {"linkedin": "https://www.linkedin.com/in/ana-example", "name": "Ana Example",
         "title": "Senior Operations Manager", "domain": "acme-ops.io",
         "source": pool.SOURCE_PARALLEL},
        {"linkedin": "https://www.linkedin.com/in/lee-example", "name": "Lee Example",
         "title": "Operations Manager", "domain": "acme-ops.io"}])
    await pool.pool_found_people(tid, people=[
        {"linkedin": "https://www.linkedin.com/in/lee-example", "name": "Lee Example",
         "title": "Operations Manager", "domain": "acme-ops.io",
         "source": pool.SOURCE_PARALLEL}])

    offered = await pool.pooled_people_at("acme-ops.io", icp_title_targets(ICP),
                                          company_name="Acme Ops", limit=5)
    assert sorted(p.full_name for p in offered) == ["Ana Example", "Lee Example"]
    async with get_platform_sessionmaker()() as s:
        sources = dict((await s.execute(select(Person.full_name, Person.source))).all())
    assert sources == {"Ana Example": pool.SOURCE_PARALLEL, "Lee Example": pool.SOURCE_LINKEDIN}


async def test_a_title_is_a_buyer_title_in_meaning_not_by_one_shared_word():
    """Measured (2026-10-29): `title_fits` alone kept "Manager", "Manager - Tax" and "Revenue
    Manager" for an Operations / HSE Manager ICP. Abbreviations, plurals and the ways a safety role
    is written still count."""
    from nexus.prospecting.parallel_contacts import parallel_people

    tid = await make_tenant("pp5")
    titles = ["Manager", "Manager - Tax", "Revenue Manager", "Office Manager & Executive Assistant",
              "Operation Mgr", "EHS Manager", "Environment, Health and Safety Manager",
              "Senior Operations Manager, Permian"]
    people = [Entity(f"P{i} Example", f"https://linkedin.com/in/p{i}-example",
                     _record("Acme Ops", t)) for i, t in enumerate(titles)]
    icp = {"buyer_titles": ["Operations Manager", "HSE Manager"]}
    async with tenant_session(tid) as ts:
        account = await _account(tid, ts)
        step = await parallel_people(ts, account, icp, want=20, seen_urls=set(), seen_names=set(),
                                     client=FakePeople(people))

    assert [p.title for p in step.people] == titles[4:]
    assert dict(step.discarded) == {"wrong_title": 4}
