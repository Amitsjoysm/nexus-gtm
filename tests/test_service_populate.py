"""Each service has its own Populate and its own daily discovery (W8, 8.3, 2026-10-29).

A run names its service, searches that service's ICP, and the accounts it adds record the service
and are scored against every service. The daily sweep gives each service with a daily number its
own passes, runs and tally; the main service is called exactly as before services existed, so a
workspace that adds none sees no change (the existing `test_icp_auto_discovery` suite pins that).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexus.models import Account, AccountServiceFit, IcpService, ProspectRun, RelevanceProfile
from nexus.prospecting import populate
from tests.conftest import auth, make_tenant, principal_from_token, signup, tenant_session
from tests.test_prospect_chain import PagedClient, Web, _row

MAIN_ICP = {"industries": ["SaaS"], "countries": ["United Kingdom"],
            "employee_min": 50, "employee_max": 250}
SERVICE_ICP = {"industries": ["Software Development"], "linkedin_industry_ids": [4],
               "countries": ["United Kingdom"], "employee_min": 50, "employee_max": 250}


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
    jobs: list = []

    async def fake(*args, **kwargs):
        jobs.append(args)

    import nexus.workers.tasks as tasks

    monkeypatch.setattr(tasks, "enqueue_populate_accounts", fake)
    monkeypatch.setattr(tasks, "enqueue_score_service_fits", fake)
    return jobs


async def _workspace(client, slug: str) -> tuple[dict, str, str]:
    token = await signup(client, slug=slug, email=f"owner@{slug}.io", company=slug.upper())
    h = auth(token)
    r = await client.put("/api/relevance/profile", headers=h,
                         json={"icp": MAIN_ICP, "value_props": [], "product_context": ""})
    assert r.status_code == 200, r.text
    sid = (await client.post("/api/relevance/services", headers=h, json={
        "name": "Platform build", "icp": SERVICE_ICP})).json()["id"]
    return h, principal_from_token(token).tenant_id, sid


async def test_a_populate_for_a_service_searches_its_icp_and_stamps_its_accounts(
    client, queued, monkeypatch
):
    h, tid, sid = await _workspace(client, "sp1")
    asked: list[dict] = []
    import nexus.prospecting.companies as chain

    real = chain.find_icp_companies

    async def spy(ts, icp, *args, **kwargs):
        asked.append(icp)
        return await real(ts, icp, *args, **kwargs)

    monkeypatch.setattr(chain, "find_icp_companies", spy)
    r = await client.post("/api/discovery/populate", headers=h, json={"count": 2,
                                                                       "service_id": sid})
    assert r.status_code == 202, r.text
    assert r.json()["service_id"] == sid
    run_id = r.json()["id"]

    await populate.execute(tid, run_id, client=PagedClient({1: [_row(1, total=2),
                                                                _row(2, total=2)]}),
                           web_search=Web())

    assert asked and asked[0]["industries"] == ["Software Development"], "the service's ICP"
    async with tenant_session(tid) as ts:
        accounts = await ts.list(Account)
        fits = await ts.list(AccountServiceFit)
    assert len(accounts) == 2 and {a.service_id for a in accounts} == {sid}
    assert {(f.account_id, f.service_id) for f in fits} == {(a.id, sid) for a in accounts}
    latest = (await client.get(f"/api/discovery/populate/latest?service_id={sid}",
                               headers=h)).json()
    assert latest["id"] == run_id
    main = (await client.get("/api/discovery/populate/latest?service_id=default",
                             headers=h)).json()
    assert main is None, "the main service has no run of its own yet"


async def test_a_populate_for_the_main_service_is_unchanged(client, queued):
    h, tid, sid = await _workspace(client, "sp2")
    r = await client.post("/api/discovery/populate", headers=h, json={"count": 1})
    assert r.json()["service_id"] is None
    await populate.execute(tid, r.json()["id"], client=PagedClient({1: [_row(1, total=1)]}),
                           web_search=Web())
    async with tenant_session(tid) as ts:
        [account] = await ts.list(Account)
        fits = await ts.list(AccountServiceFit)
    assert account.service_id is None
    assert [f.service_id for f in fits] == [sid], "still scored against the other service"


async def test_an_unknown_or_archived_service_is_refused(client, queued):
    h, _tid, sid = await _workspace(client, "sp3")
    unknown = await client.post("/api/discovery/populate", headers=h,
                                json={"count": 1, "service_id": "nope"})
    assert unknown.status_code == 404
    await client.post(f"/api/relevance/services/{sid}/archive", headers=h)
    archived = await client.post("/api/discovery/populate", headers=h,
                                 json={"count": 1, "service_id": sid})
    assert archived.status_code == 409
    assert not [j for j in queued if len(j) == 2 and j[1] != sid], "nothing was queued"


async def test_a_service_with_no_icp_yet_cannot_populate(client, queued):
    h, _tid, _sid = await _workspace(client, "sp4")
    empty = (await client.post("/api/relevance/services", headers=h,
                               json={"name": "Empty"})).json()["id"]
    r = await client.post("/api/discovery/populate", headers=h,
                          json={"count": 1, "service_id": empty})
    assert r.status_code == 400


# ---- the daily sweep -----------------------------------------------------------------------------

T0 = datetime(2026, 10, 29, 7, 0, tzinfo=timezone.utc)


async def _opted_in(monkeypatch, *, main: int = 5, service: int = 0) -> tuple[str, str]:
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    s = get_settings()
    monkeypatch.setattr(s, "automation_enabled", True)
    monkeypatch.setattr(s, "icp_discovery_enabled", True)
    monkeypatch.setattr(s, "icp_discovery_max_attempts", 3)
    monkeypatch.setattr(s, "icp_discovery_retry_hours", 3)
    tid = await make_tenant()
    async with get_sessionmaker()() as sess:
        t = await sess.get(Tenant, tid)
        t.automation_enabled = True
        t.icp_daily_count = main
        await sess.commit()
    async with tenant_session(tid) as ts:
        ts.add(RelevanceProfile(tenant_id=tid, icp=MAIN_ICP, value_props=[], product_context=""))
        svc = IcpService(tenant_id=tid, name="Platform build", icp=SERVICE_ICP,
                         daily_target=service)
        ts.add(svc)
        await ts.flush()
        return tid, svc.id


def _fake(monkeypatch, calls: list, delivered: int = 2):
    import nexus.discovery.auto as auto_mod

    async def fake(ts, *, target_count, min_fit, pool_limit, search=None, **kwargs):
        service = kwargs.get("service")
        calls.append((getattr(service, "id", None), target_count))
        return {"discovered": delivered, "screened": delivered, "account_ids": [],
                "sources": {"web": delivered}, "discarded": {"low_fit": 1},
                "notes": {"web": "failed"}}

    monkeypatch.setattr(auto_mod, "auto_discover_for_tenant", fake)


async def _tick(at):
    from nexus.workers.tasks import handle_discover_icp_accounts

    await handle_discover_icp_accounts({"now_iso": at.isoformat()})


async def test_a_service_with_a_daily_number_gets_its_own_passes(monkeypatch):
    tid, sid = await _opted_in(monkeypatch, main=5, service=8)
    calls: list = []
    _fake(monkeypatch, calls)

    await _tick(T0)
    assert calls == [(None, 5), (sid, 8)], "the main service first, then each service"
    await _tick(T0 + timedelta(hours=3, minutes=1))
    assert calls[2:] == [(None, 3), (sid, 6)], "each tops up against its own tally"

    async with tenant_session(tid) as ts:
        runs = await ts.list(ProspectRun, ProspectRun.kind == "daily")
    assert sorted((r.service_id or "", r.requested) for r in runs) == sorted(
        [("", 5), ("", 3), (sid, 8), (sid, 6)])


async def test_a_service_whose_daily_number_is_off_is_not_searched(monkeypatch):
    _tid, _sid = await _opted_in(monkeypatch, main=5, service=0)
    calls: list = []
    _fake(monkeypatch, calls)
    await _tick(T0)
    assert calls == [(None, 5)]


async def test_a_service_with_no_icp_waits_without_a_paused_alert(monkeypatch):
    from nexus.models.alerts import Alert

    tid, sid = await _opted_in(monkeypatch, main=5, service=5)
    async with tenant_session(tid) as ts:
        (await ts.get(IcpService, sid)).icp = {}
    calls: list = []
    import nexus.discovery.auto as auto_mod

    real = auto_mod.auto_discover_for_tenant

    async def spy(ts, **kwargs):
        calls.append(getattr(kwargs.get("service"), "id", None))
        if kwargs.get("service") is None:
            return {"discovered": 0, "screened": 0, "account_ids": []}
        return await real(ts, **kwargs)

    monkeypatch.setattr(auto_mod, "auto_discover_for_tenant", spy)
    await _tick(T0)
    assert calls == [None, sid]
    async with tenant_session(tid) as ts:
        runs = await ts.list(ProspectRun, ProspectRun.service_id == sid)
        alerts = await ts.list(Alert)
    assert runs == [] and alerts == []


async def test_settings_show_each_services_number_and_today(client, monkeypatch):
    from nexus.core.security import create_access_token

    tid, sid = await _opted_in(monkeypatch, main=5, service=8)
    _fake(monkeypatch, [])
    await _tick(datetime.now(timezone.utc))
    token = create_access_token(user_id="owner-user", tenant_id=tid, role="owner")
    h = {"Authorization": f"Bearer {token}"}
    r = await client.get("/api/workspace/automation", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["today"]["target"] == 5, "the main service's fields are unchanged"
    main, service = body["services"]
    assert main["service_id"] is None and main["daily_count"] == 5
    assert service["service_id"] == sid and service["name"] == "Platform build"
    assert (service["today"]["target"], service["today"]["delivered"]) == (8, 2)

    turned_off = await client.patch("/api/workspace/automation", headers=h,
                                     json={"icp_daily_count": 0, "service_id": sid})
    assert turned_off.status_code == 200, turned_off.text
    assert turned_off.json()["services"][1]["daily_count"] == 0
    too_few = await client.patch("/api/workspace/automation", headers=h,
                                 json={"icp_daily_count": 2})
    assert too_few.status_code == 422, "the main service keeps its 5-100 range"
    gone = await client.patch("/api/workspace/automation", headers=h,
                              json={"icp_daily_count": 10, "service_id": "nope"})
    assert gone.status_code == 404
