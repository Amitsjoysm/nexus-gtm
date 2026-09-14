"""A person found by "Find similar → Source new people" can be added and then worked.

Sourcing returned strangers with `is_new: true` and nowhere to put them — the only action on the
row opened their LinkedIn profile, so a rep could look at a good lead and do nothing with it.
`POST /accounts/contacts/from-lookalike` adds one, under an account the rep picked or a new one,
and the UI offers the contact's normal actions from there.

Identity rules are the ones the rest of the product uses: a person is deduped on their LinkedIn
profile, an account on its domain — and only on its name when neither side has a domain, which is
`find_existing_account`'s existing contract rather than a new, looser one.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from nexus.core.security import decode_access_token
from nexus.models.account import Contact
from nexus.models.billing import BillingUsageEvent
from tests.conftest import auth, signup, tenant_session

URL = "/api/accounts/contacts/from-lookalike"
JANE = {
    "full_name": "Jane Peer",
    "title": "VP of Sales",
    "linkedin_url": "https://www.linkedin.com/in/jane-peer-42/",
    "company": "Globex",
}


async def _account(client, token, name, domain=None):
    r = await client.post("/api/accounts", headers=auth(token), json={"name": name, "domain": domain})
    assert r.status_code == 201, r.text
    return r.json()


async def test_adds_a_sourced_person_to_the_account_the_rep_picked(client):
    token = await signup(client, slug="as1", email="o@as1.x", company="AS1")
    h = auth(token)
    acct = await _account(client, token, "Globex", "globex.com")

    r = await client.post(URL, headers=h, json={**JANE, "account_id": acct["id"]})

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True and body["account_created"] is False
    assert body["account_id"] == acct["id"] and body["account_name"] == "Globex"
    c = body["contact"]
    assert c["full_name"] == "Jane Peer" and c["title"] == "VP of Sales"
    assert c["linkedin_url"] == JANE["linkedin_url"]
    # Now a normal contact: it shows on the account and in the workspace book.
    listed = (await client.get(f"/api/accounts/{acct['id']}/contacts", headers=h)).json()
    assert [x["id"] for x in listed] == [c["id"]]


async def test_creates_the_company_when_the_workspace_does_not_have_it(client):
    h = auth(await signup(client, slug="as2", email="o@as2.x", company="AS2"))

    r = await client.post(URL, headers=h, json={
        **JANE, "new_account_name": "Globex", "new_account_domain": "https://www.Globex.com/"})

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["account_created"] is True and body["account_name"] == "Globex"
    assert body["account_domain"] == "globex.com"          # normalised like every other write
    acct = (await client.get(f"/api/accounts/{body['account_id']}", headers=h)).json()
    assert acct["source"] == "lookalike"


async def test_reuses_the_account_that_already_has_that_domain(client):
    token = await signup(client, slug="as3", email="o@as3.x", company="AS3")
    h = auth(token)
    existing = await _account(client, token, "Globex Corporation", "globex.com")

    r = await client.post(URL, headers=h, json={
        **JANE, "new_account_name": "Globex", "new_account_domain": "globex.com"})

    body = r.json()
    assert body["account_created"] is False and body["account_id"] == existing["id"]


async def test_a_bare_company_name_reuses_a_domainless_account_of_that_name(client):
    token = await signup(client, slug="as4", email="o@as4.x", company="AS4")
    h = auth(token)
    existing = await _account(client, token, "Globex Inc.")

    r = await client.post(URL, headers=h, json={**JANE, "new_account_name": "globex"})

    assert r.json()["account_id"] == existing["id"]


async def test_the_same_profile_is_never_added_twice(client):
    token = await signup(client, slug="as5", email="o@as5.x", company="AS5")
    h = auth(token)
    acct = await _account(client, token, "Globex", "globex.com")
    first = (await client.post(URL, headers=h, json={**JANE, "account_id": acct["id"]})).json()

    # Same person, URL spelled differently, offered again from another search.
    again = await client.post(URL, headers=h, json={
        **JANE, "linkedin_url": "http://linkedin.com/in/jane-peer-42", "new_account_name": "Other"})

    assert again.status_code == 200, again.text
    body = again.json()
    assert body["created"] is False and body["account_created"] is False
    assert body["contact"]["id"] == first["contact"]["id"]
    assert body["account_id"] == acct["id"]


async def test_a_profile_is_found_among_many_that_share_its_prefix(client):
    """`/in/jane-peer-42` must not be crowded out by `/in/jane-peer-42x0` … `x29`. The lookup is
    capped, so a prefix LIKE could fill the window with near-misses and create a duplicate."""
    token = await signup(client, slug="as12", email="o@as12.x", company="AS12")
    h = auth(token)
    acct = await _account(client, token, "Globex", "globex.com")
    tid = (decode_access_token(token) or {})["tid"]
    async with tenant_session(tid) as ts:
        ts.add_all([
            Contact(tenant_id=tid, account_id=acct["id"], full_name=f"Near Miss {i}",
                    linkedin_url=f"https://www.linkedin.com/in/jane-peer-42x{i}")
            for i in range(30)
        ])
        await ts.flush()
    first = (await client.post(URL, headers=h, json={**JANE, "account_id": acct["id"]})).json()
    assert first["created"] is True

    again = (await client.post(URL, headers=h, json={**JANE, "account_id": acct["id"]})).json()

    assert again["created"] is False and again["contact"]["id"] == first["contact"]["id"]


async def test_re_adding_a_deleted_person_restores_them(client):
    token = await signup(client, slug="as6", email="o@as6.x", company="AS6")
    h = auth(token)
    acct = await _account(client, token, "Globex", "globex.com")
    cid = (await client.post(URL, headers=h, json={**JANE, "account_id": acct["id"]})).json()["contact"]["id"]
    await client.delete(f"/api/contacts/{cid}", headers=h)

    body = (await client.post(URL, headers=h, json={**JANE, "account_id": acct["id"]})).json()

    assert body["contact"]["id"] == cid and body["created"] is False
    listed = (await client.get("/api/contacts", headers=h)).json()
    assert cid in [c["id"] for c in listed]


async def test_without_a_profile_the_person_is_deduped_by_name_on_that_account(client):
    token = await signup(client, slug="as7", email="o@as7.x", company="AS7")
    h = auth(token)
    acct = await _account(client, token, "Globex", "globex.com")
    no_url = {**JANE, "linkedin_url": None, "account_id": acct["id"]}
    first = (await client.post(URL, headers=h, json=no_url)).json()
    again = (await client.post(URL, headers=h, json={**no_url, "full_name": "  jane PEER "})).json()
    assert again["contact"]["id"] == first["contact"]["id"] and again["created"] is False


async def test_needs_an_account_or_a_company_name(client):
    h = auth(await signup(client, slug="as8", email="o@as8.x", company="AS8"))
    r = await client.post(URL, headers=h, json={**JANE, "company": ""})
    assert r.status_code == 422, r.text


async def test_cannot_add_into_another_workspaces_account(client):
    a = await signup(client, slug="as9a", email="o@as9a.x", company="AS9A")
    b = await signup(client, slug="as9b", email="o@as9b.x", company="AS9B")
    theirs = await _account(client, b, "Globex", "globex.com")
    r = await client.post(URL, headers=auth(a), json={**JANE, "account_id": theirs["id"]})
    assert r.status_code == 404


async def test_a_rep_can_add_people(client):
    owner = await signup(client, slug="as10", email="o@as10.x", company="AS10")
    await client.post("/api/workspace/members", headers=auth(owner), json={
        "email": "rep@as10.x", "full_name": "Rep", "password": "password123", "role": "rep"})
    rep = (await client.post("/api/auth/login",
                             json={"email": "rep@as10.x", "password": "password123"})).json()["access_token"]
    r = await client.post(URL, headers=auth(rep), json={**JANE, "new_account_name": "Globex"})
    assert r.status_code == 201, r.text


async def test_adding_is_free(client):
    """The search that found this person was already charged when its results were delivered.
    Writing the row the rep chose is not a second purchase."""
    token = await signup(client, slug="as11", email="o@as11.x", company="AS11")
    claims = decode_access_token(token) or {}
    r = await client.post(URL, headers=auth(token), json={**JANE, "new_account_name": "Globex"})
    assert r.status_code == 201, r.text

    async with tenant_session(claims["tid"]) as ts:
        charged = await ts.session.scalar(
            select(func.count()).select_from(BillingUsageEvent).where(
                BillingUsageEvent.tenant_id == ts.tenant_id,
                BillingUsageEvent.user_id == claims["sub"],
            )
        )
        contacts = await ts.list(Contact)
    # Background enrichment of the new account may meter on its own schedule, with no user; the
    # click itself charges the person who made it nothing.
    assert charged == 0
    assert len(contacts) == 1


def test_both_similar_people_views_offer_add_for_people_not_in_the_workspace():
    """Inline under the row, not a modal: on the Contacts page the results already sit in one, and
    a dialog on top of a dialog loses the list the rep was choosing from."""
    src = Path(__file__).resolve().parent.parent / "frontend" / "src"
    for rel in ("pages/ContactsPage.tsx", "pages/AccountDetailPage.tsx"):
        text = (src / rel).read_text(encoding="utf-8")
        assert "<AddSimilarPerson" in text, f"{rel} has no way to add a sourced person"
    api = (src / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "/accounts/contacts/from-lookalike" in api


def test_a_similar_person_is_called_and_emailed_at_their_own_account():
    """Results come from the whole workspace, so the person may work somewhere other than the page
    the rep is on. The call task and the draft must use THEIR account, not the page's."""
    text = (Path(__file__).resolve().parent.parent / "frontend" / "src" / "pages"
            / "AccountDetailPage.tsx").read_text(encoding="utf-8")
    start_call = text[text.index("async function startCall"):]
    start_call = start_call[: start_call.index("\n  }\n")]
    assert "account_id: c.account_id" in start_call
    assert "accountId={emailFor.account_id}" in text
