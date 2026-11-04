"""A full ICP per service (W8, owner decision 2026-10-29, superseding one ICP per workspace).

Each service a company sells has its own ICP, value props and product context. The workspace's
relevance profile is the DEFAULT service, so a workspace that adds none behaves exactly as before:
that is the first thing pinned here. Then the services API, the one resolver everything reads
(`relevance.services`), "Draft from page" reading a service's own route, and a campaign pitching
one service.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nexus.core.config import get_settings
from nexus.models import Account, IcpService, RelevanceProfile
from nexus.relevance import services as svc
from tests.conftest import auth, make_tenant, principal_from_token, signup, tenant_session

MAIN_ICP = {"industries": ["Software Development"], "employee_min": 50, "employee_max": 500}
CLOUD_ICP = {"industries": ["Hospitals and Health Care"], "employee_min": 200,
             "employee_max": 5000}


async def _workspace(tid: str, *, with_service: bool = True) -> str | None:
    """The main service (the profile) plus, optionally, a second service. Returns its id."""
    async with tenant_session(tid) as ts:
        ts.add(RelevanceProfile(tenant_id=tid, icp=MAIN_ICP, product_context="Payroll software",
                                value_props=[{"name": "Payroll", "pains_solved": ["late pay"]}]))
        if not with_service:
            return None
        service = IcpService(tenant_id=tid, name="Cloud migration", icp=CLOUD_ICP,
                             product_context="We move hospital systems to the cloud.",
                             value_props=[{"name": "Migration", "pains_solved": ["downtime"]}])
        ts.add(service)
        await ts.flush()
        return service.id


# ---- nothing moves for a workspace that adds no service -----------------------------------------

async def test_a_workspace_with_no_service_resolves_everything_to_its_profile():
    tid = await make_tenant("svc0")
    await _workspace(tid, with_service=False)
    from nexus.relevance.engine import get_profile

    async with tenant_session(tid) as ts:
        account = Account(tenant_id=tid, name="A", domain="a.io")
        ts.add(account)
        await ts.flush()
        assert account.service_id is None
        for profile in (await get_profile(ts), await svc.resolve_profile(ts, account=account),
                        await svc.profile_for(ts, None), await svc.profile_for(ts, "default")):
            assert isinstance(profile, RelevanceProfile)
            assert profile.product_context == "Payroll software"
        with svc.using_service(None):
            assert isinstance(await get_profile(ts), RelevanceProfile)
        assert await svc.active_services(ts) == []
        assert await svc.score_services(ts, [account]) == 0, "no service, no per-service rows"


async def test_the_api_lists_only_the_main_service_until_one_is_added(client):
    token = await signup(client, slug="svc1", email="o@svc1.io", company="Svc1")
    r = await client.get("/api/relevance/services", headers=auth(token))
    assert r.status_code == 200, r.text
    [main] = r.json()
    assert main["id"] is None and main["is_default"] is True
    assert main["name"] == svc.DEFAULT_NAME


# ---- the resolver --------------------------------------------------------------------------------

async def test_a_scope_names_the_service_and_an_explicit_default_beats_the_accounts_own():
    tid = await make_tenant("svc2")
    service_id = await _workspace(tid)
    from nexus.relevance.engine import get_profile

    async with tenant_session(tid) as ts:
        found = Account(tenant_id=tid, name="H", domain="h.org", service_id=service_id)
        ts.add(found)
        await ts.flush()
        assert (await svc.resolve_profile(ts, account=found)).id == service_id
        assert isinstance(await get_profile(ts), RelevanceProfile), "no scope: the default"
        with svc.using_service(service_id):
            assert (await get_profile(ts)).id == service_id
            with svc.using_service("default"):
                assert isinstance(await svc.resolve_profile(ts, account=found),
                                  RelevanceProfile), "a campaign for the main service wins"
            assert (await get_profile(ts)).id == service_id
        assert isinstance(await get_profile(ts), RelevanceProfile), "the scope is reset"


async def test_the_agent_runtime_writes_for_the_service_that_found_the_account():
    tid = await make_tenant("svc3")
    service_id = await _workspace(tid)
    from nexus.agents.runtime import get_agent_runtime

    async with tenant_session(tid) as ts:
        found = Account(tenant_id=tid, name="H", domain="h.org", service_id=service_id)
        mine = Account(tenant_id=tid, name="M", domain="m.io")
        ts.add_all([found, mine])
        await ts.flush()
        runtime = get_agent_runtime()
        ctx = await runtime._load_context(ts, found.id, {})
        assert "hospital" in ctx.relevance_context.product_context
        assert (await runtime._load_context(ts, mine.id, {})).relevance_context.product_context \
            == "Payroll software"


# ---- the services API ----------------------------------------------------------------------------

async def test_an_admin_adds_a_service_whose_icp_is_saved_like_the_profiles(client, monkeypatch):
    queued: list = []

    async def fake(tenant_id, service_id, *, queue=None):
        queued.append(service_id)

    import nexus.workers.tasks as tasks

    monkeypatch.setattr(tasks, "enqueue_score_service_fits", fake)
    token = await signup(client, slug="svc4", email="o@svc4.io", company="Svc4")
    r = await client.post("/api/relevance/services", headers=auth(token), json={
        "name": "Cloud migration", "source_url": "acme.com/services/cloud",
        "icp": {**CLOUD_ICP, "mandatory": ["industry", "nonsense"], "linkedin_industry_ids": [1],
                "include_suppliers": "false"},
        "value_props": [{"name": "Migration"}], "product_context": "Hospital cloud moves"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["icp"]["mandatory"] == ["industry"], "the gate options are normalised"
    assert "include_suppliers" not in body["icp"], "a string is not a boolean"
    assert body["icp"]["linkedin_industry_ids"] != [1], "derived, never taken from the request"
    assert body["source_url"] == "https://acme.com/services/cloud"
    assert body["daily_target"] == 0, "a new service never starts buying companies by itself"
    assert body["icp_changed"] is True
    assert queued == [body["id"]], "every account is scored against the new service"

    listed = (await client.get("/api/relevance/services", headers=auth(token))).json()
    assert [s["name"] for s in listed] == [svc.DEFAULT_NAME, "Cloud migration"]


async def test_names_are_unique_and_the_main_service_can_be_renamed(client):
    token = await signup(client, slug="svc5", email="o@svc5.io", company="Svc5")
    h = auth(token)
    one = await client.post("/api/relevance/services", headers=h, json={"name": "Audits"})
    assert one.status_code == 201
    dup = await client.post("/api/relevance/services", headers=h, json={"name": " audits "})
    assert dup.status_code == 409
    main = await client.put("/api/relevance/services/default", headers=h, json={
        "name": "Payroll", "icp": MAIN_ICP, "value_props": [], "product_context": "Payroll"})
    assert main.status_code == 200, main.text
    assert main.json()["name"] == "Payroll" and main.json()["is_default"] is True
    profile = (await client.get("/api/relevance/profile", headers=h)).json()
    assert profile["product_context"] == "Payroll", "it IS the profile"
    clash = await client.put("/api/relevance/services/default", headers=h, json={
        "name": "Audits", "icp": MAIN_ICP})
    assert clash.status_code == 409


async def test_archiving_takes_a_service_off_the_list_and_restore_brings_it_back(client):
    token = await signup(client, slug="svc6", email="o@svc6.io", company="Svc6")
    h = auth(token)
    sid = (await client.post("/api/relevance/services", headers=h,
                             json={"name": "Audits"})).json()["id"]
    archived = await client.post(f"/api/relevance/services/{sid}/archive", headers=h)
    assert archived.json()["archived_at"] is not None
    assert len((await client.get("/api/relevance/services", headers=h)).json()) == 1
    assert len((await client.get("/api/relevance/services?include_archived=true",
                                 headers=h)).json()) == 2
    restored = await client.post(f"/api/relevance/services/{sid}/restore", headers=h)
    assert restored.json()["archived_at"] is None
    assert (await client.post("/api/relevance/services/default/archive",
                              headers=h)).status_code == 404, "the main service stays"


async def test_a_rep_reads_services_but_cannot_change_them(client):
    from nexus.core.security import create_access_token

    owner = await signup(client, slug="svc7", email="o@svc7.io", company="Svc7")
    tid = principal_from_token(owner).tenant_id
    rep = create_access_token(user_id="rep-user", tenant_id=tid, role="rep")
    assert (await client.get("/api/relevance/services", headers=auth(rep))).status_code == 200
    r = await client.post("/api/relevance/services", headers=auth(rep), json={"name": "X"})
    assert r.status_code == 403


async def test_another_workspaces_service_is_not_found(client):
    a = await signup(client, slug="svc8a", email="o@svc8a.io", company="A")
    b = await signup(client, slug="svc8b", email="o@svc8b.io", company="B")
    sid = (await client.post("/api/relevance/services", headers=auth(a),
                             json={"name": "Audits"})).json()["id"]
    r = await client.put(f"/api/relevance/services/{sid}", headers=auth(b),
                         json={"name": "Mine now"})
    assert r.status_code == 404


# ---- "Draft from page" reads the service's own route (8.2) --------------------------------------

class _Search:
    def __init__(self):
        self.queries: list[str] = []

    async def __call__(self, query, *, limit=5):
        self.queries.append(query)
        return [SimpleNamespace(title="Acme", snippet="Acme does many things", url="https://acme.com")]


class _LLM:
    def __init__(self, text: str):
        self.text = text
        self.messages = None

    async def complete(self, messages, **kwargs):
        self.messages = messages
        return SimpleNamespace(text=self.text)


_DRAFT = json.dumps({"icp": {"industries": ["Hospitals and Health Care"]}, "value_props": [],
                     "product_context": "Cloud moves for hospitals"})


async def test_a_service_route_is_read_and_the_model_is_asked_about_that_service():
    from nexus.relevance.website_icp import analyze_website_to_icp

    fetched: list[str] = []

    async def fetch(url):
        fetched.append(url)
        return 200, ("<html><head><title>Cloud migration | Acme</title><script>var x=1;</script>"
                     "</head><body><h1>Cloud migration for hospitals</h1></body></html>")

    search, llm = _Search(), _LLM(_DRAFT)
    draft = await analyze_website_to_icp("acme.com/services/cloud-migration", search=search,
                                         llm=llm, fetch=fetch)
    assert fetched == ["https://acme.com/services/cloud-migration"]
    prompt = " ".join(m.content for m in llm.messages)
    assert "Cloud migration for hospitals" in prompt and "var x" not in prompt
    assert "ONE service" in prompt
    assert all("cloud migration" in q for q in search.queries), search.queries
    assert draft["product_context"] == "Cloud moves for hospitals"


async def test_the_homepage_is_researched_as_before_without_a_fetch():
    from nexus.relevance.website_icp import analyze_website_to_icp

    async def fetch(url):
        raise AssertionError("the homepage path is unchanged")

    search, llm = _Search(), _LLM(_DRAFT)
    await analyze_website_to_icp("https://acme.com/", search=search, llm=llm, fetch=fetch)
    assert "ONE service" not in " ".join(m.content for m in llm.messages)
    assert search.queries[0] == "acme.com product what they do customers"


async def test_a_page_on_a_private_address_is_never_read():
    from nexus.relevance.website_icp import fetch_page, read_service_page

    with pytest.raises(PermissionError):
        await fetch_page("http://127.0.0.1/services/x")
    assert await read_service_page("http://169.254.169.254/latest/meta-data") == ""


async def test_a_draft_is_remembered_for_its_own_service_and_charged_once(client, monkeypatch):
    import nexus.relevance.website_icp as website_icp
    from nexus.models.billing import BillingUsageEvent

    async def fake(url, *, search, llm, fetch=None):
        return json.loads(_DRAFT)

    monkeypatch.setattr(website_icp, "analyze_website_to_icp", fake)
    token = await signup(client, slug="svc9", email="o@svc9.io", company="Svc9")
    h = auth(token)
    sid = (await client.post("/api/relevance/services", headers=h,
                             json={"name": "Cloud"})).json()["id"]
    r = await client.post("/api/relevance/analyze-website", headers=h, json={
        "url": "acme.com/services/cloud", "service_id": sid})
    assert r.status_code == 200, r.text
    assert r.json()["product_context"] == "Cloud moves for hospitals"

    mine = (await client.get(f"/api/relevance/last-analysis?service_id={sid}", headers=h)).json()
    main = (await client.get("/api/relevance/last-analysis", headers=h)).json()
    assert mine["url"] == "acme.com/services/cloud"
    assert main["url"] is None, "the main service's last draft is its own"
    tid = principal_from_token(token).tenant_id
    async with tenant_session(tid) as ts:
        charges = await ts.list(BillingUsageEvent,
                                BillingUsageEvent.capability_id == "ai.icp_from_website")
    assert len(charges) == 1
    unknown = await client.post("/api/relevance/analyze-website", headers=h, json={
        "url": "acme.com/x", "service_id": "nope"})
    assert unknown.status_code == 404


# ---- a campaign pitches one service (8.4) --------------------------------------------------------

@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")


async def _campaign_world(client, slug: str):
    from nexus.models.engagement import MailboxConnection

    token = await signup(client, slug=slug, email=f"sam@{slug}.com", company=slug.upper())
    me = principal_from_token(token)
    await _workspace(me.tenant_id, with_service=False)
    sid = (await client.post("/api/relevance/services", headers=auth(token), json={
        "name": "Cloud migration", "icp": CLOUD_ICP,
        "product_context": "We move hospital systems to the cloud."})).json()["id"]
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email=f"sam@{slug}.com", status="connected",
                                    timezone="Europe/London")
        ts.add(mailbox)
        await ts.flush()
        return token, me.tenant_id, mailbox.id, sid


async def test_a_campaign_is_built_for_a_service_and_can_switch_before_launch(client, engine_on):
    token, _tid, mailbox_id, sid = await _campaign_world(client, "svcc1")
    h = auth(token)
    created = await client.post("/api/engagement/campaigns", headers=h, json={
        "name": "Hospitals", "mailbox_id": mailbox_id,
        "steps": [{"channel": "email", "angle": "Open"}], "service_id": sid})
    assert created.status_code == 201, created.text
    campaign = created.json()
    assert campaign["service_id"] == sid and campaign["service_name"] == "Cloud migration"

    moved = await client.put(f"/api/engagement/campaigns/{campaign['id']}/service", headers=h,
                             json={"service_id": "default"})
    assert moved.json()["service_id"] is None
    assert moved.json()["service_name"] == svc.DEFAULT_NAME
    bad = await client.post("/api/engagement/campaigns", headers=h, json={
        "name": "X", "mailbox_id": mailbox_id, "service_id": "not-a-service"})
    assert bad.status_code == 422

    listed = (await client.get("/api/engagement/campaigns", headers=h)).json()
    assert listed[0]["service_name"] == svc.DEFAULT_NAME


async def test_a_launched_campaign_keeps_its_service(client, engine_on):
    token, tid, mailbox_id, sid = await _campaign_world(client, "svcc2")
    from nexus.models.engagement import EngagementCampaign

    created = await client.post("/api/engagement/campaigns", headers=auth(token), json={
        "name": "Hospitals", "mailbox_id": mailbox_id,
        "steps": [{"channel": "email", "angle": "Open"}], "service_id": sid})
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    async with tenant_session(tid) as ts:
        (await ts.get(EngagementCampaign, cid)).status = "running"
    r = await client.put(f"/api/engagement/campaigns/{cid}/service", headers=auth(token),
                         json={"service_id": None})
    assert r.status_code == 409


async def test_the_campaigns_own_service_gates_who_is_enrolled(client, engine_on):
    """A campaign for the cloud service holds people at companies outside ITS must-match
    industry, whatever the main service's ICP says."""
    token, tid, mailbox_id, sid = await _campaign_world(client, "svcc3")
    from nexus.models.account import Contact

    await client.put(f"/api/relevance/services/{sid}", headers=auth(token), json={
        "name": "Cloud migration", "icp": {**CLOUD_ICP, "mandatory": ["industry"]}})
    async with tenant_session(tid) as ts:
        software = Account(tenant_id=tid, name="Soft", domain="soft.io",
                           industry="Software Development")
        hospital = Account(tenant_id=tid, name="Hosp", domain="hosp.org",
                           industry="Hospitals and Health Care")
        ts.add_all([software, hospital])
        await ts.flush()
        a = Contact(tenant_id=tid, account_id=software.id, full_name="Ann Soft",
                    email="ann@soft.io")
        b = Contact(tenant_id=tid, account_id=hospital.id, full_name="Bo Hosp",
                    email="bo@hosp.org")
        ts.add_all([a, b])
        await ts.flush()
        ids = [a.id, b.id]
    cid = (await client.post("/api/engagement/campaigns", headers=auth(token), json={
        "name": "Hospitals", "mailbox_id": mailbox_id,
        "steps": [{"channel": "email", "angle": "Open"}], "service_id": sid})).json()["id"]
    added = (await client.post(f"/api/engagement/campaigns/{cid}/contacts", headers=auth(token),
                               json={"contact_ids": ids})).json()
    assert added["added"] == [ids[1]]
    assert [s["reason"] for s in added["skipped"]] == ["outside_icp"]


