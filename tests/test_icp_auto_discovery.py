"""Daily ICP auto-discovery: strict ICP matching, dedupe, cap, idempotency. Offline."""
from __future__ import annotations

from nexus.discovery.auto import auto_discover_for_tenant
from nexus.integrations.company_search import CompanyCandidate
from nexus.models.account import Account
from nexus.models.intelligence import AccountScore
from nexus.models.relevance import RelevanceProfile
from nexus.workers.tasks import handle_discover_icp_accounts
from tests.conftest import make_tenant, tenant_session

_ICP = {
    "industries": ["saas"],
    "employee_min": 50,
    "employee_max": 5000,
    "countries": ["united states"],
}


def _searcher(candidates):
    async def _search(icp, *, limit):
        return candidates[:limit]

    return _search


async def _with_profile(ts, tid):
    ts.add(RelevanceProfile(tenant_id=tid, icp=_ICP, value_props=[], product_context=""))
    await ts.flush()


async def test_keeps_only_strict_icp_matches():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)
        cands = [
            CompanyCandidate(name="Match Co", domain="match.com", industry="SaaS",
                             country="United States", employee_count=200),       # strong fit -> kept
            CompanyCandidate(name="Wrong Industry", domain="retail.com", industry="Retail",
                             country="France", employee_count=200),              # low fit -> screened out
            CompanyCandidate(name="Too Big", domain="big.com", industry="SaaS",
                             country="United States", employee_count=999999),    # out of band -> hard-filtered
        ]
        res = await auto_discover_for_tenant(
            ts, target_count=10, min_fit=60, pool_limit=50, search=_searcher(cands)
        )
        accounts = await ts.list(Account)
        scores = await ts.list(AccountScore)

    assert res["discovered"] == 1
    assert [a.domain for a in accounts] == ["match.com"]
    assert accounts[0].source == "auto_discovery"
    assert len(scores) == 1 and scores[0].composite >= 60  # ICP-fit persisted for the list badge


async def test_dedupes_existing_domain():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)
        ts.add(Account(tenant_id=tid, name="Already", domain="match.com"))
        await ts.flush()
        res = await auto_discover_for_tenant(
            ts, target_count=10, min_fit=60, pool_limit=50,
            search=_searcher([CompanyCandidate(name="Match", domain="match.com", industry="SaaS",
                                               country="United States", employee_count=200)]),
        )
    assert res["discovered"] == 0  # the existing domain is never re-surfaced


async def test_respects_target_count_cap():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)
        cands = [
            CompanyCandidate(name="A", domain="a.com", industry="SaaS", country="United States", employee_count=100),
            CompanyCandidate(name="B", domain="b.com", industry="SaaS", country="United States", employee_count=100),
        ]
        res = await auto_discover_for_tenant(
            ts, target_count=1, min_fit=60, pool_limit=50, search=_searcher(cands)
        )
    assert res["discovered"] == 1  # stopped at the daily cap


async def test_no_icp_is_a_noop():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        res = await auto_discover_for_tenant(
            ts, target_count=10, min_fit=60, pool_limit=50,
            search=_searcher([CompanyCandidate(name="X", domain="x.com")]),
        )
    assert res["discovered"] == 0 and res.get("skipped") == "no_icp"


async def test_handler_skips_when_disabled():
    # Defaults: automation_enabled and icp_discovery_enabled are both False -> no work, no network.
    res = await handle_discover_icp_accounts({})
    assert res == {"skipped": "disabled"}


async def test_handler_no_icp_tenant_does_not_consume_daily_slot(monkeypatch):
    """A tenant with automation on but no ICP yet is re-checked cheaply each tick (no network),
    and its per-interval slot is NOT consumed — so discovery fires the moment an ICP is added."""
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    s = get_settings()
    monkeypatch.setattr(s, "automation_enabled", True)
    monkeypatch.setattr(s, "icp_discovery_enabled", True)

    tid = await make_tenant()
    async with get_sessionmaker()() as sess:
        t = await sess.get(Tenant, tid)
        t.automation_enabled = True  # opted in, but no RelevanceProfile/ICP defined
        await sess.commit()

    await handle_discover_icp_accounts({})

    async with get_sessionmaker()() as sess:
        t = await sess.get(Tenant, tid)
        assert t.icp_discovery_last_run_at is None  # slot preserved (no ICP -> no run)
