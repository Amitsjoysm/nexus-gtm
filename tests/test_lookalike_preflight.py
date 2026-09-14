# tests/test_lookalike_preflight.py
"""A paid lookalike search asks the billing engine BEFORE it spends, not after.

Both lookalike endpoints ran their Exa search first and only then called `_meter`, which swallows
every exception — `QuotaExceeded` included. Observed on the local deploy 2026-09-11: workspace
`sdrdemo1096`, plan `free`, enforcement on. The log read

    WARNING:nexus.api.accounts:metering failed for discovery.lookalike_contact
    QuotaExceeded: discovery.lookalike_contact: dependency

and the rep still received 10 sourced people, with no `billing_usage_events` row written. A refused
capability spent our Exa money, charged nothing and never showed the upsell. `_meter`'s "enforcement
applies to the NEXT call" never held: the next call was refused and swallowed the same way.

So the endpoints now ask `preflight` first, for the most the search could deliver (`limit` units),
and still charge afterwards for what was actually delivered. Shadow and `off` behave exactly as they
did, with one deliberate exception: a platform feature switch refuses in shadow too, because the
engine enforces switches whatever the billing mode and every other endpoint already honours that.
"""
from __future__ import annotations

import uuid

import pytest

from nexus.integrations.search import SearchHit
from nexus.integrations.search.provider import set_search_provider
from tests.conftest import auth, signup

CONTACT_CAP = "discovery.lookalike_contact"      # 2 credits per person delivered
COMPANY_CAP = "discovery.lookalike_company"      # 25 credits per company delivered


@pytest.fixture(autouse=True)
async def _billing_and_offline(monkeypatch):
    """Seeded billing, no seed/candidate enrichment, and a clean switch cache.

    Enrichment is switched off so the company path makes exactly the one search this suite counts;
    whether enrichment runs is `test_lookalike.py`'s subject, not this one's.
    """
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
    set_search_provider(None)


def _enforcement(monkeypatch, mode: str) -> None:
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", mode)


# ---- counting fakes for the two paid searches -----------------------------------------------------

def _person(name: str, slug: str, headline: str):
    """An Exa hit shaped like a LinkedIn profile page: the title is the name, the role is in the
    snippet — the shape `source_new` parses."""
    return SearchHit(
        title=name, url=f"https://www.linkedin.com/in/{slug}",
        snippet=f"# {name}\n\n{headline}\n\nNew York, United States (US)", source="fake",
    )


PEOPLE = [
    _person("Dana Reed", "dana-reed-99", "VP Sales at Ramp"),
    _person("Sam Ortiz", "sam-ortiz-12", "VP Sales at Brex"),
    _person("Priya Nair", "priya-nair-7", "VP Sales at Mercury"),
]

COMPANIES = [
    SearchHit(title="Globex", url="https://globex.com", source="fake"),
    SearchHit(title="Initech", url="https://initech.com", source="fake"),
]


class _CountingRegistry:
    """Stands in for `integrations.registry` on the people path. Every method is a paid call."""

    def __init__(self, hits):
        self._hits = list(hits)
        self.calls = 0

    async def search(self, query, *, limit=5):
        self.calls += 1
        return list(self._hits)

    async def find_similar(self, url, *, limit=10):
        self.calls += 1
        return list(self._hits)

    async def contact_search(self, account, icp, *, limit=10):
        self.calls += 1
        return []


class _CountingCompanySearch:
    """The Exa provider the company path calls directly — it does not go through the registry."""

    name = "fake"

    def __init__(self, hits):
        self._hits = list(hits)
        self.calls = 0

    async def search(self, query, *, limit=5):
        self.calls += 1
        return self._hits[:limit]

    async def search_companies(self, query, *, limit=10, exclude_domains=None):
        self.calls += 1
        return self._hits[:limit]


def _people_registry(monkeypatch, hits=PEOPLE) -> _CountingRegistry:
    from nexus.lookalike import contacts as mod

    reg = _CountingRegistry(hits)
    monkeypatch.setattr(mod, "get_registry", lambda: reg)
    return reg


def _company_search(hits=COMPANIES) -> _CountingCompanySearch:
    provider = _CountingCompanySearch(hits)
    set_search_provider(provider)
    return provider


# ---- workspace setup and ledger reads -------------------------------------------------------------