async def test_a_campaign_draft_reads_its_services_icp_and_value_props(monkeypatch):
    """The drafter opens the campaign's scope, so the context pack and the messaging agent, both
    several calls down, read the service through `get_profile`."""
    from nexus.engagement.drafting import drafter
    from nexus.models.engagement import EngagementCampaign
    from nexus.relevance.engine import get_profile

    tid = await make_tenant("svcc4")
    sid = await _workspace(tid)
    seen: list = []

    async def fake(ts, **kwargs):
        seen.append(await get_profile(ts))
        return drafter.Draft(body="x")

    monkeypatch.setattr(drafter, "_draft", fake)
    from nexus.models.identity import User

    async with tenant_session(tid) as ts:
        user = User(email="sam@svcc4.io", full_name="Sam", password_hash="x")
        ts.session.add(user)
        await ts.session.flush()
        for service_id in (sid, None):
            campaign = EngagementCampaign(tenant_id=tid, name="C", owner_user_id=user.id,
                                          service_id=service_id)
            ts.add(campaign)
            await ts.flush()
            await drafter.draft(ts, enrollment=SimpleNamespace(campaign_id=campaign.id),
                                contact=None, account=None, mailbox=None)
        await drafter.draft(ts, enrollment=SimpleNamespace(), contact=None, account=None,
                            mailbox=None)
    assert [getattr(p, "id", None) for p in seen][0] == sid
    assert isinstance(seen[1], RelevanceProfile) and isinstance(seen[2], RelevanceProfile)
