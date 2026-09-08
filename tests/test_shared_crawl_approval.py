# tests/test_shared_crawl_approval.py
"""The shared crawl gathered and delivered nothing, because nothing wrote the verdict.

`nexus/companies/` is a four-stage rollout: backfill, shadow crawl, **diff**, fan-out. Stages 1, 2
and 4 shipped. Stage 3 shipped as a library — `diff.record_verdict` — with no endpoint, no scheduled
job and no caller outside this suite.

Both gates read the same column:

    fanout.fanout_company              -> deliver only when crawl_verdict == "agrees"
    pipeline._covered_by_shared_crawl  -> skip the per-tenant crawl on the same condition

They MUST agree, and they do. What neither could do is become true. Measured on the live deployment
on 2026-09-08: 106 companies, every one crawled that day, 715 shared signals gathered, **every
single one at `crawl_verdict = 'unknown'`** — so fan-out delivered nothing, the per-tenant crawl ran
in full for all 111 accounts, and the layer built to halve the crawl bill was doubling it.

These pin the rung that closes it, and the two things that make an approval mean something.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


async def _as_admin(client, monkeypatch, slug: str = "sharedcrawl"):
    from nexus.billing.catalog import sync_catalog
    from nexus.core.config import get_settings

    await sync_catalog()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    return await signup(client, slug=slug, email="boss@nexus.com", company=slug.upper())


async def _company(domain: str = "acme.com", *, crawled: bool = True) -> str:
    """A shared company row, optionally with a crawl behind it."""
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.company import Company

    async with get_platform_sessionmaker()() as s:
        row = Company(
            id=f"cid-{domain}", domain=domain, name=domain.split(".")[0].title(),
            last_crawled_at=utcnow() if crawled else None,
        )
        s.add(row)
        await s.commit()
        return row.id


# ---- the gap ---------------------------------------------------------------------------------

def test_the_two_gates_still_read_the_same_column():
    """Gate delivery on the verdict but not the per-tenant skip, and an unproven company gets
    NEITHER crawl: signals simply stop, which is indistinguishable from a quiet market.

    The existing suite pins that they agree. This pins that the condition they agree on is the one
    this endpoint writes — a rename on either side would leave the console approving a column
    nothing reads."""
    import inspect

    from nexus.companies import fanout
    from nexus import pipeline

    assert 'crawl_verdict != "agrees"' in inspect.getsource(fanout.fanout_company)
    assert '"crawl_verdict", "unknown") == "agrees"' in inspect.getsource(
        pipeline._covered_by_shared_crawl
    )


async def test_the_verdict_is_reachable_from_the_api(fresh_db, client, monkeypatch):
    """THE gap. `record_verdict` existed and had no caller outside this suite, so `crawl_verdict`
    could never leave `unknown` and both gates were permanently closed."""
    token = await _as_admin(client, monkeypatch)
    company_id = await _company()

    r = await client.post(
        f"/api/admin/shared-crawl/companies/{company_id}/verdict",
        headers=auth(token), json={"agrees": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "agrees"

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.company import Company

    async with get_platform_sessionmaker()() as s:
        assert (await s.get(Company, company_id)).crawl_verdict == "agrees"


async def test_approving_a_company_nobody_has_crawled_is_refused(fresh_db, client, monkeypatch):
    """An approval granted on no evidence is exactly the assertion this gate exists to prevent.
    Agreement between two empty sets is not agreement."""
    token = await _as_admin(client, monkeypatch)
    company_id = await _company("never.com", crawled=False)

    r = await client.post(
        f"/api/admin/shared-crawl/companies/{company_id}/verdict",
        headers=auth(token), json={"agrees": True},
    )
    assert r.status_code == 409, r.text
    assert "never been crawled" in r.text


async def test_recording_a_disagreement_is_never_refused(fresh_db, client, monkeypatch):
    """"We compared and it was wrong" and "we never compared" call for different actions, and one
    absent verdict hides the first behind the second. So a finding is always recordable, including
    for a company with no crawl behind it."""
    token = await _as_admin(client, monkeypatch)
    company_id = await _company("wrong.com", crawled=False)

    r = await client.post(
        f"/api/admin/shared-crawl/companies/{company_id}/verdict",
        headers=auth(token), json={"agrees": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "disagrees"


# ---- the evidence ------------------------------------------------------------------------------

async def test_a_company_agrees_only_when_every_account_agrees(fresh_db):
    """One tenant missing signals the shared crawl does not have is exactly the failure fan-out
    would multiply. Averaging it away across the other tenants is how it would ship unnoticed."""
    from nexus.companies.diff import diff_companies
    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.models.account import Account
    from nexus.models.company import Company, CompanySignal
    from nexus.models.identity import Tenant
    from nexus.models.signal import SignalEvent

    async with get_platform_sessionmaker()() as s:
        company = Company(id="cid-x", domain="x.com", name="X", last_crawled_at=utcnow())
        s.add(company)
        for i, slug in enumerate(("ta", "tb")):
            tenant = Tenant(name=slug.upper(), slug=slug)
            s.add(tenant)
            await s.flush()
            account = Account(
                tenant_id=tenant.id, name="X", domain="x.com", company_id=company.id
            )
            s.add(account)
            await s.flush()
            s.add(SignalEvent(
                tenant_id=tenant.id, account_id=account.id, kind="news", source="web",
                title="shared one", dedupe_key="both", occurred_at=utcnow(),
            ))
            if i == 1:
                # Tenant B holds a signal the shared crawl never found. Fan-out would deliver LESS
                # than that tenant already sees.
                s.add(SignalEvent(
                    tenant_id=tenant.id, account_id=account.id, kind="funding", source="web",
                    title="only tenant B has this", dedupe_key="tenant-only",
                    occurred_at=utcnow(),
                ))
        s.add(CompanySignal(
            company_id=company.id, kind="news", title="shared one",
            dedupe_key="both", occurred_at=utcnow(),
        ))
        await s.commit()

    rows = await diff_companies(limit=10)
    row = next(r for r in rows if r["company_id"] == "cid-x")
    assert row["accounts_agreeing"] == 1
    assert row["accounts_disagreeing"] == 1
    assert row["would_agree"] is False, "one disagreeing tenant must block the whole company"
    assert "tenant-only" in row["missing_from_shared"], "the evidence must name what is missing"


async def test_a_company_with_nothing_to_compare_is_not_comparable(fresh_db):
    """Never crawled, or linked to no accounts with signals. Reporting that as agreement is the
    most dangerous possible false negative for a gate like this."""
    from nexus.companies.diff import diff_companies

    await _company("empty.com", crawled=False)
    rows = await diff_companies(limit=10)
    row = next(r for r in rows if r["domain"] == "empty.com")
    assert row["comparable"] is False
    assert row["would_agree"] is False


async def test_the_summary_reports_what_is_actually_delivering(fresh_db, client, monkeypatch):
    """The headline an operator needs: while nothing is approved, the shared crawl is running and
    reaching nobody, and both crawls are being paid for."""
    token = await _as_admin(client, monkeypatch)
    await _company("a.com")
    await _company("b.com")

    r = await client.get("/api/admin/shared-crawl/summary", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["companies"]["unknown"] == 2
    assert body["accounts_served_by_shared_crawl"] == 0


# ---- the gate on the gate ------------------------------------------------------------------------

async def test_approving_needs_sources_manage(fresh_db, client, monkeypatch):
    """Authorising a data source to reach EVERY tenant is the same act as registering one, and only
    the `superadmin` preset holds `sources.manage`. A tenant role grants none of it."""
    from nexus.billing.permissions import ROLE_PRESETS, SOURCES_MANAGE

    assert SOURCES_MANAGE in ROLE_PRESETS["superadmin"]
    assert SOURCES_MANAGE not in ROLE_PRESETS["support"]

    # A workspace owner who is not a platform admin cannot reach it at all. 404, not 403, and
    # deliberately so: a 403 confirms the route exists, so a non-admin caller gets the same answer
    # for a real admin route as for an invented one. `require_platform_permission` documents it.
    token = await signup(client, slug="tenantonly", email="owner@acme.com", company="Acme")
    r = await client.get("/api/admin/shared-crawl/summary", headers=auth(token))
    assert r.status_code == 404, r.text


async def test_the_verdict_is_audited(fresh_db, client, monkeypatch):
    """Every platform-admin mutation carries a before/after snapshot. This one changes what every
    tenant sees, so it is not the place to make an exception."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingAuditLog

    token = await _as_admin(client, monkeypatch)
    company_id = await _company()
    await client.post(
        f"/api/admin/shared-crawl/companies/{company_id}/verdict",
        headers=auth(token), json={"agrees": True, "note": "checked by hand"},
    )

    async with get_platform_sessionmaker()() as s:
        rows = (await s.scalars(
            select(BillingAuditLog).where(BillingAuditLog.action == "shared_crawl.verdict")
        )).all()
    assert len(rows) == 1
    assert rows[0].before == {"crawl_verdict": "unknown"}
    assert rows[0].after == {"crawl_verdict": "agrees"}


# ---- and the thing it must not become ------------------------------------------------------------

def test_nothing_promotes_a_company_automatically():
    """The subsystem's own rule: "Do not enable fan-out on assertion. It multiplies any attribution
    mistake by the number of subscribing tenants, and four of this subsystem's six attribution bugs
    were found only by running against live providers."

    A scheduled job that promoted a company because two crawls happened to match is promotion on
    assertion wearing a schedule. `record_verdict` must be reachable only from the endpoint a person
    presses, which is what this asserts structurally.
    """
    import pathlib

    callers = []
    for path in pathlib.Path("nexus").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "record_verdict(" in text and path.name != "diff.py":
            callers.append(str(path).replace("\\", "/"))

    assert callers == ["nexus/api/routers/admin_shared_crawl.py"], (
        f"record_verdict gained a caller outside the operator endpoint: {callers}"
    )
