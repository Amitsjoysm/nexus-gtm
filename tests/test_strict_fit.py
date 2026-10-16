"""Strict fit: a premium choice that holds LinkedIn and stored candidates to the fit threshold.

Decided with the product owner (2026-10-15). LinkedIn and database candidates are delivered without
the fit threshold, as ICP matches by construction. Measured on the local deploy, only 15-27% of what
discovery delivered scored 70 or more, so holding them to it is a trade of volume for precision:
off by default, offered to workspaces whose plan includes ``discovery.strict_fit`` (enterprise
mode, granted per plan in Admin > What's included), and always plan-gated except when billing is
switched off entirely.
"""
from __future__ import annotations

from collections import Counter

from nexus.core.config import get_settings
from nexus.core.security import decode_access_token
from nexus.prospecting import companies as chain
from tests.conftest import auth, make_tenant, seed_relevance_profile, signup, tenant_session
from tests.test_prospect_chain import ICP


async def _seed_billing() -> None:
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates

    await sync_catalog()
    await sync_plans()
    await sync_rates()


async def _on_plan(tid: str, plan_id: str) -> None:
    from nexus.models.billing import BillingSubscription

    async with tenant_session(tid) as ts:
        sub = await ts.first(BillingSubscription)
        if sub is None:
            ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        else:
            sub.plan_id, sub.status = plan_id, "active"


async def _workspace(slug: str, plan_id: str, *, strict: bool) -> str:
    from nexus.models.identity import Tenant

    await _seed_billing()
    tid = await make_tenant(slug)
    await _on_plan(tid, plan_id)
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, icp=dict(ICP))
        (await ts.session.get(Tenant, tid)).icp_strict_fit = strict
    return tid


async def _deliver(tid: str, *, min_fit: int, source: str = "linkedin"):
    from nexus.prospecting.deliver import deliver
    from nexus.relevance.engine import get_profile

    cand = chain.Candidate(domain=f"{tid[:6]}.io", name="Strict Co", source=source,
                           industry="Software", country="United Kingdom", employee_count=120)
    discarded: Counter = Counter()
    async with tenant_session(tid) as ts:
        ids, _ = await deliver(ts, await get_profile(ts), [cand], limit=1, source="discovery",
                               min_fit=min_fit, discarded=discarded)
    return ids, discarded


async def test_off_by_default_a_linkedin_candidate_skips_the_threshold():
    tid = await _workspace("sf1", "enterprise", strict=False)
    ids, _ = await _deliver(tid, min_fit=101)
    assert len(ids) == 1, "the default changed: LinkedIn candidates were held to the threshold"


async def test_on_and_included_it_holds_linkedin_and_stored_candidates():
    for slug, source in (("sf2", "linkedin"), ("sf2b", "database")):
        tid = await _workspace(slug, "enterprise", strict=True)
        ids, discarded = await _deliver(tid, min_fit=101, source=source)
        assert ids == [] and discarded["low_fit"] == 1, source


async def test_a_candidate_that_clears_the_bar_is_still_delivered():
    tid = await _workspace("sf3", "enterprise", strict=True)
    ids, _ = await _deliver(tid, min_fit=0)
    assert len(ids) == 1


async def test_chosen_but_not_included_does_nothing():
    """A plan that loses the capability keeps the workspace's choice and stops applying it."""
    tid = await _workspace("sf4", "launch", strict=True)
    ids, _ = await _deliver(tid, min_fit=101)
    assert len(ids) == 1


async def test_billing_off_makes_it_available(monkeypatch):
    from nexus.prospecting import strict_fit

    tid = await _workspace("sf5", "launch", strict=True)
    async with tenant_session(tid) as ts:
        assert not await strict_fit.available(ts)
        monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
        assert await strict_fit.available(ts)


def test_the_capability_is_premium_and_enterprise_includes_it():
    from nexus.billing.catalog import CAPABILITY_SEED
    from nexus.billing.plans import PLAN_SEED

    cap = next(c for c in CAPABILITY_SEED if c["id"] == "discovery.strict_fit")
    assert cap["default_mode"] == "enterprise"
    assert cap["depends_on"] == ["module.discovery"]
    enterprise = next(p for p in PLAN_SEED if p["id"] == "enterprise")
    assert ("discovery.strict_fit", "enabled", None, None) in enterprise["entitlements"]


async def test_the_settings_api_offers_it_only_where_the_plan_includes_it(client):
    await _seed_billing()
    token = await signup(client, slug="sfapi", email="o@sfapi.com", company="SF API")
    h = auth(token)

    got = (await client.get("/api/workspace/automation", headers=h)).json()
    assert got["icp_strict_fit"] is False and got["icp_strict_fit_available"] is False

    refused = await client.patch("/api/workspace/automation", headers=h,
                                 json={"icp_strict_fit": True})
    assert refused.status_code == 422 and "not included" in refused.text
    off = await client.patch("/api/workspace/automation", headers=h,
                             json={"icp_strict_fit": False})
    assert off.status_code == 200, "turning it off must never be refused"

    await _on_plan(decode_access_token(token)["tid"], "enterprise")
    on = await client.patch("/api/workspace/automation", headers=h, json={"icp_strict_fit": True})
    assert on.status_code == 200, on.text
    assert on.json()["icp_strict_fit"] is True and on.json()["icp_strict_fit_available"] is True


async def test_a_string_false_is_refused_not_read_as_true(client):
    await _seed_billing()
    h = auth(await signup(client, slug="sfstr", email="o@sfstr.com", company="SF Str"))
    r = await client.patch("/api/workspace/automation", headers=h,
                           json={"icp_strict_fit": "false"})
    assert r.status_code == 422
