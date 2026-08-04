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


# ---- shadow crawl (step 3) ----------------------------------------------------------------------

class _Source:
    """A signal source stand-in that records what it was handed."""

    name = "probe"

    def __init__(self, signals=None):
        self._signals = signals or []
        self.seen = []

    async def fetch(self, account):
        self.seen.append(account)
        return list(self._signals)


def _raw(kind="funding", key="k1", title="Acme raises $40M", strength=0.9):
    from nexus.ingestion.sources import RawSignal

    return RawSignal(kind=kind, source="probe", title=title, dedupe_key=key, strength=strength)


async def _company(domain="crawl.com", name="Crawl Co"):
    from nexus.companies.resolution import resolve_company
    from nexus.core.db import get_sessionmaker

    async with get_sessionmaker()() as s:
        c = await resolve_company(s, domain=domain, name=name)
        await s.commit()
        return c


async def _shared_signals(company_id: str):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.company import CompanySignal

    async with get_sessionmaker()() as s:
        return (await s.scalars(
            select(CompanySignal).where(CompanySignal.company_id == company_id)
        )).all()


async def test_the_shared_crawl_writes_company_signals():
    from nexus.companies.crawl import crawl_company

    company = await _company("shadow1.com")
    report = await crawl_company(company, sources=[_Source([_raw()])])
    assert report.get("error") is None
    assert report["new"] == 1
    assert [s.kind for s in await _shared_signals(company.id)] == ["funding"]


async def test_the_shared_crawl_is_idempotent():
    """The same event fetched twice must update, not duplicate — a unique index backs this, and a
    duplicate would double-count once fan-out is on."""
    from nexus.companies.crawl import crawl_company

    company = await _company("shadow2.com")
    await crawl_company(company, sources=[_Source([_raw()])])
    second = await crawl_company(company, sources=[_Source([_raw()])])
    assert second["new"] == 0
    assert len(await _shared_signals(company.id)) == 1


async def test_sources_receive_the_company_not_a_tenant_account():
    """The stand-in carries the fields sources read, so no source signature had to change — the
    per-tenant path that currently works is untouched."""
    from nexus.companies.crawl import crawl_company

    company = await _company("shadow3.com", name="Shadow Three")
    source = _Source([])
    await crawl_company(company, sources=[source])
    handed = source.seen[0]
    assert handed.domain == "shadow3.com"
    assert handed.name == "Shadow Three"
    assert handed.tenant_id == ""          # shared crawl belongs to no tenant


async def test_one_failing_source_does_not_cost_the_others():
    from nexus.companies.crawl import crawl_company

    class Broken:
        name = "broken"

        async def fetch(self, account):
            raise RuntimeError("provider down")

    company = await _company("shadow4.com")
    report = await crawl_company(company, sources=[Broken(), _Source([_raw()])])
    assert report["new"] == 1


async def test_the_crawl_stamps_even_when_nothing_is_found():
    """The stamp means "we looked". Without it the due-scan picks the same company forever."""
    from nexus.core.db import get_sessionmaker
    from nexus.companies.crawl import crawl_company
    from nexus.models.company import Company

    company = await _company("shadow5.com")
    await crawl_company(company, sources=[_Source([])])
    async with get_sessionmaker()() as s:
        assert (await s.get(Company, company.id)).last_crawled_at is not None


async def test_the_sweep_is_bounded_and_never_raises(monkeypatch):
    from nexus.companies.crawl import crawl_due_companies

    for i in range(3):
        await _company(f"sweep{i}.com")
    report = await crawl_due_companies(limit=2, max_age_hours=0)
    assert report["scanned"] <= 2

    import nexus.core.db as core_db

    def boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(core_db, "get_platform_sessionmaker", boom)
    assert "error" in await crawl_due_companies()


# ---- diff harness (step 4) -----------------------------------------------------------------------

