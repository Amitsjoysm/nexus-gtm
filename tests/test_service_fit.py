"""Fit is scored per service (owner decision 2026-10-29).

Every account is scored against every service; the Accounts list shows the service that found an
account and filters by it; Account 360 shows each service's fit; an account's must-match result and
its Find contacts titles are the finding service's.
"""
from __future__ import annotations

from nexus.models import Account, AccountServiceFit, IcpService, RelevanceProfile
from nexus.relevance import services as svc
from tests.conftest import auth, make_tenant, principal_from_token, signup, tenant_session

MAIN_ICP = {"industries": ["Software Development"], "employee_min": 50, "employee_max": 500}
HEALTH_ICP = {"industries": ["Hospitals and Health Care"], "employee_min": 200,
              "employee_max": 5000, "mandatory": ["industry"]}


async def _services(tid: str) -> tuple[str, str]:
    async with tenant_session(tid) as ts:
        ts.add(RelevanceProfile(tenant_id=tid, icp=MAIN_ICP, value_props=[], product_context=""))
        health = IcpService(tenant_id=tid, name="Health cloud", icp=HEALTH_ICP)
        audits = IcpService(tenant_id=tid, name="Audits", icp={"industries": ["Banking"]})
        ts.add_all([health, audits])
        await ts.flush()
        return health.id, audits.id


async def test_every_account_is_scored_against_every_service_and_rescoring_overwrites():
    tid = await make_tenant("fit1")
    health, audits = await _services(tid)
    async with tenant_session(tid) as ts:
        hospital = Account(tenant_id=tid, name="H", domain="h.org", employee_count=900,
                           industry="Hospitals and Health Care")
        ts.add(hospital)
        await ts.flush()
        assert await svc.score_services(ts, [hospital]) == 2
        assert await svc.score_services(ts, [hospital]) == 2
        rows = await ts.list(AccountServiceFit)
        fits = await svc.fits_for(ts, [hospital.id])
    assert len(rows) == 2, "one row per (account, service), overwritten"
    assert fits[hospital.id][health] > fits[hospital.id][audits]
    by_service = {r.service_id: r for r in rows}
    assert by_service[health].gate["passed"] is True
    assert by_service[audits].gate is None, "nothing mandatory, no gate"


async def test_an_archived_service_is_not_scored():
    tid = await make_tenant("fit2")
    health, audits = await _services(tid)
    from nexus.core.db import utcnow

    async with tenant_session(tid) as ts:
        (await ts.get(IcpService, audits)).archived_at = utcnow()
        account = Account(tenant_id=tid, name="A", domain="a.io")
        ts.add(account)
        await ts.flush()
        await svc.score_services(ts, [account])
        assert [r.service_id for r in await ts.list(AccountServiceFit)] == [health]


async def test_the_scoring_refresh_scores_every_service_and_the_badge_is_the_finders():
    from nexus.agents.runtime import get_agent_runtime
    from nexus.models.intelligence import AccountScore

    tid = await make_tenant("fit3")
    health, _audits = await _services(tid)
    async with tenant_session(tid) as ts:
        found = Account(tenant_id=tid, name="H", domain="h.org", employee_count=900,
                        industry="Hospitals and Health Care", service_id=health)
        ts.add(found)
        await ts.flush()
        result = await get_agent_runtime().run("scoring", ts, account_id=found.id)
        assert result.status == "completed", result.error
        score = (await ts.list(AccountScore))[0]
        fits = await svc.fits_for(ts, [found.id])
    assert set(fits[found.id]) == {health, _audits}
    assert score.icp_fit == fits[found.id][health], "scored against the service that found it"


async def test_the_list_shows_the_finding_service_and_filters_by_it(client):
    token = await signup(client, slug="fit4", email="o@fit4.io", company="Fit4")
    tid = principal_from_token(token).tenant_id
    health, audits = await _services(tid)
    async with tenant_session(tid) as ts:
        found = Account(tenant_id=tid, name="Hosp", domain="hosp.org",
                        industry="Software Development", service_id=health)
        mine = Account(tenant_id=tid, name="Mine", domain="mine.io",
                       industry="Software Development")
        ts.add_all([found, mine])
        await ts.flush()
        await svc.score_services(ts, [found, mine])
        found_id, mine_id = found.id, mine.id

    rows = {a["id"]: a for a in (await client.get("/api/accounts", headers=auth(token))).json()}
    assert rows[found_id]["service_id"] == health
    assert rows[found_id]["service_name"] == "Health cloud"
    assert rows[mine_id]["service_id"] is None and rows[mine_id]["service_name"] is None
    # Its must-match result is the health service's (industry), not the main service's (none).
    assert rows[found_id]["icp_gate"]["passed"] is False
    assert rows[mine_id]["icp_gate"] is None

    only = (await client.get(f"/api/accounts?service_id={health}", headers=auth(token))).json()
    assert [a["id"] for a in only] == [found_id]
    main = (await client.get("/api/accounts?service_id=default", headers=auth(token))).json()
    assert [a["id"] for a in main] == [mine_id]


