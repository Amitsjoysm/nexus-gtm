"""Prospector against the provider as it really answers (first real answers, 2026-10-22).

Each test pins one fault traced to its root cause on the local stack, with the real API:

* a contact's office ``Phone`` was read as its direct phone (35 of 50 rows have one, 6 a real
  ``DirectPhone``), so a switchboard was shown, and charged for, as a direct dial;
* the person's own ``EmployeeLinkedIn`` (26 of 50) never reached the rep;
* seniority, company size and revenue were free text, so a guessed spelling matched nothing;
* "Show total" asked for a count the provider cannot give: 500 after 120 s on contacts, no total
  field at all on companies (and the page re-bought on every click);
* the first search after the provider idles took 14-124 s against a 30 s limit;
* the request's database connection sat "idle in transaction" for the whole provider wait;
* a Net new scan of slow pages could run for minutes on one click.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from tests.conftest import auth, principal_from_token, signup, tenant_session
from tests.prospector_fixtures import SECRETS, FakeVendor, many_contacts


@pytest.fixture
def vendor():
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient

    fake = FakeVendor()
    search.set_client(InfoJoyClient(keys=["k"], transport=fake.transport(), backoff_s=0))
    yield fake
    search.set_client(None)


@pytest.fixture(autouse=True)
async def _billing_seeded():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates
    from nexus.features.switches import invalidate

    invalidate()
    await sync_catalog()
    await sync_plans()
    await sync_rates()
    yield
    invalidate()


async def _workspace(client, slug: str) -> tuple[dict, str]:
    token = await signup(client, slug=slug, email=f"owner@{slug}.io", company=slug.upper())
    return auth(token), principal_from_token(token).tenant_id


async def _rows(client, h, view="contacts", **body) -> list[dict]:
    r = await client.post("/api/prospector/search", headers=h, json={"view": view, **body})
    assert r.status_code == 200, r.text
    return r.json()["rows"]


# ---- the records -------------------------------------------------------------------------------

def test_an_office_phone_is_not_a_direct_phone():
    from nexus.prospector.records import parse_contact

    office_only = parse_contact({"FirstName": "A", "DirectPhone": None, "Phone": "+1 212 555 0100",
                                 "Extension": None})
    assert office_only.direct_phone == ""
    both = parse_contact({"DirectPhone": "+1 415 555 7890", "Phone": "+1 212 555 0100"})
    assert both.direct_phone == "+1 415 555 7890"


def test_the_persons_own_profile_is_read_and_placeholders_are_not_values():
    from nexus.prospector.records import parse_company, parse_contact

    person = parse_contact({"FirstName": "None", "EmployeeLinkedIn": "https://www.linkedin.com/in/x-y"})
    assert person.linkedin_url == "https://www.linkedin.com/in/x-y"
    assert person.first_name == "None"          # a name is never a placeholder
    company = parse_company({"CompanyName": "Acme", "State": "NULL", "City": "NULL",
                             "IndustryType": "Not Available", "EmployeeSizeRange": "1-10 employees"})
    assert (company.state, company.city, company.industry) == ("", "", "")
    assert company.employee_size_range == "1-10"


async def test_rows_show_a_direct_phone_and_a_profile_only_when_real(client, vendor):
    h, _ = await _workspace(client, "po1")
    rows = {r["first_name"]: r for r in await _rows(client, h)}
    assert rows["Priya"]["has_direct_phone"] is False and rows["Priya"]["phone_masked"] == ""
    assert rows["John"]["has_linkedin"] is True
    assert rows["John"]["linkedin_masked"] == "linkedin.com/in/j•••"
    # Priya's "profile" is a company page: delivery would drop it, so it is not promised.
    assert rows["Priya"]["has_linkedin"] is False and rows["Priya"]["linkedin_masked"] == ""


async def test_no_profile_or_office_number_leaks_into_a_search(client, vendor):
    h, _ = await _workspace(client, "po2")
    r = await client.post("/api/prospector/search", headers=h, json={"view": "contacts"})
    for secret in SECRETS:
        assert secret not in r.text, secret


async def test_a_saved_contact_carries_its_profile_and_no_office_number(client, vendor):
    from nexus.models.account import Contact

    h, tid = await _workspace(client, "po3")
    rows = await _rows(client, h)
    r = await client.post("/api/prospector/deliver", headers=h, json={
        "selection": {"view": "contacts", "scope": "total", "filters": {},
                      "rows": [{"api_page": x["api_page"], "api_page_size": x["api_page_size"],
                                "row_key": x["row_key"]} for x in rows]},
        "action": "save", "include_phones": True, "request_id": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    async with tenant_session(tid) as ts:
        people = {c.full_name: c for c in await ts.list(Contact)}
    assert people["John Smith"].linkedin_url == "https://www.linkedin.com/in/johnsmith-acmeexample"
    assert people["Priya Raman"].linkedin_url is None       # a company page is not a profile
    assert people["Priya Raman"].phone is None              # the office line is not a direct phone


async def test_a_contact_export_has_the_profile(client, vendor):
    h, _ = await _workspace(client, "po4")
    rows = await _rows(client, h)
    john = next(x for x in rows if x["first_name"] == "John")
    r = await client.post("/api/prospector/export", headers=h, json={
        "selection": {"view": "contacts", "scope": "total", "filters": {},
                      "rows": [{"api_page": john["api_page"], "api_page_size": john["api_page_size"],
                                "row_key": john["row_key"]}]},
        "include_phones": False, "request_id": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    header, line = r.text.strip().splitlines()[:2]
    assert header.split(",")[-1] == "linkedin_url"
    assert line.endswith("https://www.linkedin.com/in/johnsmith-acmeexample")


# ---- the filters -------------------------------------------------------------------------------

async def test_bounded_vocabularies_are_offered_as_the_provider_spells_them(client):
    h, _ = await _workspace(client, "po5")
    cat = (await client.get("/api/prospector/filters?view=contacts", headers=h)).json()
    fields = {f["key"]: f for s in cat["sections"] for f in s["fields"]}
    assert "C Level" in fields["seniorities"]["options"]
    assert fields["employee_sizes"]["options"][0] == "1-10"
    assert "$1B-$10B" in fields["revenue_ranges"]["options"]


def test_a_size_sends_every_spelling_the_provider_stores():
    from nexus.prospector.filters import SearchFilters

    payload = SearchFilters(employee_sizes=["1-10", "51-200"]).api_payload("companies")
    assert payload["EmployeeSizeRanges"] == ["1-10", " 1-10", "1-10 employees", "51-200",
                                             "51-200 employees"]


# ---- counts ------------------------------------------------------------------------------------

async def test_a_count_the_provider_cannot_give_is_not_asked_for(client, vendor):
    h, _ = await _workspace(client, "po6")
    for view in ("contacts", "companies"):
        cat = (await client.get(f"/api/prospector/filters?view={view}", headers=h)).json()
        assert cat["can_count"] is False
    body = (await client.post("/api/prospector/search", headers=h,
                              json={"view": "contacts", "include_count": True})).json()
    assert body["total"] is None
    assert vendor.requests[-1]["body"]["IncludeTotalCount"] is False
    # Asking again is a cache hit, not another bought page.
    asked = len(vendor.requests)
    await client.post("/api/prospector/search", headers=h,
                      json={"view": "contacts", "include_count": True})
    assert len(vendor.requests) == asked


async def test_a_count_is_asked_for_once_the_provider_supports_it(client, vendor, monkeypatch):
    from nexus.prospector import client as prospector_client

    monkeypatch.setitem(prospector_client.COUNT_SUPPORTED, "contacts", True)
    h, _ = await _workspace(client, "po7")
    body = (await client.post("/api/prospector/search", headers=h,
                              json={"view": "contacts", "include_count": True})).json()
    assert body["total"] == 4 and vendor.requests[-1]["body"]["IncludeTotalCount"] is True


# ---- slow answers ------------------------------------------------------------------------------

async def test_a_slow_answer_says_why_and_what_to_do():
    from nexus.prospector.client import READ_TIMEOUT_S, InfoJoyClient

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    out = await InfoJoyClient(keys=["k"], transport=httpx.MockTransport(slow)).search(
        "contacts", {}, page=1, size=25)
    assert out.status == "unavailable" and "first search after a quiet spell" in out.detail
    assert READ_TIMEOUT_S >= 60


async def test_the_connection_is_released_while_the_provider_answers(client):
    """The fault measured on Postgres (one connection 'idle in transaction' for the whole wait),
    shown here as the session holding no transaction during the call, then bound again: the held
    row is still found and the day's count still written."""
    from nexus.models.account import Account, Contact
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient
    from nexus.prospector.limits import rows_today
    from nexus.prospector.schemas import SearchIn

    _, tid = await _workspace(client, "po8")
    async with tenant_session(tid) as ts:
        acme = Account(name="Acme Example Inc", domain="acme-example.com")
        ts.add(acme)
        await ts.flush()
        ts.add(Contact(account_id=acme.id, full_name="John Smith",
                       email="john.smith@acme-example.com"))

    seen: list[bool] = []

    class Watching(InfoJoyClient):
        async def search(self, *a, **kw):
            seen.append(watched.session.in_transaction())
            return await super().search(*a, **kw)

    search.set_client(Watching(keys=["k"], transport=FakeVendor().transport(), backoff_s=0))
    try:
        async with tenant_session(tid) as watched:
            out = await search.search(watched, user_id="u", body=SearchIn(view="contacts"))
            assert await rows_today(watched) == 4
    finally:
        search.set_client(None)
    assert seen == [False]
    assert out.rows[0].saved is True and all(not r.saved for r in out.rows[1:])


