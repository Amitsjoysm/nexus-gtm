"""Shared companies carrying a stamped label are not offered to other workspaces (2026-10-26).

Until `test_web_hit_labels.py`, every web-found account was written with its workspace's FIRST ICP
industry and FIRST country, whatever the company was. The backfill copied those values into the
shared `companies` row, `derive_codes` turned them into a LinkedIn code and an office country, and
the chain's shared step offered the row to any other workspace whose ICP named the same industry,
where the fit screen passed because the stamped text was in that ICP too. Measured locally: 202
shared rows; all 65 "Manufacturing" accounts one workspace received were another's web finds.

Decided with the product owner: existing ACCOUNTS keep their values (fix forward only), but the
stamped shared rows are quarantined, not offered again until a real source (a LinkedIn read)
labels them. A row is stamped when the account that CREATED it was a web find from before the fix;
an uploaded, CRM or hand-typed label is the customer's own and is left alone.
"""
from __future__ import annotations

import sys
from datetime import timedelta

from sqlalchemy import select

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.models import Account, Company
from nexus.prospecting import companies as chain
from tests.conftest import make_tenant, seed_relevance_profile, tenant_session

ICP = {"industries": ["Manufacturing"], "linkedin_industry_ids": [25],
       "countries": ["United States"]}
BEFORE_FIX = utcnow() - timedelta(days=20)


async def _account(tid, domain, *, source, industry="Manufacturing", country="United States",
                   created_at=BEFORE_FIX):
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name=domain, domain=domain, industry=industry,
                      country=country, source=source)
        ts.add(acc)
        await ts.flush()
        acc.created_at = created_at


async def _backfill():
    from nexus.companies.backfill import backfill_companies

    await backfill_companies()


async def _row(domain) -> Company:
    async with get_platform_sessionmaker()() as s:
        return (await s.scalars(select(Company).where(Company.domain == domain))).first()


async def _offered(tid, n=5):
    from nexus.prospecting.deliver import fit_screen
    from nexus.relevance.engine import get_profile

    from tests.test_prospect_chain import PagedClient, Web

    async with tenant_session(tid) as ts:
        screen = fit_screen(await get_profile(ts), 0)
        result = await chain.find_icp_companies(ts, ICP, n, screen=screen, client=PagedClient(),
                                                web_search=Web())
    return sorted(c.domain for c in result.candidates if c.source == "shared")


async def _workspace(slug):
    tid = await make_tenant(slug)
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, icp=ICP)
    return tid


async def _script(monkeypatch, *args):
    from scripts import quarantine_stamped_companies

    monkeypatch.setattr(sys, "argv", ["quarantine_stamped_companies.py", *args])
    await quarantine_stamped_companies.main()


async def test_a_stamped_shared_row_is_no_longer_offered_to_another_workspace(monkeypatch):
    finder = await _workspace("quar1")
    await _account(finder, "zuora.com", source="auto_discovery")      # software, stamped
    await _backfill()
    other = await _workspace("quar2")
    assert await _offered(other) == ["zuora.com"], "the stamped row reaches another workspace"

    await _script(monkeypatch, "--apply")

    assert (await _row("zuora.com")).labels_quarantined_at is not None
    assert await _offered(other) == []


async def test_an_uploaded_or_crm_label_is_the_customers_and_is_left_alone(monkeypatch):
    owner = await _workspace("quar3")
    await _account(owner, "acme-mfg.io", source="csv_import")
    await _account(owner, "beta-mfg.io", source="crm")
    await _backfill()

    await _script(monkeypatch, "--apply")

    assert (await _row("acme-mfg.io")).labels_quarantined_at is None
    assert (await _row("beta-mfg.io")).labels_quarantined_at is None


async def test_only_the_account_that_created_the_row_decides(monkeypatch):
    """A second workspace's account got its label FROM the shared row, so its being a discovery
    account says nothing about where the label came from."""
    owner = await _workspace("quar4")
    await _account(owner, "gamma-mfg.io", source="csv_import",
                   created_at=BEFORE_FIX - timedelta(days=5))
    await _backfill()
    later = await _workspace("quar5")
    await _account(later, "gamma-mfg.io", source="auto_discovery")
    await _backfill()

    await _script(monkeypatch, "--apply")

    assert (await _row("gamma-mfg.io")).labels_quarantined_at is None


async def test_a_web_find_made_after_the_fix_is_not_quarantined(monkeypatch):
    finder = await _workspace("quar6")
    await _account(finder, "delta-mfg.io", source="auto_discovery", created_at=utcnow())
    await _backfill()

    await _script(monkeypatch, "--apply")

    assert (await _row("delta-mfg.io")).labels_quarantined_at is None


async def test_the_dry_run_writes_nothing_and_says_what_it_would_do(monkeypatch, capsys):
    finder = await _workspace("quar7")
    await _account(finder, "epsilon.io", source="discovery")
    await _backfill()

    await _script(monkeypatch)

    assert (await _row("epsilon.io")).labels_quarantined_at is None
    assert "would quarantine 1" in capsys.readouterr().out


async def test_a_linkedin_read_relabels_the_row_and_lifts_the_quarantine(monkeypatch):
    from tests.test_prospect_chain import _row as linkedin_row
    from tests.test_prospect_chain import _store

    finder = await _workspace("quar8")
    await _account(finder, "co1.example-co.io", source="auto_discovery")
    await _backfill()
    await _script(monkeypatch, "--apply")
    assert (await _row("co1.example-co.io")).labels_quarantined_at is not None

    await _store(linkedin_row(1, industry=4, countries=("GB",)))

    row = await _row("co1.example-co.io")
    assert row.labels_quarantined_at is None
    # LinkedIn's own label and HQ replace the stamped text, which used to stay (fill blanks only).
    assert (row.industry, row.country) == ("Software Development", "United Kingdom")
    assert row.linkedin_industry_id == 4
