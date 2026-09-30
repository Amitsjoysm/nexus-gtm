"""Do-not-contact and one-click unsubscribe (D7, D11, RFC 8058)."""
from __future__ import annotations

import pathlib

import pytest

from tests.conftest import auth, principal_from_token, signup, tenant_session


async def _workspace(client, slug: str):
    token = await signup(client, slug=slug, email=f"owner@{slug}co.com", company=slug.title())
    return token, principal_from_token(token)


async def _contact(tenant_id: str, email: str = "Jane@Acme.io") -> str:
    from nexus.models.account import Account, Contact

    async with tenant_session(tenant_id) as ts:
        account = Account(name="Acme", domain="acme.io")
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email=email)
        ts.add(contact)
        await ts.flush()
        return contact.id


# ---- tokens -------------------------------------------------------------------------------------

def test_a_token_identifies_its_contact_and_refuses_any_change():
    from nexus.engagement.suppression.tokens import make_token, read_token

    tenant, contact = "a" * 32, "b" * 32
    token = make_token(tenant, contact)
    assert read_token(token) == (tenant, contact)
    assert "@" not in token
    assert read_token(token.replace(contact, "c" * 32)) is None
    assert read_token(token[:-1] + ("0" if token[-1] != "0" else "1")) is None
    assert read_token("garbage") is None


# ---- service ------------------------------------------------------------------------------------

async def test_blocking_is_idempotent_and_an_unsubscribe_makes_a_no_permanent(client):
    from nexus.engagement.suppression.service import PermanentBlock, active_reasons, lift, suppress

    _token, me = await _workspace(client, "idem")
    async with tenant_session(me.tenant_id) as ts:
        first = await suppress(ts, email=" Jane@Acme.io ", reason="declined")
        again = await suppress(ts, email="jane@acme.io", reason="manual")
        assert again.id == first.id and again.reason == "declined"
        upgraded = await suppress(ts, email="jane@acme.io", reason="unsubscribed")
        assert upgraded.id == first.id and upgraded.reason == "unsubscribed"
        with pytest.raises(PermanentBlock):
            await lift(ts, upgraded, user_id=me.user_id, note="they asked on a call")
        assert await active_reasons(ts, ["JANE@acme.io", "other@acme.io"]) == {
            "jane@acme.io": "unsubscribed"}


async def test_a_lift_needs_a_note_and_the_address_can_be_blocked_again(client):
    from nexus.engagement.suppression.service import active_block, lift, suppress
    from nexus.models.audit import AuditLog

    _token, me = await _workspace(client, "lift")
    async with tenant_session(me.tenant_id) as ts:
        block = await suppress(ts, email="sam@acme.io", reason="bounced")
        with pytest.raises(ValueError, match="Say why"):
            await lift(ts, block, user_id=me.user_id, note="ok")
        await lift(ts, block, user_id=me.user_id, note="address was fixed by their IT")
        assert await active_block(ts, "sam@acme.io") is None
        again = await suppress(ts, email="sam@acme.io", reason="manual")
        assert again.id != block.id
        actions = {row.action for row in await ts.list(AuditLog)}
        assert {"engagement.dnc.add", "engagement.dnc.lift"} <= actions


# ---- public one-click endpoint ------------------------------------------------------------------

async def test_a_get_shows_a_button_and_blocks_nobody(client):
    from nexus.engagement.suppression.service import active_block
    from nexus.engagement.suppression.tokens import make_token

    _token, me = await _workspace(client, "getpage")
    contact_id = await _contact(me.tenant_id)
    r = await client.get(f"/api/u/{make_token(me.tenant_id, contact_id)}")
    assert r.status_code == 200
    assert "<form method=\"post\"" in r.text and "Getpage" in r.text
    assert r.headers["cache-control"] == "no-store"
    async with tenant_session(me.tenant_id) as ts:
        assert await active_block(ts, "jane@acme.io") is None