async def test_unwritten_work_is_never_committed_to_release_a_connection(client):
    from nexus.core.tenancy import connection_released
    from nexus.models.account import Account

    _, tid = await _workspace(client, "po9")
    async with tenant_session(tid) as ts:
        ts.add(Account(name="Pending Example", domain="pending-example.com"))
        async with connection_released(ts) as released:
            assert released is False                 # something is pending: hold, do not commit
        await ts.flush()
        async with connection_released(ts) as released:
            assert released is False                 # flushed, uncommitted: still not committed
        await ts.session.rollback()
    async with tenant_session(tid) as ts:
        assert await ts.first(Account, Account.domain == "pending-example.com") is None


async def test_a_net_new_scan_stops_on_time_and_resumes(client, vendor, monkeypatch):
    """Every row held, so the page never fills: without the deadline the scan would read pages
    2-5 on this one click; with it, it stops after the first and says "keep scanning"."""
    from nexus.prospector import search
    from nexus.prospector.holdings import Held

    async def everyone_held(ts, records):
        from nexus.prospector.identity import contact_identity

        return {contact_identity(r): Held("contact", "c", None) for r in records}

    vendor.contacts = many_contacts(450)
    monkeypatch.setattr(search, "held_contacts", everyone_held)
    monkeypatch.setattr(search, "NET_NEW_DEADLINE_S", 0.0)
    h, _ = await _workspace(client, "po10")
    r = await client.post("/api/prospector/search", headers=h,
                          json={"view": "contacts", "scope": "net_new", "page_size": 25})
    body = r.json()
    assert r.status_code == 200, r.text
    assert len(vendor.requests) == 1                 # one provider page, not five
    assert body["rows"] == [] and body["scanned"] == 100
    assert body["has_more"] is True and body["next_cursor"] and body["scan_capped"] is True

    monkeypatch.setattr(search, "NET_NEW_DEADLINE_S", 45.0)
    r2 = await client.post("/api/prospector/search", headers=h, json={
        "view": "contacts", "scope": "net_new", "page_size": 25, "cursor": body["next_cursor"]})
    assert r2.status_code == 200
    assert vendor.requests[1]["body"]["PageNumber"] == 2      # resumed where it stopped
