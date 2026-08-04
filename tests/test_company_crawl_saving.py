# tests/test_company_crawl_saving.py
"""The shared company crawl has to REPLACE per-tenant work, not add to it.

Until this seam existed the company layer was pure additional cost: every account was still crawled
per tenant, and the shared crawl ran on top. Forty workspaces tracking Stripe crawled it forty-one
times instead of once. The saving only appears when the per-tenant crawl steps aside for accounts
the shared crawl already covers.

The conditions are deliberately narrow, because the failure mode of getting this wrong is an
account that silently stops receiving signals — indistinguishable from a quiet market:

* the flag is on,
* the account is linked to a shared company,
* that company has actually been crawled at least once, AND
* a recorded diff concluded the shared crawl matches the per-tenant one (`crawl_verdict`).

Any of those missing and the per-tenant crawl runs exactly as before. The last condition is what
makes turning the global flag on safe: it changes nothing until evidence exists, per company.
"""
from __future__ import annotations

from datetime import datetime, timezone

from tests.conftest import make_tenant, tenant_session


async def _account(ts, *, name="Acme", domain="acme.com", company_id=None):
    from nexus.models.account import Account

    account = Account(name=name, domain=domain, company_id=company_id)
    ts.add(account)
    await ts.flush()
    return account


async def _company(domain="acme.com", *, crawled: bool, verdict: str = "agrees"):
    """A shared company row. Defaults to a proven one, since most tests are about the other gates."""
    from nexus.companies.resolution import company_id_for, normalise_domain
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.company import Company

    normalised = normalise_domain(domain)
    async with get_platform_sessionmaker()() as session:
        company = Company(
            id=company_id_for(normalised), domain=normalised, name=normalised,
            last_crawled_at=datetime.now(timezone.utc) if crawled else None,
            crawl_verdict=verdict,
        )
        session.add(company)
        await session.commit()
        return company.id


class _CountingIngestion:
    """Stands in for the real ingestion service and records whether it was asked to crawl."""

    def __init__(self):
        self.calls = 0

    async def run_sources(self, ts, account):
        self.calls += 1
        return []


async def _run(monkeypatch, *, flag: bool, company_id, crawled=True):
    from nexus.core.config import get_settings
    from nexus.ingestion.service import set_ingestion_service
    from nexus.pipeline import process_account

    counter = _CountingIngestion()
    set_ingestion_service(counter)
    monkeypatch.setattr(get_settings(), "shared_company_crawl_enabled", flag)

    cid = await _company(crawled=crawled) if company_id else None
    tid = await make_tenant(slug=f"cs{abs(hash((flag, company_id, crawled))) % 9999}", name="CS")
    async with tenant_session(tid) as ts:
        account = await _account(ts, company_id=cid)
        result = await process_account(ts, account)
    return counter.calls, result


async def test_the_per_tenant_crawl_is_skipped_for_a_covered_account(monkeypatch):
    """The whole point: one crawl for the company, not one per tenant."""
    calls, result = await _run(monkeypatch, flag=True, company_id=True)
    assert calls == 0
    assert result.get("signals_source") == "shared_company"


async def test_scoring_still_runs_for_a_skipped_account(monkeypatch):
    """Only the crawl is shared. Scoring, inbox and plays are per-tenant and must be untouched —
    skipping them would turn a cost optimisation into a broken workspace."""
    _, result = await _run(monkeypatch, flag=True, company_id=True)
    assert "composite_score" in result
    assert result.get("scoring_status") != "skipped"


async def test_an_unlinked_account_is_still_crawled_per_tenant(monkeypatch):
    """An account with no usable domain never joins the shared store, so it keeps its own crawl.
    That is the documented safety property, not a gap."""
    calls, _ = await _run(monkeypatch, flag=True, company_id=None)
    assert calls == 1


async def test_a_linked_but_never_crawled_company_does_not_stop_the_tenant_crawl(monkeypatch):
    """Backfill links accounts long before the shared crawler reaches them. Skipping on the link
    alone would black out every newly-linked account until the shared crawl caught up."""
    calls, _ = await _run(monkeypatch, flag=True, company_id=True, crawled=False)
    assert calls == 1


