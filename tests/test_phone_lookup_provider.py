# tests/test_phone_lookup_provider.py
"""Phone lookup can be switched off from the Superadmin panel, and "off" must mean off.

It always ran the Apify actor when a key existed; there was nothing to choose. Off has two traps
beyond "do not call the actor":

* A turned-off lookup must not RECORD anything. A recorded `not_found` on the shared person is never
  re-purchased, so switching the lookup off for an afternoon would permanently blank every number
  asked for during it, for every tenant.
* An answer already bought is still an answer. The shared record and a source database cost
  nothing at the margin, so those keep serving (and keep being charged, as today).
"""
from __future__ import annotations

import pathlib

import pytest

from nexus.core.config import get_settings
from nexus.core.db import get_platform_sessionmaker
from nexus.integrations.apify import ApifyClient, set_apify_client
from nexus.people import enrich
from nexus.people.enrich import find_phone
from nexus.people.store import read_person, record_phone_lookup, resolve_person_record
from tests.conftest import make_tenant, tenant_session

LINKEDIN = "https://www.linkedin.com/in/phone-provider-test/"


class _CountingApify(ApifyClient):
    def __init__(self):
        super().__init__(["stub-key"])
        self.runs = 0

    async def run_actor(self, actor, run_input, *, timeout=None):
        self.runs += 1
        return [{"phone": "(415) 555-2671"}]


@pytest.fixture
def apify():
    stub = _CountingApify()
    set_apify_client(stub)
    try:
        yield stub
    finally:
        set_apify_client(None)


@pytest.fixture
def charges(monkeypatch):
    seen: list[dict] = []

    async def record(ts, *, user_id, cached=False, provider="apify"):
        seen.append({"cached": cached, "provider": provider})

    monkeypatch.setattr(enrich, "_meter_lookup", record)
    return seen


async def test_turned_off_buys_nothing_records_nothing_and_charges_nothing(
    monkeypatch, apify, charges,
):
    monkeypatch.setattr(get_settings(), "phone_lookup_provider", "off")
    tid = await make_tenant(slug="plp1", name="PLP One")
    async with tenant_session(tid) as ts:
        result = await find_phone(ts, linkedin_url=LINKEDIN)

    assert result.status == "disabled"
    assert result.phone == ""
    assert apify.runs == 0
    assert charges == []
    async with get_platform_sessionmaker()() as s:
        person = await resolve_person_record(s, linkedin_url=LINKEDIN)
        view = await read_person(s, person.id)
    assert view.last_enriched_at is None, "a turned-off lookup recorded a miss nobody bought"


async def test_an_answer_already_bought_is_still_served_when_turned_off(
    monkeypatch, apify, charges,
):
    tid = await make_tenant(slug="plp2", name="PLP Two")
    async with get_platform_sessionmaker()() as s:
        person = await resolve_person_record(s, linkedin_url=LINKEDIN)
        await record_phone_lookup(s, person.id, phone="+14155552671", source="apify")
        await s.commit()

    monkeypatch.setattr(get_settings(), "phone_lookup_provider", "off")
    async with tenant_session(tid) as ts:
        result = await find_phone(ts, linkedin_url=LINKEDIN)

    assert result.phone == "+14155552671"
    assert result.cached is True
    assert apify.runs == 0
    assert charges == [{"cached": True, "provider": "apify"}]


async def test_apify_remains_the_behaviour_by_default(apify, charges):
    tid = await make_tenant(slug="plp3", name="PLP Three")
    async with tenant_session(tid) as ts:
        result = await find_phone(ts, linkedin_url=LINKEDIN)
    assert result.status == "found"
    assert apify.runs == 1


async def test_an_unrecognised_value_keeps_todays_behaviour(monkeypatch, apify, charges):
    """Only the word `off` turns it off. A typo in the environment must not silently stop phone
    lookups for every rep."""
    monkeypatch.setattr(get_settings(), "phone_lookup_provider", "apfy")
    tid = await make_tenant(slug="plp4", name="PLP Four")
    async with tenant_session(tid) as ts:
        result = await find_phone(ts, linkedin_url=LINKEDIN)
    assert result.status == "found"
    assert apify.runs == 1


def test_the_endpoint_says_where_it_was_turned_off():
    """"Not configured. Set NEXUS_APIFY_API_KEY" would send an operator to add a key that is
    already there."""
    src = pathlib.Path("nexus/api/routers/contacts.py").read_text(encoding="utf-8")
    start = src.index("async def enrich_contact_phone")
    block = src[start: src.index("\n@router", start)]
    assert 'result.status == "disabled"' in block
    assert "turned off in Runtime settings" in block


# ---- Platform health ------------------------------------------------------------------------------

async def test_health_reports_a_turned_off_lookup_without_a_network_call(monkeypatch):
    from nexus.api.routers import admin_health

    async def no_network(*args, **kwargs):
        raise AssertionError("no request expected")

    monkeypatch.setattr(get_settings(), "phone_lookup_provider", "off")
    monkeypatch.setattr(admin_health, "_actor_permission", no_network)
    status, detail = await admin_health._probe_phone_lookup()
    assert status == "unconfigured"
    assert "turned off" in detail


async def test_health_says_when_there_is_no_apify_key(monkeypatch):
    from nexus.api.routers import admin_health

    set_apify_client(ApifyClient([]))
    try:
        status, detail = await admin_health._probe_phone_lookup()
    finally:
        set_apify_client(None)
    assert status == "unconfigured"
    assert "no Apify key" in detail


async def test_an_unapproved_phone_actor_is_degraded_not_broken(monkeypatch):
    """Per-account approval is a console click, not a bad key. Reporting it as an error sends an
    operator to rotate credentials that were never wrong."""
    from nexus.api.routers import admin_health

    async def needs_approval(http, key, actor_id):
        return 200, "FULL_PERMISSIONS"

    set_apify_client(ApifyClient(["stub-key"]))
    monkeypatch.setattr(admin_health, "_actor_permission", needs_approval)
    try:
        status, detail = await admin_health._probe_phone_lookup()
    finally:
        set_apify_client(None)
    assert status == "degraded"
    assert "approval" in detail