async def _workspace(client, slug: str) -> tuple[dict, str, str]:
    """Signed up AFTER the seeds, so it starts on `free` with its 200 credits, like a real signup.

    Returns (headers, account id, seed contact id).
    """
    h = auth(await signup(client, slug=slug, email=f"o@{slug}.com", company=slug.upper()))
    acc = (await client.post("/api/accounts", headers=h,
                             json={"name": "Acme", "domain": "acme.co"})).json()
    seed = (await client.post(f"/api/accounts/{acc['id']}/contacts", headers=h, json={
        "full_name": "Alex Kim", "title": "VP Sales",
        "linkedin_url": "https://www.linkedin.com/in/alex-kim",
    })).json()
    return h, acc["id"], seed["id"]


async def _tenant_id(slug: str) -> str:
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    async with get_sessionmaker()() as s:
        return (await s.scalars(select(Tenant.id).where(Tenant.slug == slug))).first()


async def _put_on(slug: str, plan_id: str, *, balance: float) -> None:
    """Move the workspace to ``plan_id`` and set its credit balance to exactly ``balance``."""
    from nexus.billing.credits import balance as current_balance
    from nexus.billing.credits import burn_credits, grant_credits
    from nexus.models.billing import BillingSubscription
    from nexus.workers.tasks import tenant_session

    async with tenant_session(await _tenant_id(slug)) as ts:
        sub = await ts.first(BillingSubscription)
        sub.plan_id = plan_id
        await ts.flush()
        now = await current_balance(ts)
        key = uuid.uuid4().hex
        if balance > now:
            await grant_credits(ts, balance - now, reason="test", idempotency_key=f"t-{key}")
        elif balance < now:
            await burn_credits(ts, now - balance, reason="test", idempotency_key=f"t-{key}")


async def _usage(slug: str, capability_id: str) -> list:
    from nexus.models.billing import BillingUsageEvent
    from nexus.workers.tasks import tenant_session

    async with tenant_session(await _tenant_id(slug)) as ts:
        return await ts.list(BillingUsageEvent, BillingUsageEvent.capability_id == capability_id)


async def _balance(slug: str) -> float:
    from nexus.billing.credits import balance
    from nexus.workers.tasks import tenant_session

    async with tenant_session(await _tenant_id(slug)) as ts:
        return await balance(ts)


async def _switch(capability_id: str, state: str, message: str = "") -> None:
    from nexus.core.db import get_sessionmaker
    from nexus.features.switches import invalidate
    from nexus.models.feature_switch import FeatureSwitch

    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id=capability_id, state=state, message=message))
        await s.commit()
    invalidate()


def _people_url(contact_id: str, limit: int = 10) -> str:
    return f"/api/accounts/contacts/{contact_id}/lookalikes?mode=new&limit={limit}"


def _company_url(account_id: str, limit: int = 10) -> str:
    return f"/api/accounts/{account_id}/lookalikes?limit={limit}"


def _assert_upsell(body: dict, capability_id: str, reason: str, plan: str) -> None:
    assert body["error"] == "quota_exceeded", body
    assert body["capability"] == capability_id, body
    assert body["reason"] == reason, body
    assert body["plan"] == plan, body
    assert body["upgrade_url"] == "/settings/billing", body


# ---- enforcement on: refused BEFORE the search ----------------------------------------------------

async def test_a_plan_without_discovery_gets_the_upsell_before_the_people_search(
    client, monkeypatch
):
    """The measured incident. `free` disables `module.discovery`, so sourcing people is refused —
    and must be refused before Exa is called, not after the results are already in hand."""
    _enforcement(monkeypatch, "on")
    h, _, cid = await _workspace(client, "pf1")
    reg = _people_registry(monkeypatch)

    r = await client.post(_people_url(cid), headers=h)

    assert r.status_code == 402, r.text
    _assert_upsell(r.json(), CONTACT_CAP, "dependency", "free")
    assert reg.calls == 0, f"a refused plan still ran {reg.calls} paid search call(s)"
    assert await _usage("pf1", CONTACT_CAP) == []
    assert await _balance("pf1") == 200


async def test_a_plan_without_discovery_gets_the_upsell_before_the_company_search(
    client, monkeypatch
):
    _enforcement(monkeypatch, "on")
    h, aid, _ = await _workspace(client, "pf2")
    provider = _company_search()

    r = await client.post(_company_url(aid), headers=h)

    assert r.status_code == 402, r.text
    _assert_upsell(r.json(), COMPANY_CAP, "dependency", "free")
    assert provider.calls == 0, f"a refused plan still ran {provider.calls} paid search call(s)"
    assert await _usage("pf2", COMPANY_CAP) == []
    assert await _balance("pf2") == 200