async def test_account_360_shows_each_services_fit_and_rescores_a_stale_one(client):
    token = await signup(client, slug="fit5", email="o@fit5.io", company="Fit5")
    h = auth(token)
    tid = principal_from_token(token).tenant_id
    health, audits = await _services(tid)
    async with tenant_session(tid) as ts:
        account = Account(tenant_id=tid, name="Hosp", domain="hosp.org", employee_count=900,
                          industry="Hospitals and Health Care", service_id=health)
        ts.add(account)
        await ts.flush()
        account_id = account.id

    fits = (await client.get(f"/api/relevance/services/fits/{account_id}", headers=h)).json()
    assert [f["name"] for f in fits] == ["Main service", "Health cloud", "Audits"]
    assert [f["found_here"] for f in fits] == [False, True, False]
    assert fits[1]["icp_fit"] > fits[0]["icp_fit"]
    assert fits[1]["gate"]["passed"] is True

    # The service's ICP changes: the next read scores against the saved ICP, not the old one.
    r = await client.put(f"/api/relevance/services/{health}", headers=h, json={
        "name": "Health cloud", "icp": {"industries": ["Banking"], "mandatory": ["industry"]}})
    assert r.status_code == 200, r.text
    again = (await client.get(f"/api/relevance/services/fits/{account_id}", headers=h)).json()
    assert again[1]["gate"]["passed"] is False
    other = await signup(client, slug="fit5b", email="o@fit5b.io", company="B")
    assert (await client.get(f"/api/relevance/services/fits/{account_id}",
                             headers=auth(other))).status_code == 404


async def test_saving_a_services_icp_rescores_every_account_by_job():
    from nexus.workers.tasks import handle_score_service_fits

    tid = await make_tenant("fit6")
    health, _audits = await _services(tid)
    async with tenant_session(tid) as ts:
        ts.add_all([Account(tenant_id=tid, name=f"A{i}", domain=f"a{i}.io") for i in range(3)])
    out = await handle_score_service_fits({"tenant_id": tid, "service_id": health})
    assert out["scored"] == 3
    async with tenant_session(tid) as ts:
        assert len(await ts.list(AccountServiceFit, AccountServiceFit.service_id == health)) == 3
    assert (await handle_score_service_fits({"tenant_id": tid, "service_id": "gone"}))[
        "skipped"] == "no_service"


async def test_find_contacts_uses_the_buyer_titles_of_the_service_that_found_the_account(
    client, monkeypatch
):
    import nexus.contacts.sourcing as sourcing
    from nexus.relevance.engine import get_profile

    token = await signup(client, slug="fit7", email="o@fit7.io", company="Fit7")
    tid = principal_from_token(token).tenant_id
    health, _audits = await _services(tid)
    async with tenant_session(tid) as ts:
        found = Account(tenant_id=tid, name="Hosp", domain="hosp.org", service_id=health)
        mine = Account(tenant_id=tid, name="Mine", domain="mine.io")
        ts.add_all([found, mine])
        await ts.flush()
        ids = (found.id, mine.id)
    seen: list = []

    async def fake(ts, account, **kwargs):
        seen.append((await get_profile(ts)).icp.get("industries"))
        return sourcing.FindContactsResult([], {"status": "nobody_fits", "found": 0},
                                           {"status": "not_needed", "found": 0},
                                           domain=account.domain or "")

    monkeypatch.setattr(sourcing, "find_contacts", fake)
    for account_id in ids:
        r = await client.post(f"/api/accounts/{account_id}/source-contacts", headers=auth(token))
        assert r.status_code == 200, r.text
    assert seen == [["Hospitals and Health Care"], ["Software Development"]]