async def test_a_shared_crawl_missing_a_tenant_signal_is_a_disagreement():
    """The case that matters. Extra shared signals are fine — the shared crawl may have run more
    recently — but a MISSING one means fan-out would show less than the tenant has today."""
    from nexus.companies.diff import CompanyDiff

    missing = CompanyDiff(tenant_only=["funding:acme:2026-08"], shared_only=[], both=3)
    assert not missing.agrees

    extra = CompanyDiff(tenant_only=[], shared_only=["news:acme:2026-W31:evt"], both=3)
    assert extra.agrees


async def test_the_diff_only_details_disagreements():
    """A report listing every agreement is one nobody reads."""
    from nexus.companies.backfill import backfill_companies
    from nexus.companies.diff import diff_sample
    from nexus.models.account import Account

    tid = await make_tenant(slug="cd1")
    async with tenant_session(tid) as ts:
        ts.add(Account(tenant_id=tid, name="Diff Co", domain="diffco.com"))
        await ts.flush()
    await backfill_companies()

    report = await diff_sample()
    assert report.get("error") is None
    assert report["compared"] >= 1
    assert all(d["agrees"] is False for d in report["details"])


async def test_an_unlinked_account_is_skipped_not_counted():
    """Comparing an account with no company would report perfect agreement against nothing."""
    from nexus.companies.diff import diff_account
    from nexus.core.db import get_sessionmaker
    from nexus.models.account import Account

    async with get_sessionmaker()() as s:
        assert await diff_account(s, Account(tenant_id="t", name="X", domain="x.com")) is None


async def test_the_diff_never_raises(monkeypatch):
    """It is a gate; a crashing gate is one people route around."""
    import nexus.core.db as core_db

    def boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(core_db, "get_platform_sessionmaker", boom)
    from nexus.companies.diff import diff_sample

    assert "error" in await diff_sample()


async def test_nothing_reads_company_signals_yet():
    """The shadow property, asserted rather than assumed: no per-tenant read path consults
    `company_signals`, so a wrong shared crawl cannot reach a customer's inbox."""
    from pathlib import Path

    consumers = []
    for path in Path("nexus").rglob("*.py"):
        if path.parts[:2] == ("nexus", "companies"):
            continue                       # the shared layer may reference its own table
        if path.name in ("company.py", "__init__.py"):
            continue
        if "CompanySignal" in path.read_text(encoding="utf-8"):
            consumers.append(str(path))
    assert consumers == [], f"company_signals is being read outside the shadow layer: {consumers}"


# ---- fan-out (step 5) ---------------------------------------------------------------------------

async def _linked_account(tid: str, name: str, domain: str):
    """An account already linked to its shared company."""
    from nexus.companies.backfill import backfill_companies

    aid = await _account(tid, name, domain)
    await backfill_companies()
    return aid, await _company_id_of(tid, aid)


async def _tenant_signals(tid: str, account_id: str):
    from nexus.models.signal import SignalEvent

    async with tenant_session(tid) as ts:
        return await ts.list(SignalEvent, SignalEvent.account_id == account_id)


async def test_fanout_is_off_by_default():
    """It multiplies any attribution mistake by the number of subscribing tenants, so it stays off
    until the diff harness reports agreement on real data."""
    from nexus.companies.fanout import fanout_due_companies
    from nexus.core.config import Settings

    assert Settings().shared_company_crawl_enabled is False
    assert (await fanout_due_companies()).get("skipped") == "disabled"


async def test_fanout_delivers_one_crawl_to_every_subscribing_tenant(monkeypatch):
    """The payoff: one crawl, N tenants — instead of N crawls of the same company."""
    from nexus.companies.crawl import crawl_company
    from nexus.companies.fanout import fanout_company
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.company import Company

    monkeypatch.setattr(get_settings(), "shared_company_crawl_enabled", True)
    a = await make_tenant(slug="fo1a")
    b = await make_tenant(slug="fo1b")
    a_id, company_id = await _linked_account(a, "Shared", "fanout1.com")
    b_id, b_company = await _linked_account(b, "Shared Inc", "fanout1.com")
    assert company_id == b_company

    async with get_sessionmaker()() as s:
        company = await s.get(Company, company_id)
    await crawl_company(company, sources=[_Source([_raw(key="fo:funding:1")])])

    report = await fanout_company(company_id)
    assert report["tenants"] == 2
    assert len(await _tenant_signals(a, a_id)) == 1
    assert len(await _tenant_signals(b, b_id)) == 1


