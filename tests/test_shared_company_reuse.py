"""Companies one workspace found are offered to the next workspace with the same ICP (2026-10-02).

The shared ``companies`` table already holds every account any workspace adds (the backfill links
them within a tick), but the ICP chain's first step only read rows a LinkedIn search had stored:
it filters on LinkedIn's industry code, office countries and size band, and a row that came from an
upload, a CRM pull, a hand-add or a web find has none of those. Measured on the local deploy: 256 of
393 shared companies carried an industry and could never be offered to anybody.

Decided with the product owner: **derive the codes from the row's own text, for free** (the same
deterministic mapper the relevance engine scores with, never the LLM), and offer those rows to other
workspaces only when they **clear the workspace's fit threshold**, because a code read off a word is
weaker evidence than a LinkedIn filter.
"""
from __future__ import annotations

from collections import Counter

from sqlalchemy import select

from nexus.core.db import get_platform_sessionmaker
from nexus.models import Account, Company, CompanyCountry
from nexus.prospecting import companies as chain
from tests.conftest import make_tenant, seed_relevance_profile, tenant_session
from tests.test_prospect_chain import ICP, PagedClient, Web, _row, _store


async def _shared(domain, **fields):
    from nexus.companies.resolution import resolve_company

    async with get_platform_sessionmaker()() as s:
        row = await resolve_company(s, domain=domain, name=fields.pop("name", domain),
                                    source="account_backfill", **fields)
        await s.commit()
        return row.id


async def _company(domain):
    async with get_platform_sessionmaker()() as s:
        row = (await s.scalars(select(Company).where(Company.domain == domain))).first()
        countries = sorted((await s.scalars(select(CompanyCountry.country_code).where(
            CompanyCountry.company_id == row.id))).all())
        return row, countries


# ---- deriving the codes ------------------------------------------------------------------------

async def test_a_row_from_an_upload_gets_codes_derived_from_its_own_text():
    await _shared("acme-saas.io", industry="Software & SaaS", country="United Kingdom",
                  employee_count=120)
    row, countries = await _company("acme-saas.io")
    assert row.linkedin_industry_id == 4                      # Software Development
    assert row.hq_country_code == "GB" and countries == ["GB"]
    assert (row.employee_range_min, row.employee_range_max) == (120, 120)
    # Derived, not read from LinkedIn: that is what keeps it behind the fit threshold.
    assert row.linkedin_fetched_at is None


async def test_derived_codes_only_fill_blanks_and_linkedin_replaces_them():
    await _shared("co1.example-co.io", industry="Retail", country="Germany", employee_count=9)
    row, _ = await _company("co1.example-co.io")
    assert row.linkedin_industry_id == 27

    # A LinkedIn read of the same company is better evidence and takes over.
    await _store(_row(1))
    row, countries = await _company("co1.example-co.io")
    assert row.linkedin_industry_id == 4 and row.linkedin_fetched_at is not None
    assert (row.employee_range_min, row.employee_range_max) == (51, 200)
    # The guessed Germany gives way to the offices LinkedIn lists, or a German ICP would keep
    # matching a British company on a word.
    assert countries == ["GB"]

    # A later non-LinkedIn write never touches what LinkedIn set.
    await _shared("co1.example-co.io", industry="Manufacturing", country="France",
                  employee_count=5000)
    row, _ = await _company("co1.example-co.io")
    assert row.linkedin_industry_id == 4 and row.employee_range_max == 200


async def test_text_that_maps_to_nothing_derives_nothing():
    await _shared("vague.io", industry="Stuff and things", country="Atlantis")
    row, countries = await _company("vague.io")
    assert row.linkedin_industry_id is None and row.hq_country_code is None and countries == []


# ---- offering them -----------------------------------------------------------------------------

async def _fit_screen(tid, min_fit):
    from nexus.prospecting.deliver import fit_screen
    from nexus.relevance.engine import get_profile

    async with tenant_session(tid) as ts:
        return fit_screen(await get_profile(ts), min_fit)


async def _workspace(slug):
    tid = await make_tenant(slug)
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, icp=dict(ICP))
    return tid


async def test_a_derived_row_is_offered_to_another_workspace_when_it_fits():
    await _shared("fits.io", name="Fits Ltd", industry="Software & SaaS",
                  country="United Kingdom", employee_count=120)
    tid = await _workspace("reuse1")
    client, web = PagedClient(), Web()
    async with tenant_session(tid) as ts:
        result = await chain.find_icp_companies(ts, ICP, 1, client=client, web_search=web,
                                                screen=await _fit_screen(tid, 50))
    assert [(c.domain, c.source) for c in result.candidates] == [("fits.io", "shared")]
    # Found in the store, so nothing was bought.
    assert client.calls == [] and web.calls == 0


