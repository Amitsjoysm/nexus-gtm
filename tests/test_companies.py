"""Shared company records: domain normalisation, resolution, backfill.

The whole design rests on one claim — **the normalised domain is the identity, and nothing else is**.
Name-based resolution across tenants is how one workspace's data reaches another's, and this
subsystem has already shipped five wrong-attribution bugs by trusting a name match. So most of this
file is about what must NOT resolve.
"""
from __future__ import annotations

import pytest

from nexus.companies.resolution import company_id_for, normalise_domain
from tests.conftest import make_tenant, tenant_session


# ---- normalisation ------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("acme.com", "acme.com"),
    ("ACME.COM", "acme.com"),
    ("www.acme.com", "acme.com"),
    ("https://acme.com", "acme.com"),
    ("https://www.acme.com/careers?utm=x", "acme.com"),
    ("acme.com:8443", "acme.com"),
    ("  acme.com. ", "acme.com"),
    ("dana@acme.com", "acme.com"),          # an email passed where a domain was expected
    ("sub.acme.com", "sub.acme.com"),       # a subdomain IS a different host; do not guess
])
def test_domains_normalise_to_one_comparable_form(raw, expected):
    assert normalise_domain(raw) == expected


@pytest.mark.parametrize("raw", [
    "", None, "   ", "acme",               # a bare label is not a domain
    "not a domain.com",                     # spaces
    "gmail.com", "outlook.com",             # free mail: thousands of unrelated people
    "example.com", "test.com",              # reserved names that fill test data
    "bit.ly",                               # link shorteners
])
def test_things_that_are_not_a_company_resolve_to_nothing(raw):
    """Each of these would merge unrelated businesses into one shared record — the exact
    cross-tenant leak this table must never cause."""
    assert normalise_domain(raw) == ""


def test_the_id_is_deterministic():
    """Two workers racing on the same company must generate the same primary key: one insert wins
    and the other re-reads, instead of both succeeding and splitting the timeline in two."""
    assert company_id_for("acme.com") == company_id_for("acme.com")
    assert company_id_for("acme.com") != company_id_for("globex.com")


# ---- resolution ---------------------------------------------------------------------------------

async def _session():
    from nexus.core.db import get_sessionmaker

    return get_sessionmaker()()


async def test_resolving_creates_one_company_and_reuses_it():
    from nexus.companies.resolution import resolve_company

    async with await _session() as s:
        a = await resolve_company(s, domain="https://www.acme.com/", name="Acme Corp")
        await s.flush()
        b = await resolve_company(s, domain="acme.com", name="Acme")
        assert a is not None and b is not None
        assert a.id == b.id                 # the same real-world company
        assert a.domain == "acme.com"


async def test_an_account_with_no_domain_gets_no_company():
    """Not a gap to close later — it is the safety property. Name resolution across tenants is the
    leak this design exists to avoid."""
    from nexus.companies.resolution import resolve_company

    async with await _session() as s:
        assert await resolve_company(s, domain=None, name="Acme Corp") is None
        assert await resolve_company(s, domain="", name="Acme Corp") is None
        assert await resolve_company(s, domain="gmail.com", name="Acme Corp") is None


async def test_firmographics_only_fill_blanks():
    """A tenant's correction must never rewrite what every other tenant sees. Per-tenant overrides
    belong on `accounts`."""
    from nexus.companies.resolution import resolve_company

    async with await _session() as s:
        first = await resolve_company(s, domain="fill.com", name="Fill", industry="Fintech",
                                      employee_count=100)
        await s.flush()
        await resolve_company(s, domain="fill.com", name="Fill", industry="Healthcare",
                              employee_count=999)
        assert first.industry == "Fintech"      # not overwritten
        assert first.employee_count == 100


async def test_tech_stack_is_unioned_not_replaced():
    """One tenant seeing "Postgres" must not erase another's "Kafka"."""
    from nexus.companies.resolution import resolve_company

    async with await _session() as s:
        c = await resolve_company(s, domain="stack.com", name="Stack", tech_stack=["Kafka"])
        await s.flush()
        await resolve_company(s, domain="stack.com", name="Stack", tech_stack=["Postgres"])
        assert set(c.tech_stack) == {"Kafka", "Postgres"}