async def test_a_balance_short_of_the_full_limit_is_refused_before_searching(client, monkeypatch):
    """Checked for the most the search could deliver. 5 people at 2 credits is 10; with 9 left,
    the search might return five results the balance cannot pay for, so it does not run."""
    _enforcement(monkeypatch, "on")
    h, _, cid = await _workspace(client, "pf3")
    await _put_on("pf3", "launch", balance=9)
    reg = _people_registry(monkeypatch)

    r = await client.post(_people_url(cid, limit=5), headers=h)

    assert r.status_code == 402, r.text
    _assert_upsell(r.json(), CONTACT_CAP, "credits_exhausted", "launch")
    assert reg.calls == 0
    assert await _balance("pf3") == 9


async def test_a_balance_that_covers_the_limit_is_charged_only_for_what_was_delivered(
    client, monkeypatch
):
    """Exactly enough for the limit (5 x 2 = 10) passes, and the charge is still per RESULT: three
    people delivered cost 6 credits, not 10."""
    _enforcement(monkeypatch, "on")
    h, _, cid = await _workspace(client, "pf4")
    await _put_on("pf4", "launch", balance=10)
    reg = _people_registry(monkeypatch)

    r = await client.post(_people_url(cid, limit=5), headers=h)

    assert r.status_code == 200, r.text
    assert len(r.json()["lookalikes"]) == 3
    assert reg.calls >= 1
    rows = await _usage("pf4", CONTACT_CAP)
    assert [float(e.quantity) for e in rows] == [3.0]
    assert await _balance("pf4") == 4


async def test_an_allowed_company_search_is_charged_per_company_delivered(client, monkeypatch):
    """The company path, at its own price: two companies at 25 credits, against a balance that
    exactly covers the limit of five (125)."""
    _enforcement(monkeypatch, "on")
    h, aid, _ = await _workspace(client, "pf5")
    await _put_on("pf5", "launch", balance=125)
    provider = _company_search()

    r = await client.post(_company_url(aid, limit=5), headers=h)

    assert r.status_code == 200, r.text
    assert len(r.json()["lookalikes"]) == 2
    assert provider.calls == 1
    assert [float(e.quantity) for e in await _usage("pf5", COMPANY_CAP)] == [2.0]
    assert await _balance("pf5") == 75


async def test_an_allowed_search_that_finds_nothing_charges_nothing(client, monkeypatch):
    """Charged on results delivered. Asking first must not turn into charging first."""
    _enforcement(monkeypatch, "on")
    h, _, cid = await _workspace(client, "pf6")
    await _put_on("pf6", "launch", balance=100)
    reg = _people_registry(monkeypatch, hits=[])

    r = await client.post(_people_url(cid), headers=h)

    assert r.status_code == 200, r.text
    assert r.json()["lookalikes"] == []
    assert reg.calls >= 1, "the search should have run"
    assert await _usage("pf6", CONTACT_CAP) == []
    assert await _balance("pf6") == 100


async def test_the_existing_mode_is_never_gated(client, monkeypatch):
    """`existing` ranks the workspace's own contacts with no external call. It is not metered, so
    it must not be refused either — even on a plan that excludes discovery."""
    _enforcement(monkeypatch, "on")
    h, _, cid = await _workspace(client, "pf7")

    r = await client.post(f"/api/accounts/contacts/{cid}/lookalikes?mode=existing", headers=h)

    assert r.status_code == 200, r.text


async def test_an_unknown_contact_is_still_a_404_not_a_402(client, monkeypatch):
    """The preflight runs after the request is known to be valid. Telling someone to upgrade in
    order to look up a contact that does not exist is the wrong instruction."""
    _enforcement(monkeypatch, "on")
    h, _, _ = await _workspace(client, "pf8")

    r = await client.post(_people_url("nope"), headers=h)

    assert r.status_code == 404, r.text


# ---- shadow: exactly as before, plus the platform switch ------------------------------------------

async def test_shadow_still_sources_people_for_a_plan_that_excludes_them(client, monkeypatch):
    """Shadow evaluates and records but never refuses on plan grounds. What it did before this
    change: the search runs, the rep gets the people, ONE usage row records them (would-block), and
    no credits move because the plan does not price a capability it disables."""
    _enforcement(monkeypatch, "shadow")
    h, _, cid = await _workspace(client, "ps1")
    reg = _people_registry(monkeypatch)

    r = await client.post(_people_url(cid), headers=h)

    assert r.status_code == 200, r.text
    assert len(r.json()["lookalikes"]) == 3
    assert reg.calls >= 1
    assert [float(e.quantity) for e in await _usage("ps1", CONTACT_CAP)] == [3.0], (
        "the preflight must not add a usage row of its own"
    )
    assert await _balance("ps1") == 200