async def test_a_derived_row_below_the_threshold_is_not_offered():
    await _shared("weak.io", industry="Software", country="United Kingdom", employee_count=120)
    tid = await _workspace("reuse2")
    async with tenant_session(tid) as ts:
        result = await chain.find_icp_companies(ts, ICP, 1, screen=await _fit_screen(tid, 101))
    assert [c.domain for c in result.candidates if c.source == "shared"] == []


async def test_without_a_screen_derived_rows_are_never_offered():
    await _shared("unscreened.io", industry="Software", country="United Kingdom",
                  employee_count=120)
    tid = await _workspace("reuse3")
    async with tenant_session(tid) as ts:
        result = await chain.find_icp_companies(ts, ICP, 1)
    assert all(c.source != "shared" for c in result.candidates)


async def test_linkedin_rows_come_before_derived_ones():
    await _store(_row(1))
    await _shared("derived.io", industry="Software", country="United Kingdom",
                  employee_count=120)
    tid = await _workspace("reuse4")
    async with tenant_session(tid) as ts:
        result = await chain.find_icp_companies(ts, ICP, 2, screen=await _fit_screen(tid, 0))
    assert [c.source for c in result.candidates] == ["database", "shared"]


async def test_a_row_the_workspace_already_holds_is_not_offered():
    await _shared("held.io", industry="Software", country="United Kingdom", employee_count=120)
    tid = await _workspace("reuse5")
    async with tenant_session(tid) as ts:
        ts.add(Account(tenant_id=tid, name="Held", domain="held.io"))
    async with tenant_session(tid) as ts:
        result = await chain.find_icp_companies(ts, ICP, 1, screen=await _fit_screen(tid, 0))
    assert all(c.domain != "held.io" for c in result.candidates)


async def test_delivery_holds_a_shared_candidate_to_the_threshold_too():
    from nexus.prospecting.deliver import deliver
    from nexus.relevance.engine import get_profile

    cid = await _shared("deliver.io", industry="Software", country="United Kingdom",
                        employee_count=120)
    tid = await _workspace("reuse6")
    cand = chain.Candidate(domain="deliver.io", name="Deliver", source="shared", company_id=cid,
                           industry="Software", country="United Kingdom", employee_count=120)
    discarded = Counter()
    async with tenant_session(tid) as ts:
        ids, _ = await deliver(ts, await get_profile(ts), [cand], limit=1, source="discovery",
                               min_fit=101, discarded=discarded)
    assert ids == [] and discarded["low_fit"] == 1


# ---- the callers pass the screen -----------------------------------------------------------------

def test_every_caller_of_the_chain_passes_a_fit_screen():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "nexus"
    for rel in ("prospecting/populate.py", "discovery/auto.py", "agents/discovery.py"):
        source = (root / rel).read_text(encoding="utf-8")
        call = source[source.index("find_icp_companies("):]
        call = call[:call.index(")\n")]
        assert "screen=fit_screen(" in call, rel


# ---- rows stored before this ---------------------------------------------------------------------

async def test_the_script_derives_codes_for_rows_stored_before_and_dry_run_writes_nothing(
        monkeypatch, capsys):
    import sys

    from scripts import derive_company_codes

    async with get_platform_sessionmaker()() as s:
        # Written the old way: text, no codes.
        s.add(Company(id="old1", domain="old.io", name="Old", industry="SaaS",
                      country="United Kingdom", employee_count=60, source="account_backfill"))
        await s.commit()

    monkeypatch.setattr(sys, "argv", ["derive_company_codes.py"])
    await derive_company_codes.main()
    row, countries = await _company("old.io")
    assert row.linkedin_industry_id is None and countries == []
    assert "industry code for 1" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["derive_company_codes.py", "--apply"])
    await derive_company_codes.main()
    row, countries = await _company("old.io")
    assert row.linkedin_industry_id == 4 and countries == ["GB"] and row.employee_range_min == 60


# ---- accounts that learn more later ------------------------------------------------------------

async def test_what_a_linked_account_learns_later_reaches_the_shared_row():
    from nexus.companies.backfill import backfill_companies, fill_from_linked_accounts

    tid = await make_tenant("later")
    async with tenant_session(tid) as ts:
        ts.add(Account(tenant_id=tid, name="Later Co", domain="later.io"))
    await backfill_companies()
    row, _ = await _company("later.io")
    assert row.industry is None and row.linkedin_industry_id is None

    async with tenant_session(tid) as ts:
        account = await ts.first(Account, Account.domain == "later.io")
        account.industry, account.country, account.employee_count = \
            "Software", "United Kingdom", 80
    report = await fill_from_linked_accounts()
    assert report["filled"] == 1
    row, countries = await _company("later.io")
    assert row.industry == "Software" and row.linkedin_industry_id == 4 and countries == ["GB"]
    # Converges: nothing left to fill, so the next tick touches nothing.
    assert (await fill_from_linked_accounts())["filled"] == 0