async def test_the_company_table_is_platform_global():
    """No tenant_id, so apply_rls.py leaves it alone. Enrolling it would make the shared crawler
    see zero rows — silent under RLS, not an error."""
    from nexus.models.company import Company, CompanySignal

    assert "tenant_id" not in Company.__table__.columns
    assert "tenant_id" not in CompanySignal.__table__.columns


# ---- backfill -----------------------------------------------------------------------------------

async def _account(tid: str, name: str, domain: str | None):
    from nexus.models.account import Account

    async with tenant_session(tid) as ts:
        acct = Account(tenant_id=tid, name=name, domain=domain)
        ts.add(acct)
        await ts.flush()
        return acct.id


async def _company_id_of(tid: str, account_id: str):
    from nexus.models.account import Account

    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == account_id)
        return acct.company_id


async def test_backfill_links_accounts_to_shared_companies():
    from nexus.companies.backfill import backfill_companies

    tid = await make_tenant(slug="co1")
    aid = await _account(tid, "Acme Corp", "acme.com")

    report = await backfill_companies()
    assert report.get("error") is None
    assert report["linked"] >= 1
    assert await _company_id_of(tid, aid)


async def test_two_tenants_tracking_one_company_share_a_row():
    """The entire point: forty workspaces tracking Stripe should crawl it once, not forty times."""
    from nexus.companies.backfill import backfill_companies

    a = await make_tenant(slug="co2a")
    b = await make_tenant(slug="co2b")
    a_id = await _account(a, "Shared Co", "https://www.sharedco.com")
    b_id = await _account(b, "SharedCo Inc", "sharedco.com")

    await backfill_companies()
    assert await _company_id_of(a, a_id) == await _company_id_of(b, b_id)


async def test_backfill_skips_accounts_with_no_domain():
    from nexus.companies.backfill import backfill_companies

    tid = await make_tenant(slug="co3")
    aid = await _account(tid, "No Domain Co", None)

    report = await backfill_companies()
    assert report["skipped_no_domain"] >= 1
    assert await _company_id_of(tid, aid) is None


async def test_backfill_is_idempotent():
    """It only ever fills a NULL, so a partial run followed by a full one equals one full run."""
    from nexus.companies.backfill import backfill_companies

    tid = await make_tenant(slug="co4")
    aid = await _account(tid, "Idem Co", "idemco.com")

    await backfill_companies()
    first = await _company_id_of(tid, aid)
    second_report = await backfill_companies()
    assert await _company_id_of(tid, aid) == first
    # Nothing left to do for this account on the second pass.
    assert second_report.get("error") is None


async def test_a_dry_run_changes_nothing():
    """An operator should be able to see the blast radius before authorising it."""
    from nexus.companies.backfill import backfill_companies

    tid = await make_tenant(slug="co5")
    aid = await _account(tid, "Dry Co", "dryco.com")

    report = await backfill_companies(dry_run=True)
    assert report["linked"] >= 1
    assert await _company_id_of(tid, aid) is None


async def test_backfill_is_bounded_per_call():
    """An unbounded update on the accounts table holds a lock for as long as it takes."""
    from nexus.companies.backfill import backfill_companies

    tid = await make_tenant(slug="co6")
    for i in range(4):
        await _account(tid, f"Bounded {i}", f"bounded{i}.com")

    report = await backfill_companies(limit=2)
    assert report["scanned"] == 2


async def test_the_backfill_never_raises(monkeypatch):
    """It runs as maintenance; a failure must not take down whatever scheduled it."""
    import nexus.core.db as core_db

    def boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(core_db, "get_platform_sessionmaker", boom)
    from nexus.companies.backfill import backfill_companies

    assert "error" in await backfill_companies()


async def test_existing_accounts_are_unaffected_until_linked():
    """The compatibility line: a null company_id must behave exactly as before the column existed."""
    from nexus.models.account import Account

    tid = await make_tenant(slug="co7")
    aid = await _account(tid, "Untouched Co", "untouched.com")
    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == aid)
        assert acct.company_id is None
        assert acct.name == "Untouched Co"      # everything else intact
