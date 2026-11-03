"""Add one contact from the Contacts page (SDR round 2, 5.4).

Only "Add similar person" could put a single person in by hand. `POST /accounts/contacts` takes a
name, email, title, phone and LinkedIn profile, and an account the workspace has or a company named
by its website. It reuses the contact write of `POST /accounts/{id}/contacts` (the profile must be
the person's own) and the account find-or-create of the similar-person add (domain first).
"""
from __future__ import annotations

from tests.conftest import auth, principal_from_token, signup, tenant_session


async def _workspace(client, slug):
    token = await signup(client, slug=slug, email=f"owner@{slug}.io", company=slug.upper())
    return auth(token), principal_from_token(token).tenant_id


async def _account(tid: str, name: str, domain: str | None, *, archived: bool = False) -> str:
    from nexus.models.account import Account

    async with tenant_session(tid) as ts:
        a = Account(tenant_id=tid, name=name, domain=domain)
        if archived:
            a.set_archived(True)
        ts.add(a)
        await ts.flush()
        return a.id


def _person(**over) -> dict:
    person = {"full_name": "Sam Porter", "email": "Sam.Porter@Globex.io", "title": "COO",
              "phone": "+1 415 555 0100",
              "linkedin_url": "https://www.linkedin.com/in/sam-porter-globex"}
    person.update(over)
    return person


async def test_a_contact_goes_on_an_account_the_workspace_has(client):
    from nexus.models.account import Contact

    h, tid = await _workspace(client, "ac1")
    account_id = await _account(tid, "Globex", "globex.io")
    r = await client.post("/api/accounts/contacts", headers=h,
                          json={"contact": _person(), "account_id": account_id})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True and body["account_created"] is False
    assert body["account_id"] == account_id

    async with tenant_session(tid) as ts:
        c = await ts.get(Contact, body["contact"]["id"])
        assert c.email == "sam.porter@globex.io"
        assert c.title == "COO" and c.phone == "+1 415 555 0100"
        assert c.linkedin_url.startswith("https://www.linkedin.com/in/sam-porter-globex")
        # The same provenance a typed contact has always had.
        assert (c.field_sources or {}).get("person", {}).get("source") == "manual"


async def test_a_new_company_is_named_by_its_website_and_reuses_the_one_already_held(client):
    from nexus.models.account import Account

    h, tid = await _workspace(client, "ac2")
    held = await _account(tid, "Globex Corporation", "globex.io", archived=True)

    r = await client.post("/api/accounts/contacts", headers=h, json={
        "contact": _person(), "new_account_domain": "https://www.Globex.io/about",
        "new_account_name": "Globex"})
    assert r.status_code == 201, r.text
    assert r.json()["account_id"] == held and r.json()["account_created"] is False

    fresh = await client.post("/api/accounts/contacts", headers=h, json={
        "contact": _person(full_name="Ann Able", email="ann@initech.io", linkedin_url=None),
        "new_account_domain": "initech.io"})
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["account_created"] is True

    async with tenant_session(tid) as ts:
        globex = await ts.get(Account, held)
        assert globex.archived_at is None, "an archived match comes back into the list"
        initech = await ts.get(Account, fresh.json()["account_id"])
        assert initech.domain == "initech.io" and initech.name == "initech.io"
        assert initech.owner_user_id == principal_from_token(h["Authorization"][7:]).user_id


async def test_the_same_person_is_not_added_twice(client):
    h, tid = await _workspace(client, "ac3")
    account_id = await _account(tid, "Globex", "globex.io")
    first = await client.post("/api/accounts/contacts", headers=h,
                              json={"contact": _person(), "account_id": account_id})
    contact_id = first.json()["contact"]["id"]

    by_email = await client.post("/api/accounts/contacts", headers=h, json={
        "contact": _person(linkedin_url=None, email="SAM.PORTER@globex.io"),
        "account_id": account_id})
    assert by_email.status_code == 200
    assert by_email.json()["created"] is False and by_email.json()["contact"]["id"] == contact_id

    await client.delete(f"/api/contacts/{contact_id}", headers=h)
    by_profile = await client.post("/api/accounts/contacts", headers=h, json={
        "contact": _person(email="sam@elsewhere.io"), "new_account_domain": "elsewhere.io"})
    assert by_profile.status_code == 200 and by_profile.json()["contact"]["id"] == contact_id
    listed = await client.get("/api/contacts", headers=h)
    assert contact_id in {c["id"] for c in listed.json()}, "a deleted match is restored"


async def test_what_is_refused_is_refused_before_anything_is_written(client):
    from nexus.models.account import Account, Contact

    h, tid = await _workspace(client, "ac4")
    cases = [
        ({"contact": _person(linkedin_url="https://www.linkedin.com/company/globex"),
          "new_account_domain": "globex.io"}, "not a LinkedIn profile"),
        ({"contact": _person(email="not-an-address"), "new_account_domain": "globex.io"},
         "not an email"),
        ({"contact": _person(full_name="   "), "new_account_domain": "globex.io"}, "name"),
        ({"contact": _person(), "new_account_domain": "globex"}, "website"),
        ({"contact": _person()}, "website"),
    ]
    for body, said in cases:
        r = await client.post("/api/accounts/contacts", headers=h, json=body)
        assert r.status_code == 422, (body, r.text)
        assert said in r.text
    async with tenant_session(tid) as ts:
        assert await ts.list(Account) == [] and await ts.list(Contact) == []


async def test_another_workspace_s_account_cannot_be_chosen(client):
    h1, tid1 = await _workspace(client, "ac5a")
    h2, _ = await _workspace(client, "ac5b")
    theirs = await _account(tid1, "Globex", "globex.io")
    r = await client.post("/api/accounts/contacts", headers=h2,
                          json={"contact": _person(), "account_id": theirs})
    assert r.status_code == 404


async def test_the_account_page_s_add_contact_writes_through_the_same_code(client):
    """`POST /accounts/{id}/contacts` keeps its contract after the shared write moved out."""
    h, tid = await _workspace(client, "ac6")
    account_id = await _account(tid, "Globex", "globex.io")
    r = await client.post(f"/api/accounts/{account_id}/contacts", headers=h,
                          json={"full_name": "Ann Able",
                                "linkedin_url": "https://www.linkedin.com/company/globex"})
    assert r.status_code == 422
    ok = await client.post(f"/api/accounts/{account_id}/contacts", headers=h,
                           json={"full_name": "Ann Able", "email": "ann@globex.io"})
    assert ok.status_code == 201 and ok.json()["account_id"] == account_id