async def test_nothing_changes_while_the_flag_is_off(monkeypatch):
    """Default off. The shadow period has to leave the working pipeline exactly as it was."""
    calls, _ = await _run(monkeypatch, flag=False, company_id=True)
    assert calls == 1


# ---- the two gates must agree ---------------------------------------------------------------------

async def _company_with_verdict(domain: str, verdict: str):
    from datetime import datetime, timezone

    from nexus.companies.resolution import company_id_for, normalise_domain
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.company import Company

    normalised = normalise_domain(domain)
    async with get_platform_sessionmaker()() as s:
        c = Company(
            id=company_id_for(normalised), domain=normalised, name=normalised,
            last_crawled_at=datetime.now(timezone.utc), crawl_verdict=verdict,
        )
        s.add(c)
        await s.commit()
        return c.id


async def _crawls_for(monkeypatch, verdict: str) -> int:
    from nexus.core.config import get_settings
    from nexus.ingestion.service import set_ingestion_service
    from nexus.pipeline import process_account

    counter = _CountingIngestion()
    set_ingestion_service(counter)
    monkeypatch.setattr(get_settings(), "shared_company_crawl_enabled", True)

    cid = await _company_with_verdict(f"{verdict}-co.com", verdict)
    tid = await make_tenant(slug=f"v{verdict[:6]}", name="V")
    async with tenant_session(tid) as ts:
        account = await _account(ts, domain=f"{verdict}-co.com", company_id=cid)
        await process_account(ts, account)
    return counter.calls


async def test_a_proven_company_stops_being_crawled_per_tenant(monkeypatch):
    """`agrees` is the only verdict that earns the saving."""
    assert await _crawls_for(monkeypatch, "agrees") == 0


async def test_an_unproven_company_keeps_its_per_tenant_crawl(monkeypatch):
    """Global flag on, but a company nobody has compared has earned nothing. This is what makes
    turning the flag on safe: it changes nothing until evidence exists."""
    assert await _crawls_for(monkeypatch, "unknown") == 1


async def test_a_disagreeing_company_keeps_its_per_tenant_crawl(monkeypatch):
    """The measured failure: fan-out would deliver less than the tenant already has."""
    assert await _crawls_for(monkeypatch, "disagrees") == 1


async def test_fanout_and_the_per_tenant_skip_gate_on_the_same_verdict(monkeypatch):
    """The invariant that stops an account getting NO crawl at all.

    If fan-out required `agrees` but the skip only required `last_crawled_at`, an unproven company
    would be skipped per-tenant and refused by fan-out — signals would simply stop, which is
    indistinguishable from a quiet market.
    """
    from nexus.companies.fanout import fanout_company
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "shared_company_crawl_enabled", True)
    cid = await _company_with_verdict("unproven-co.com", "unknown")
    report = await fanout_company(cid)
    assert report.get("skipped") == "unproven", (
        "fan-out must refuse exactly the companies the per-tenant skip still crawls"
    )


async def test_recording_a_verdict_is_what_flips_the_gate():
    from nexus.companies.diff import record_verdict
    from nexus.core.db import get_platform_sessionmaker

    cid = await _company_with_verdict("verdict-co.com", "unknown")
    async with get_platform_sessionmaker()() as s:
        assert await record_verdict(s, cid, agrees=True) == "agrees"
        await s.commit()
    async with get_platform_sessionmaker()() as s:
        assert await record_verdict(s, cid, agrees=False) == "disagrees", (
            "a disagreement must be recorded, not merely withheld"
        )


# ---- a newly-added covered account must not show an empty timeline -----------------------------------

