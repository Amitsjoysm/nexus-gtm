""""How many companies now?" — saving an ICP, then filling the workspace with N companies.

Decided with the product owner 2026-09-24: when an ICP is added the screen asks how many companies
to add; "20" is checked against the credit balance for all 20 before anything is bought, and only
the companies actually delivered are charged, 5 credits each, whatever source they came from.

Saving an ICP stores the LinkedIn industry codes its industries map to, and no longer starts a
discovery batch by itself: that batch would add, and bill, companies nobody asked for.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from nexus.models import Account, ProspectRun
from nexus.models.intelligence import AccountScore
from nexus.prospecting import populate
from tests.conftest import auth, principal_from_token, signup, tenant_session
from tests.test_prospect_chain import PagedClient, Web, _row

ICP = {"industries": ["SaaS"], "countries": ["United Kingdom"],
       "employee_min": 50, "employee_max": 250}


@pytest.fixture(autouse=True)
async def _billing(monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings
    from nexus.features.switches import invalidate

    await sync_catalog()
    await sync_plans()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "account_enrich_enabled", False)
    invalidate()
    yield
    invalidate()


@pytest.fixture
def queued(monkeypatch):
    jobs: list[tuple[str, str]] = []

    async def fake(tenant_id, run_id, *, queue=None):
        jobs.append((tenant_id, run_id))

    import nexus.workers.tasks as tasks

    monkeypatch.setattr(tasks, "enqueue_populate_accounts", fake)
    return jobs


async def _workspace(client, slug: str, *, icp=ICP) -> tuple[dict, str]:
    token = await signup(client, slug=slug, email=f"owner@{slug}.io", company=slug.upper())
    h = auth(token)
    if icp is not None:
        r = await client.put("/api/relevance/profile", headers=h,
                             json={"icp": icp, "value_props": [], "product_context": ""})
        assert r.status_code == 200, r.text
    return h, principal_from_token(token).tenant_id


async def _put_on(tid: str, plan_id: str, *, balance: float) -> None:
    from nexus.billing.credits import balance as current_balance
    from nexus.billing.credits import burn_credits, grant_credits
    from nexus.models.billing import BillingSubscription

    async with tenant_session(tid) as ts:
        sub = await ts.first(BillingSubscription)
        sub.plan_id = plan_id
        await ts.flush()
        now = await current_balance(ts)
        key = uuid.uuid4().hex
        if balance > now:
            await grant_credits(ts, balance - now, reason="test", idempotency_key=f"t-{key}")
        elif balance < now:
            await burn_credits(ts, now - balance, reason="test", idempotency_key=f"t-{key}")


async def _usage(tid: str) -> list:
    from nexus.models.billing import BillingUsageEvent

    async with tenant_session(tid) as ts:
        return await ts.list(BillingUsageEvent,
                             BillingUsageEvent.capability_id == "discovery.account_added")


# ---- saving the ICP ----------------------------------------------------------------------------

async def test_saving_an_icp_stores_its_linkedin_codes_and_asks_for_a_count(client):
    token = await signup(client, slug="pp1", email="owner@pp1.io", company="PP1")
    body = {"icp": {**ICP, "linkedin_industry_ids": [999]}, "value_props": [],
            "product_context": ""}

    first = (await client.put("/api/relevance/profile", headers=auth(token), json=body)).json()
    again = (await client.put("/api/relevance/profile", headers=auth(token),
                              json={**body, "icp": first["icp"]})).json()

    assert first["icp"]["linkedin_industry_ids"] == [4], "derived, never taken from the request"
    assert first["icp_changed"] is True
    assert again["icp_changed"] is False, "the codes the server added are not a change"
    assert again["icp"]["linkedin_industry_ids"] == [4]


async def test_saving_an_icp_no_longer_starts_a_batch_by_itself(client, monkeypatch):
    started: list[bool] = []

    async def fake():
        started.append(True)

    import nexus.workers.tasks as tasks

    monkeypatch.setattr(tasks, "enqueue_discover_icp_accounts", fake)
    await _workspace(client, "pp2")
    assert started == []


def test_a_linkedin_industry_name_scores_as_the_icp_industry_it_maps_to():
    from nexus.models.relevance import RelevanceProfile
    from nexus.relevance import get_relevance_engine

    profile = RelevanceProfile(tenant_id="t", icp={**ICP, "linkedin_industry_ids": [4]})
    linkedin = Account(tenant_id="t", name="A", domain="a.io", industry="Software Development")
    unrelated = Account(tenant_id="t", name="B", domain="b.io", industry="Farming")
    legacy = RelevanceProfile(tenant_id="t", icp=ICP)       # saved before codes were stored

    engine = get_relevance_engine()
    assert engine.score_icp_fit(profile, linkedin).breakdown["industry"] == 1.0
    assert engine.score_icp_fit(profile, unrelated).breakdown["industry"] == 0.0
    assert engine.score_icp_fit(legacy, linkedin).breakdown["industry"] == 0.0


# ---- asking for N ------------------------------------------------------------------------------

async def test_the_quote_is_five_credits_a_company_against_the_balance(client):
    h, _ = await _workspace(client, "pp3")
    q = (await client.get("/api/discovery/populate/quote?count=20", headers=h)).json()
    assert q["credits_per_company"] == 5 and q["total_credits"] == 100
    assert q["balance"] == 200 and q["enough"] is True


async def test_a_request_is_queued_and_a_second_is_refused_while_it_runs(client, queued):
    h, tid = await _workspace(client, "pp4")

    r = await client.post("/api/discovery/populate", headers=h, json={"count": 20})
    again = await client.post("/api/discovery/populate", headers=h, json={"count": 5})

    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued" and r.json()["requested"] == 20
    assert queued == [(tid, r.json()["id"])]
    assert again.status_code == 409


@pytest.mark.parametrize("count", [0, 501])
async def test_a_count_out_of_range_is_refused(client, queued, count):
    h, _ = await _workspace(client, f"pp5x{count}")
    r = await client.post("/api/discovery/populate", headers=h, json={"count": count})
    assert r.status_code == 422 and queued == []


async def test_without_an_icp_there_is_nothing_to_match(client, queued):
    h, _ = await _workspace(client, "pp6", icp=None)
    r = await client.post("/api/discovery/populate", headers=h, json={"count": 5})
    assert r.status_code == 400 and queued == []


async def test_a_balance_short_of_the_whole_count_is_refused_before_anything_is_bought(
    client, queued, monkeypatch
):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    h, tid = await _workspace(client, "pp7")
    await _put_on(tid, "launch", balance=90)            # 20 x 5 = 100 > 90

    r = await client.post("/api/discovery/populate", headers=h, json={"count": 20})

    assert r.status_code == 402, r.text
    assert queued == []
    async with tenant_session(tid) as ts:
        assert await ts.list(ProspectRun) == []


# ---- the job -----------------------------------------------------------------------------------

async def test_the_job_adds_owned_accounts_and_charges_only_what_it_delivered(client, queued):
    h, tid = await _workspace(client, "pp8")
    r = await client.post("/api/discovery/populate", headers=h, json={"count": 5})
    run_id = r.json()["id"]
    owner = principal_from_token(h["Authorization"].split()[1]).user_id
    client_ = PagedClient({1: [_row(1), _row(2), _row(3, website=None), _row(4, total=3)]})

    await populate.execute(tid, run_id, client=client_, web_search=Web())

    run = (await client.get(f"/api/discovery/populate/{run_id}", headers=h)).json()
    assert run["status"] == "done" and run["delivered"] == 3
    assert run["sources"] == {"linkedin": 3}
    assert run["discarded"] == {"no_website": 1}
    async with tenant_session(tid) as ts:
        accounts = await ts.list(Account)
        scores = await ts.list(AccountScore)
    assert sorted(a.domain for a in accounts) == [
        "co1.example-co.io", "co2.example-co.io", "co4.example-co.io"]
    assert {a.owner_user_id for a in accounts} == {owner}
    assert {a.source for a in accounts} == {"prospecting"}
    assert all(a.company_id for a in accounts), "linked to the shared company row"
    assert {a.industry for a in accounts} == {"Software Development"}
    assert len(scores) == 3
    usage = await _usage(tid)
    assert [float(u.quantity) for u in usage] == [3.0], "charged for 3 delivered, not 5 asked"

    latest = (await client.get("/api/discovery/populate/latest", headers=h)).json()
    assert latest["id"] == run_id


async def test_a_retried_job_does_not_run_or_charge_twice(client, queued):
    h, tid = await _workspace(client, "pp9")
    run_id = (await client.post("/api/discovery/populate", headers=h,
                                json={"count": 2})).json()["id"]
    client_ = PagedClient({1: [_row(1, total=2), _row(2, total=2)]})

    await populate.execute(tid, run_id, client=client_, web_search=Web())
    second = await populate.execute(tid, run_id, client=client_, web_search=Web())

    assert second == {"skipped": "not_queued", "run_id": run_id}
    assert len(await _usage(tid)) == 1


async def test_a_failure_is_written_on_the_run_and_nothing_is_charged(client, queued, monkeypatch):
    h, tid = await _workspace(client, "pp10")
    run_id = (await client.post("/api/discovery/populate", headers=h,
                                json={"count": 2})).json()["id"]

    async def broken(*a, **k):
        raise RuntimeError("database went away")

    monkeypatch.setattr("nexus.prospecting.companies.find_icp_companies", broken)
    await populate.execute(tid, run_id, client=PagedClient(), web_search=Web())

    run = (await client.get(f"/api/discovery/populate/{run_id}", headers=h)).json()
    assert run["status"] == "failed" and "database went away" in run["error"]
    assert await _usage(tid) == []
    async with tenant_session(tid) as ts:
        assert await ts.list(Account) == []


async def test_linkedin_being_unavailable_is_recorded_on_the_run(client, queued):
    from nexus.integrations.apify import ApifyNotConfigured

    h, tid = await _workspace(client, "pp11")
    run_id = (await client.post("/api/discovery/populate", headers=h,
                                json={"count": 2})).json()["id"]

    await populate.execute(tid, run_id, client=PagedClient(error=ApifyNotConfigured("x")),
                           web_search=Web())

    run = (await client.get(f"/api/discovery/populate/{run_id}", headers=h)).json()
    assert run["status"] == "done" and run["delivered"] == 0
    assert run["notes"] == {"linkedin": "not_configured"}


async def test_the_worker_retries_a_run_it_cannot_see_yet():
    from nexus.workers.tasks import handle_populate_accounts

    with pytest.raises(RuntimeError, match="not visible yet"):
        await handle_populate_accounts({"tenant_id": "nobody", "run_id": "missing"})


async def test_populated_accounts_are_found_by_the_daily_sweep_as_held(client, queued):
    """The anti-join covers what populate created, so the daily sweep never offers them again."""
    from nexus.prospecting.companies import find_icp_companies

    h, tid = await _workspace(client, "pp12")
    run_id = (await client.post("/api/discovery/populate", headers=h,
                                json={"count": 2})).json()["id"]
    await populate.execute(tid, run_id, client=PagedClient({1: [_row(1), _row(2, total=2)]}),
                           web_search=Web())

    async with tenant_session(tid) as ts:
        icp = (await ts.session.scalars(select(Account.domain))).all()
        result = await find_icp_companies(ts, {**ICP, "linkedin_industry_ids": [4]}, 5,
                                          client=PagedClient(), web_search=Web())
    assert len(icp) == 2
    assert result.candidates == []