async def test_a_one_click_post_unsubscribes_permanently_and_twice_is_harmless(client):
    from nexus.engagement.suppression.service import active_block
    from nexus.engagement.suppression.tokens import make_token

    _token, me = await _workspace(client, "oneclick")
    contact_id = await _contact(me.tenant_id)
    url = f"/api/u/{make_token(me.tenant_id, contact_id)}"
    for _ in range(2):
        r = await client.post(url, content="List-Unsubscribe=One-Click",
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 200 and "You are unsubscribed" in r.text
    async with tenant_session(me.tenant_id) as ts:
        block = await active_block(ts, "jane@acme.io")
        assert block is not None and block.reason == "unsubscribed"


async def test_a_forged_token_gets_a_neutral_page(client):
    r = await client.post(f"/api/u/v1.{'a' * 32}.{'b' * 32}.{'c' * 32}")
    assert r.status_code == 404 and "not valid" in r.text


# ---- tenant API ---------------------------------------------------------------------------------

async def test_members_add_and_check_and_only_managers_lift_with_a_note(client):
    from nexus.core.security import create_access_token

    owner, me = await _workspace(client, "dncapi")
    rep = create_access_token(user_id="rep-user", tenant_id=me.tenant_id, role="rep")

    added = await client.post("/api/engagement/do-not-contact", json={"email": "Pat@Acme.io"},
                              headers=auth(rep))
    assert added.status_code == 201 and added.json()["reason"] == "manual"
    assert added.json()["liftable"] is True

    check = await client.post("/api/engagement/do-not-contact/check",
                              json={"emails": ["pat@acme.io", "nobody@acme.io"]},
                              headers=auth(rep))
    assert check.json() == {"pat@acme.io": "manual"}

    block_id = added.json()["id"]
    assert (await client.post(f"/api/engagement/do-not-contact/{block_id}/lift",
                              json={"note": "they asked us to follow up"},
                              headers=auth(rep))).status_code == 403
    lifted = await client.post(f"/api/engagement/do-not-contact/{block_id}/lift",
                               json={"note": "they asked us to follow up"}, headers=auth(owner))
    assert lifted.status_code == 200 and lifted.json()["lifted_at"]

    listed = await client.get("/api/engagement/do-not-contact", headers=auth(rep))
    assert listed.json() == []
    history = await client.get("/api/engagement/do-not-contact",
                               params={"include_lifted": True}, headers=auth(rep))
    assert [b["email"] for b in history.json()] == ["pat@acme.io"]


async def test_an_unsubscribe_cannot_be_lifted_through_the_api(client):
    from nexus.engagement.suppression.service import suppress

    owner, me = await _workspace(client, "perm")
    async with tenant_session(me.tenant_id) as ts:
        block = await suppress(ts, email="gone@acme.io", reason="unsubscribed")
        block_id = block.id
    r = await client.post(f"/api/engagement/do-not-contact/{block_id}/lift",
                          json={"note": "please let us email again"}, headers=auth(owner))
    assert r.status_code == 409


# ---- domains and bulk upload (2026-09-30) -------------------------------------------------------

def test_an_entry_is_read_as_an_address_or_a_domain_or_refused():
    from nexus.engagement.suppression.service import classify_entry

    assert classify_entry(" Jane@Acme.io ") == ("email", "jane@acme.io")
    assert classify_entry("acme.io") == ("domain", "acme.io")
    assert classify_entry("@Acme.io") == ("domain", "acme.io")
    assert classify_entry("https://www.acme.io/about") == ("domain", "acme.io")
    for junk in ("", "email", "domain", "acme", "jane@", "@", "not an address", "a@b"):
        assert classify_entry(junk)[0] is None, junk


async def test_a_blocked_domain_blocks_everyone_there_and_its_subdomains(client):
    from nexus.engagement.suppression.service import active_block, active_reasons, block_entry

    _token, me = await _workspace(client, "dom")
    async with tenant_session(me.tenant_id) as ts:
        row, created = await block_entry(ts, "acme.io", created_by_user_id=me.user_id)
        assert created and row.email == "@acme.io" and row.reason == "manual"
        assert (await active_block(ts, "anyone@acme.io")).id == row.id
        assert (await active_block(ts, "sam@eu.acme.io")).id == row.id
        assert await active_block(ts, "sam@notacme.io") is None
        assert await active_reasons(ts, ["Pat@Acme.io", "x@other.io"]) == {"pat@acme.io": "manual"}
        again, created = await block_entry(ts, "@ACME.io")
        assert not created and again.id == row.id


async def test_one_unsubscribe_under_a_blocked_domain_does_not_make_the_domain_permanent(client):
    from nexus.engagement.suppression.service import active_block, block_entry, lift, suppress

    _token, me = await _workspace(client, "domperm")
    async with tenant_session(me.tenant_id) as ts:
        domain, _ = await block_entry(ts, "acme.io")
        person = await suppress(ts, email="jane@acme.io", reason="unsubscribed")
        assert person.id != domain.id and domain.reason == "manual"
        await lift(ts, domain, user_id=me.user_id, note="we signed a partnership")
        assert await active_block(ts, "sam@acme.io") is None
        assert (await active_block(ts, "jane@acme.io")).reason == "unsubscribed"


async def test_a_blocked_domain_marks_campaign_candidates_blocked(client):
    from nexus.engagement.sequences.candidates import candidates
    from nexus.engagement.suppression.service import block_entry

    _token, me = await _workspace(client, "domcand")
    await _contact(me.tenant_id, email="jane@acme.io")
    async with tenant_session(me.tenant_id) as ts:
        await block_entry(ts, "acme.io")
        [candidate] = await candidates(ts)
        assert candidate.blocked is True


async def test_an_uploaded_list_blocks_addresses_and_domains_and_reports_the_rest(client):
    token, _me = await _workspace(client, "dncbulk")
    r = await client.post("/api/engagement/do-not-contact/bulk", headers=auth(token), json={
        "entries": ["email", "pat@acme.io", "PAT@acme.io", "globex.com", "@initech.com",
                    "not an address", "pat@acme.io"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["emails_blocked"], body["domains_blocked"], body["already_blocked"]) == (1, 2, 0)
    assert body["unreadable"] == ["email", "not an address"]

    again = await client.post("/api/engagement/do-not-contact/bulk", headers=auth(token),
                              json={"entries": ["globex.com", "new@acme.io"]})
    assert again.json()["already_blocked"] == 1 and again.json()["emails_blocked"] == 1

    listed = await client.get("/api/engagement/do-not-contact", headers=auth(token))
    kinds = {b["email"]: b["kind"] for b in listed.json()}
    assert kinds["@globex.com"] == "domain" and kinds["pat@acme.io"] == "email"

    one = await client.post("/api/engagement/do-not-contact", headers=auth(token),
                            json={"email": "hooli.com"})
    assert one.status_code == 201 and one.json()["kind"] == "domain"


async def test_an_upload_is_capped_per_request(client):
    token, _me = await _workspace(client, "dnccap")
    r = await client.post("/api/engagement/do-not-contact/bulk", headers=auth(token),
                          json={"entries": [f"p{i}@acme.io" for i in range(1001)]})
    assert r.status_code == 422


# ---- UI -----------------------------------------------------------------------------------------

SRC = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"


def test_the_list_page_takes_an_uploaded_list_of_addresses_or_domains():
    page = (SRC / "pages/engagement/DoNotContactPage.tsx").read_text(encoding="utf-8")
    assert "api.bulkDoNotContact(" in page
    assert 'type="file"' in page


def test_the_list_page_and_the_contact_badge_are_wired():
    app = (SRC / "App.tsx").read_text(encoding="utf-8")
    assert 'path="/do-not-contact"' in app
    page = (SRC / "pages/engagement/DoNotContactPage.tsx").read_text(encoding="utf-8")
    assert "api.liftDoNotContact" in page and "b.liftable" in page
    assert "note.trim().length < 5" in page, "a lift must not be sendable without a note"
    contacts = (SRC / "pages/ContactsPage.tsx").read_text(encoding="utf-8")
    assert "useDoNotContact(" in contacts and "<DncBadge" in contacts
    mailboxes = (SRC / "pages/engagement/MailboxesPage.tsx").read_text(encoding="utf-8")
    assert 'to="/do-not-contact"' in mailboxes