async def test_a_new_account_on_a_proven_company_is_seeded_immediately(monkeypatch):
    """The cost optimisation must not reintroduce "zero signals after 30 minutes".

    A rep adds an account whose company is already proven. The per-tenant crawl steps aside, and the
    next fan-out sweep may be hours away — so without a first-run backfill the rep stares at an empty
    page, which is the exact complaint first-crawl-on-create exists to prevent.
    """
    from datetime import datetime, timezone

    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker
    from nexus.ingestion.service import set_ingestion_service
    from nexus.models.company import CompanySignal
    from nexus.models.signal import SignalEvent
    from nexus.pipeline import process_account

    counter = _CountingIngestion()
    set_ingestion_service(counter)
    monkeypatch.setattr(get_settings(), "shared_company_crawl_enabled", True)

    cid = await _company_with_verdict("seeded-co.com", "agrees")
    async with get_platform_sessionmaker()() as s:
        s.add(CompanySignal(
            company_id=cid, kind="funding", source="test", title="Seeded Co raises $40M",
            dedupe_key="seed:1", strength=0.9, occurred_at=datetime.now(timezone.utc),
        ))
        await s.commit()

    tid = await make_tenant(slug="seeded", name="Seeded")
    async with tenant_session(tid) as ts:
        account = await _account(ts, name="Seeded Co", domain="seeded-co.com", company_id=cid)
        await process_account(ts, account)
        signals = await ts.list(SignalEvent, SignalEvent.account_id == account.id)

    assert counter.calls == 0, "still no per-tenant crawl — the saving must survive"
    assert len(signals) == 1, "but the rep must see the company's known history straight away"


async def test_the_seeding_raises_alerts_like_a_normal_ingest(monkeypatch):
    """It goes through IngestionService.ingest, so same-transaction alerting applies unchanged.
    A signal nobody is notified about is the bug this codebase already shipped once."""
    from datetime import datetime, timezone

    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker
    from nexus.ingestion.service import set_ingestion_service
    from nexus.models.alerts import Alert
    from nexus.models.company import CompanySignal
    from nexus.pipeline import process_account

    set_ingestion_service(_CountingIngestion())
    monkeypatch.setattr(get_settings(), "shared_company_crawl_enabled", True)

    cid = await _company_with_verdict("alerted-co.com", "agrees")
    async with get_platform_sessionmaker()() as s:
        s.add(CompanySignal(
            company_id=cid, kind="funding", source="test", title="Alerted Co raises $50M",
            dedupe_key="alert:1", strength=0.9, occurred_at=datetime.now(timezone.utc),
        ))
        await s.commit()

    tid = await make_tenant(slug="alerted", name="Alerted")
    async with tenant_session(tid) as ts:
        account = await _account(ts, name="Alerted Co", domain="alerted-co.com", company_id=cid)
        await process_account(ts, account)
        alerts = await ts.list(Alert)

    assert len(alerts) == 1, "a seeded signal must notify somebody, like any other"


async def test_a_second_run_does_not_re_seed(monkeypatch):
    """`last_refreshed_at` gates it, and ingest dedupes anyway — belt and braces, because a
    re-seed on every sweep would resurrect signals a rep had dismissed."""
    from datetime import datetime, timezone

    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker
    from nexus.ingestion.service import set_ingestion_service
    from nexus.models.company import CompanySignal
    from nexus.models.signal import SignalEvent
    from nexus.pipeline import process_account

    set_ingestion_service(_CountingIngestion())
    monkeypatch.setattr(get_settings(), "shared_company_crawl_enabled", True)

    cid = await _company_with_verdict("twice-co.com", "agrees")
    async with get_platform_sessionmaker()() as s:
        s.add(CompanySignal(
            company_id=cid, kind="funding", source="test", title="Twice Co raises $10M",
            dedupe_key="twice:1", strength=0.9, occurred_at=datetime.now(timezone.utc),
        ))
        await s.commit()

    tid = await make_tenant(slug="twiceco", name="Twice")
    async with tenant_session(tid) as ts:
        account = await _account(ts, name="Twice Co", domain="twice-co.com", company_id=cid)
        await process_account(ts, account)
        account.last_refreshed_at = datetime.now(timezone.utc)
        await ts.flush()
        await process_account(ts, account)
        signals = await ts.list(SignalEvent, SignalEvent.account_id == account.id)

    assert len(signals) == 1