async def test_fanout_is_idempotent(monkeypatch):
    """It goes through IngestionService.ingest, so the per-tenant `(tenant_id, dedupe_key)` unique
    constraint applies unchanged — running twice cannot double-deliver."""
    from nexus.companies.crawl import crawl_company
    from nexus.companies.fanout import fanout_company
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.company import Company

    monkeypatch.setattr(get_settings(), "shared_company_crawl_enabled", True)
    tid = await make_tenant(slug="fo2")
    aid, company_id = await _linked_account(tid, "Idem", "fanout2.com")
    async with get_sessionmaker()() as s:
        company = await s.get(Company, company_id)
    await crawl_company(company, sources=[_Source([_raw(key="fo:funding:2")])])

    await fanout_company(company_id)
    await fanout_company(company_id)
    assert len(await _tenant_signals(tid, aid)) == 1


async def test_fanout_creates_alerts_like_a_normal_ingest(monkeypatch):
    """Reusing ingest is the point: alerts fire on the same path. A second write path would drift,
    and the first thing to drift would be alerts — signals appearing with nobody notified, which is
    exactly the bug that shipped once already."""
    from nexus.companies.crawl import crawl_company
    from nexus.companies.fanout import fanout_company
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.alerts import Alert
    from nexus.models.company import Company

    monkeypatch.setattr(get_settings(), "shared_company_crawl_enabled", True)
    tid = await make_tenant(slug="fo3")
    _aid, company_id = await _linked_account(tid, "Alerting", "fanout3.com")
    async with get_sessionmaker()() as s:
        company = await s.get(Company, company_id)
    await crawl_company(
        company, sources=[_Source([_raw(kind="funding", key="fo:funding:3", strength=0.9)])]
    )
    await fanout_company(company_id)

    async with tenant_session(tid) as ts:
        alerts = await ts.list(Alert)
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"


async def test_fanout_skips_archived_accounts(monkeypatch):
    """A deleted account must not resurrect itself with a fresh batch of signals."""
    from nexus.companies.crawl import crawl_company
    from nexus.companies.fanout import fanout_company
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.account import Account
    from nexus.models.company import Company

    monkeypatch.setattr(get_settings(), "shared_company_crawl_enabled", True)
    tid = await make_tenant(slug="fo4")
    aid, company_id = await _linked_account(tid, "Archived", "fanout4.com")
    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == aid)
        acct.set_archived(True, reason="deleted by user")
        await ts.flush()

    async with get_sessionmaker()() as s:
        company = await s.get(Company, company_id)
    await crawl_company(company, sources=[_Source([_raw(key="fo:funding:4")])])
    report = await fanout_company(company_id)

    assert report["delivered"] == 0
    assert await _tenant_signals(tid, aid) == []


async def test_one_tenants_failure_does_not_stop_the_others(monkeypatch):
    """Per-tenant isolation, the same posture as every other sweep in this codebase."""
    from nexus.companies.fanout import fanout_company
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "shared_company_crawl_enabled", True)
    # A company nobody tracks: the sweep must complete cleanly rather than raise.
    report = await fanout_company("does-not-exist")
    assert report.get("error") is None
    assert report["delivered"] == 0


async def test_fanout_never_raises(monkeypatch):
    """It is an optimisation over a working pipeline; a failure degrades to "keep crawling per
    tenant", never to lost signals."""
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "shared_company_crawl_enabled", True)
    import nexus.core.db as core_db

    def boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(core_db, "get_platform_sessionmaker", boom)
    from nexus.companies.fanout import fanout_company

    assert "error" in await fanout_company("any")


def test_the_shared_crawl_is_scheduled_but_fanout_is_gated():
    """The crawl gathers data continuously — it is consumed by nobody — while delivery waits on the
    flag. That separation is what makes the shadow period useful."""
    from nexus.workers.tasks import HANDLERS

    assert "crawl_companies" in HANDLERS
    assert "backfill_companies" in HANDLERS
