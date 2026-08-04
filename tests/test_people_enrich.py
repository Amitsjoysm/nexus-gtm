# tests/test_people_enrich.py
"""Paid phone lookup, bought once and shared.

The saving this subsystem exists for: forty workspaces tracking one VP Engineering should trigger
one actor run, not forty. The tests that matter are the ones about *not calling* the actor.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nexus.core.db import get_platform_sessionmaker
from nexus.integrations.apify import ApifyClient, set_apify_client
from nexus.people.enrich import extract_phone, find_phone
from nexus.people.store import read_person, resolve_person_record
from tests.conftest import make_tenant, tenant_session

LINKEDIN = "https://www.linkedin.com/in/derek-lemoine-8891a6176/"


class _StubApify(ApifyClient):
    """Counts actor runs so a cache hit is provable, not assumed."""

    def __init__(self, items=None, *, configured=True):
        super().__init__(["stub-key"] if configured else [])
        self.items = items if items is not None else [{"phone": "(415) 555-2671"}]
        self.runs = 0

    async def run_actor(self, actor, run_input, *, timeout=None):
        self.runs += 1
        return self.items


async def _seed_billing():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()


# ---- the saving ----------------------------------------------------------------------------------

async def test_a_second_tenant_asking_for_the_same_person_does_not_pay_again():
    """This one test is the entire business case for the shared people store."""
    await _seed_billing()
    stub = _StubApify()
    set_apify_client(stub)
    try:
        a = await make_tenant(slug="pe1", name="PE One")
        b = await make_tenant(slug="pe2", name="PE Two")

        async with tenant_session(a) as ts:
            first = await find_phone(ts, linkedin_url=LINKEDIN, full_name="Derek Lemoine")
        async with tenant_session(b) as ts:
            second = await find_phone(ts, linkedin_url=LINKEDIN, full_name="Derek Lemoine")

        assert first.phone == "+14155552671"
        assert second.phone == "+14155552671"
        assert second.cached is True
        assert stub.runs == 1, "the second workspace must be served from the shared record"
    finally:
        set_apify_client(None)


async def test_a_recorded_miss_is_not_re_purchased():
    """A `not_found` is an expensive answer. Re-asking every crawl is the difference between a
    bounded monthly bill and an unbounded one."""
    await _seed_billing()
    stub = _StubApify(items=[])
    set_apify_client(stub)
    try:
        tid = await make_tenant(slug="pe3", name="PE Three")
        async with tenant_session(tid) as ts:
            await find_phone(ts, linkedin_url=LINKEDIN)
            again = await find_phone(ts, linkedin_url=LINKEDIN)

        assert again.cached is True
        assert again.status == "not_found"
        assert stub.runs == 1
    finally:
        set_apify_client(None)


async def test_a_stale_record_is_refreshed():
    """People do change numbers. The cache is a saving, not a freeze."""
    await _seed_billing()
    stub = _StubApify()
    set_apify_client(stub)
    try:
        tid = await make_tenant(slug="pe4", name="PE Four")
        async with get_platform_sessionmaker()() as s:
            person = await resolve_person_record(s, linkedin_url=LINKEDIN)
            person.last_enriched_at = datetime.now(timezone.utc) - timedelta(days=400)
            person.phone_status = "found"
            await s.commit()

        async with tenant_session(tid) as ts:
            result = await find_phone(ts, linkedin_url=LINKEDIN, ttl_days=180)

        assert result.cached is False
        assert stub.runs == 1
    finally:
        set_apify_client(None)


# ---- storage ---------------------------------------------------------------------------------------

async def test_the_number_is_stored_in_e164_and_encrypted():
    await _seed_billing()
    set_apify_client(_StubApify(items=[{"phone": "415.555.2671"}]))
    try:
        tid = await make_tenant(slug="pe5", name="PE Five")
        async with tenant_session(ts_id := tid) as ts:
            await find_phone(ts, linkedin_url=LINKEDIN)
        assert ts_id

        async with get_platform_sessionmaker()() as s:
            person = await resolve_person_record(s, linkedin_url=LINKEDIN)
            sealed = person.phone_encrypted
            view = await read_person(s, person.id)

        assert view.phone == "+14155552671", "stored canonical, not as the actor returned it"
        assert sealed and "555" not in sealed, "the number must not be readable from the row"
    finally:
        set_apify_client(None)


async def test_the_region_cascade_reaches_the_stored_number():
    await _seed_billing()
    set_apify_client(_StubApify(items=[{"phone": "020 7946 0958"}]))
    try:
        tid = await make_tenant(slug="pe6", name="PE Six")
        async with tenant_session(tid) as ts:
            result = await find_phone(
                ts, linkedin_url=LINKEDIN, country="United Kingdom", account_country="US",
            )
        assert result.phone == "+442079460958"
    finally:
        set_apify_client(None)


# ---- safety ------------------------------------------------------------------------------------------

async def test_no_identity_means_no_lookup():
    """A contact with neither a profile URL nor an email has no shared identity, so there is
    nothing to look up and nothing to cache against."""
    await _seed_billing()
    stub = _StubApify()
    set_apify_client(stub)
    try:
        tid = await make_tenant(slug="pe7", name="PE Seven")
        async with tenant_session(tid) as ts:
            result = await find_phone(ts, linkedin_url="", full_name="Nobody")
        assert result.ok is False
        assert result.status == "no_identity"
        assert stub.runs == 0
    finally:
        set_apify_client(None)


async def test_an_unconfigured_apify_says_so_rather_than_reporting_no_phone():
    """"Not configured" and "this person has no phone" must never look the same."""
    await _seed_billing()
    set_apify_client(_StubApify(configured=False))
    try:
        tid = await make_tenant(slug="pe8", name="PE Eight")
        async with tenant_session(tid) as ts:
            result = await find_phone(ts, linkedin_url=LINKEDIN)
        assert result.status == "unconfigured"
    finally:
        set_apify_client(None)


async def test_a_failing_actor_never_breaks_the_caller():
    """Losing the contact you were about to call because a scraper was down is worse than a
    blank field."""
    await _seed_billing()

    class _Boom(_StubApify):
        async def run_actor(self, actor, run_input, *, timeout=None):
            raise RuntimeError("actor exploded")

    set_apify_client(_Boom())
    try:
        tid = await make_tenant(slug="pe9", name="PE Nine")
        async with tenant_session(tid) as ts:
            result = await find_phone(ts, linkedin_url=LINKEDIN)
        assert result.ok is False
    finally:
        set_apify_client(None)


# ---- reading third-party output ------------------------------------------------------------------------

def test_the_extractor_survives_the_shapes_actors_actually_return():
    """Actor output is not a contract. Reading one hard-coded key would make an upstream rename
    look like "this person has no phone number" — silent, and identical to the truth."""
    cases = [
        [{"phone": "+14155552671"}],
        [{"phone_number": "+14155552671"}],
        [{"phoneNumbers": ["+14155552671"]}],
        [{"phones": [{"phone": "+14155552671"}]}],
        [{"contact": {"mobile": "+14155552671"}}],
        [{"unrelated": 1}, {"phone": "+14155552671"}],
    ]
    for items in cases:
        assert extract_phone(items) == "+14155552671", items


def test_the_extractor_returns_empty_rather_than_guessing():
    assert extract_phone([]) == ""
    assert extract_phone([{"name": "Derek"}]) == ""
    assert extract_phone([{"phone": ""}]) == ""


# ---- billable, and controllable by a platform admin ------------------------------------------------

async def _usage_rows(tenant_id: str, capability: str) -> list:
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingUsageEvent

    async with get_platform_sessionmaker()() as s:
        return list(
            (
                await s.scalars(
                    select(BillingUsageEvent).where(
                        BillingUsageEvent.tenant_id == tenant_id,
                        BillingUsageEvent.capability_id == capability,
                    )
                )
            ).all()
        )


async def test_a_phone_lookup_is_metered():
    """Enrichment stays billable. The shared store improves margin, not price."""
    from nexus.people.enrich import PHONE_CAPABILITY

    await _seed_billing()
    set_apify_client(_StubApify())
    try:
        tid = await make_tenant(slug="pb1", name="PB One")
        async with tenant_session(tid) as ts:
            await find_phone(ts, linkedin_url=LINKEDIN)
            await ts.session.commit()
        assert len(await _usage_rows(tid, PHONE_CAPABILITY)) == 1
    finally:
        set_apify_client(None)


async def test_a_cache_hit_is_still_metered():
    """The customer received an answer either way. Charging only on a miss would hand the saving to
    whoever happened to ask second and make revenue depend on crawl ordering."""
    from nexus.people.enrich import PHONE_CAPABILITY

    await _seed_billing()
    stub = _StubApify()
    set_apify_client(stub)
    try:
        a = await make_tenant(slug="pb2", name="PB Two")
        b = await make_tenant(slug="pb3", name="PB Three")
        async with tenant_session(a) as ts:
            await find_phone(ts, linkedin_url=LINKEDIN)
            await ts.session.commit()
        async with tenant_session(b) as ts:
            result = await find_phone(ts, linkedin_url=LINKEDIN)
            await ts.session.commit()

        assert result.cached is True
        assert stub.runs == 1, "one actor run..."
        assert len(await _usage_rows(b, PHONE_CAPABILITY)) == 1, "...but both tenants are charged"
    finally:
        set_apify_client(None)


async def test_a_cache_hit_is_tagged_so_the_margin_is_visible():
    """Same revenue, no COGS. Without the flag the saving is invisible in the usage stream."""
    from nexus.people.enrich import PHONE_CAPABILITY

    await _seed_billing()
    set_apify_client(_StubApify())
    try:
        a = await make_tenant(slug="pb4", name="PB Four")
        b = await make_tenant(slug="pb5", name="PB Five")
        async with tenant_session(a) as ts:
            await find_phone(ts, linkedin_url=LINKEDIN)
            await ts.session.commit()
        async with tenant_session(b) as ts:
            await find_phone(ts, linkedin_url=LINKEDIN)
            await ts.session.commit()

        assert (await _usage_rows(a, PHONE_CAPABILITY))[0].attrs.get("cached") is False
        assert (await _usage_rows(b, PHONE_CAPABILITY))[0].attrs.get("cached") is True
    finally:
        set_apify_client(None)


async def test_a_platform_admin_can_reprice_the_phone_lookup(client, monkeypatch):
    """Pricing belongs to Admin, not to a redeploy — the premise of the whole billing platform."""
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings
    from tests.conftest import auth, signup

    await _seed_billing()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@pb.com")
    token = await signup(client, slug="pb6", email="boss@pb.com", company="PB6")

    listed = await client.get("/api/admin/billing/rates", headers=auth(token))
    assert "enrich.phone" in [r["capability_id"] for r in listed.json()], "must be repriceable"

    r = await client.put(
        "/api/admin/billing/rates/enrich.phone", headers=auth(token),
        json={"credits_per_unit": 12, "tiers": [], "active": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["credits_per_unit"] == 12


async def test_a_platform_admin_can_gate_the_phone_lookup_per_plan(client, monkeypatch):
    """Quota and on/off per plan, without touching application code — the one seam."""
    from nexus.core.config import get_settings
    from tests.conftest import auth, signup

    await _seed_billing()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss7@pb.com")
    token = await signup(client, slug="pb7", email="boss7@pb.com", company="PB7")

    r = await client.put(
        "/api/admin/billing/plans/starter/entitlements/enrich.phone",
        headers=auth(token), json={"mode": "metered", "quota": 50},
    )
    assert r.status_code == 200, r.text
    assert r.json()["quota"] == 50