async def test_shadow_still_finds_companies_for_a_plan_that_excludes_them(client, monkeypatch):
    _enforcement(monkeypatch, "shadow")
    h, aid, _ = await _workspace(client, "ps2")
    provider = _company_search()

    r = await client.post(_company_url(aid), headers=h)

    assert r.status_code == 200, r.text
    assert len(r.json()["lookalikes"]) == 2
    assert provider.calls == 1
    assert [float(e.quantity) for e in await _usage("ps2", COMPANY_CAP)] == [2.0]
    assert await _balance("ps2") == 200


async def test_shadow_still_searches_when_the_balance_cannot_cover_the_limit(client, monkeypatch):
    """The full-limit check is an enforcement decision. In shadow it must not refuse anything the
    meter itself would have let through."""
    _enforcement(monkeypatch, "shadow")
    h, _, cid = await _workspace(client, "ps3")
    await _put_on("ps3", "launch", balance=0)
    reg = _people_registry(monkeypatch)

    r = await client.post(_people_url(cid), headers=h)

    assert r.status_code == 200, r.text
    assert len(r.json()["lookalikes"]) == 3
    assert reg.calls >= 1
    assert [float(e.quantity) for e in await _usage("ps3", CONTACT_CAP)] == [3.0]
    assert await _balance("ps3") == 0


async def test_a_feature_switch_stops_the_search_even_in_shadow(client, monkeypatch):
    """The one shadow-mode change, and it is the engine's own rule: a platform switch is enforced
    whatever the billing mode. Before this, the switch 402 was raised AFTER the search and then
    swallowed, so taking discovery offline never stopped a lookalike."""
    _enforcement(monkeypatch, "shadow")
    h, _, cid = await _workspace(client, "ps4")
    await _put_on("ps4", "launch", balance=100)
    await _switch("module.discovery", "maintenance", "Search is being upgraded")
    reg = _people_registry(monkeypatch)

    r = await client.post(_people_url(cid), headers=h)

    assert r.status_code == 402, r.text
    body = r.json()
    assert body["reason"] == "feature_switch"
    assert body["switch_state"] == "maintenance"
    assert body["switch_message"] == "Search is being upgraded"
    assert "upgrade_url" not in body, "a switch is not an upsell"
    assert reg.calls == 0


# ---- off: the kill switch -------------------------------------------------------------------------

async def test_off_runs_the_search_and_records_nothing(client, monkeypatch):
    _enforcement(monkeypatch, "off")
    h, _, cid = await _workspace(client, "po1")
    reg = _people_registry(monkeypatch)

    r = await client.post(_people_url(cid), headers=h)

    assert r.status_code == 200, r.text
    assert len(r.json()["lookalikes"]) == 3
    assert reg.calls >= 1
    assert await _usage("po1", CONTACT_CAP) == []
    assert await _balance("po1") == 200


# ---- the seam: asking is not charging, and asking agrees with charging -----------------------------

@pytest.mark.parametrize(
    ("plan_id", "balance", "quantity"),
    [("free", 200, 5), ("launch", 9, 5), ("launch", 10, 5)],
)
async def test_preflight_writes_nothing_and_agrees_with_the_meter(
    client, monkeypatch, plan_id, balance, quantity
):
    """`preflight` must reach the verdict `check_and_meter` would, without the usage row or the
    burn. Asked first, then the real meter on the same state: same answer, and the ledger only
    moves when the meter runs."""
    from nexus.billing.entitlements import check_and_meter, preflight
    from nexus.workers.tasks import tenant_session

    _enforcement(monkeypatch, "on")
    slug = f"pp-{plan_id}-{balance}"
    await _workspace(client, slug)
    await _put_on(slug, plan_id, balance=balance)

    async with tenant_session(await _tenant_id(slug)) as ts:
        asked = await preflight(ts, CONTACT_CAP, quantity=quantity)
    assert await _usage(slug, CONTACT_CAP) == []
    assert await _balance(slug) == balance

    async with tenant_session(await _tenant_id(slug)) as ts:
        charged = await check_and_meter(ts, capability_id=CONTACT_CAP, quantity=quantity)

    assert asked.allowed == charged.allowed
    assert asked.reason == charged.reason
