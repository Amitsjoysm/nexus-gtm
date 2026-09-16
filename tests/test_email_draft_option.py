# tests/test_email_draft_option.py
"""A rep chooses: put it in my Drafts, or send it now.

Asked for 2026-09-16. `save_to_drafts` (IMAP APPEND into the mailbox's Drafts folder) has existed
since the sender did and was reachable from one place — the orchestrator's tool — so a rep reading a
draft on screen had exactly one button that did anything with it.

Saving a draft is NOT a send, and the difference is load-bearing in two places:

* **It is not metered.** `outreach.email_send` prices a message that left the building. Charging for
  a draft would bill a customer for pressing save.
* **An unverified or invalid address does not block it.** The message is going into the rep's own
  Drafts folder for them to look at; refusing that protects nobody, and the send path still refuses.
"""
from __future__ import annotations

import pathlib

import pytest

from tests.conftest import auth, signup

SIGNATURE = "Jane Roe\nSDR, Acme"


def _mailbox(**over) -> dict:
    base = {
        "id": "m1", "provider": "gmail", "username": "rep@acme.com", "password": "app-password",
        "from_email": "rep@acme.com", "from_name": "Jane", "enabled": True, "default": True,
        "owner_user_id": "u1", "signature": SIGNATURE,
    }
    base.update(over)
    return base


async def _workspace_with_mailbox(slug: str, mailbox: dict | None = None):
    from nexus.models.account import Account, Contact
    from nexus.models.identity import Tenant
    from tests.conftest import make_tenant, tenant_session

    tid = await make_tenant(slug=slug, name=slug.upper())
    async with tenant_session(tid) as ts:
        tenant = await ts.session.get(Tenant, tid)
        tenant.email_settings = {"accounts": [mailbox or _mailbox()]}
        await ts.flush()
        account = Account(tenant_id=tid, name="Marketjoy")
        ts.add(account)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=account.id, full_name="Curtis Bent",
                          email="curtis@marketjoy.com", email_status="valid")
        ts.add(contact)
        await ts.flush()
    return tid


async def test_a_draft_is_appended_to_the_mailboxs_own_drafts_folder(monkeypatch):
    """The same shape trap the send path hit: `resolve_smtp` reads the credentials off the TOP
    level, so the mailbox dict itself is what the saver must be given."""
    from nexus.integrations import email_sender
    from nexus.models.account import Contact
    from nexus.outreach import send as send_mod
    from tests.conftest import tenant_session

    appended: dict = {}

    def fake_append(cfg, to, subject, body):
        appended.update({"cfg": cfg, "to": to, "subject": subject, "body": body})

    monkeypatch.setattr(email_sender, "_save_draft_blocking", fake_append)

    tid = await _workspace_with_mailbox("dr1")
    async with tenant_session(tid) as ts:
        contact = (await ts.list(Contact))[0]
        outcome = await send_mod.draft_for_contact(
            ts, contact=contact, subject="Quick question", body="Hi Curtis,\n\nShort body.",
            user_id="u1",
        )

    assert outcome.ok, outcome.detail
    assert appended["cfg"]["imap_host"] == "imap.gmail.com"
    assert appended["cfg"]["username"] == "rep@acme.com"
    assert appended["cfg"]["drafts_folder"] == "[Gmail]/Drafts"
    assert appended["to"] == "curtis@marketjoy.com"
    # Signed exactly like a send: what the rep opens in Gmail is what would have gone out.
    assert appended["body"].endswith(SIGNATURE)


async def test_saving_a_draft_is_not_billed(monkeypatch):
    """`outreach.email_send` prices a message that left. A draft has not left."""
    from nexus.integrations import email_sender
    from nexus.models.account import Contact
    from nexus.outreach import send as send_mod
    from tests.conftest import tenant_session

    monkeypatch.setattr(email_sender, "_save_draft_blocking", lambda *a, **k: None)
    charged: list[str] = []

    async def fake_meter(ts, *, user_id):
        charged.append(user_id)

    monkeypatch.setattr(send_mod, "_meter_send", fake_meter)

    tid = await _workspace_with_mailbox("dr2")
    async with tenant_session(tid) as ts:
        contact = (await ts.list(Contact))[0]
        await send_mod.draft_for_contact(
            ts, contact=contact, subject="S", body="Hi Curtis,\n\nB.", user_id="u1",
        )

    assert charged == []


async def test_a_doubtful_address_still_gets_a_draft(monkeypatch):
    """Nothing leaves, so there is nothing to protect the domain from — and a rep who wants to fix
    the address by hand needs the draft to exist first."""
    from nexus.integrations import email_sender
    from nexus.models.account import Contact
    from nexus.outreach import send as send_mod
    from tests.conftest import tenant_session

    monkeypatch.setattr(email_sender, "_save_draft_blocking", lambda *a, **k: None)

    tid = await _workspace_with_mailbox("dr3")
    async with tenant_session(tid) as ts:
        contact = (await ts.list(Contact))[0]
        contact.email_status = "invalid"
        await ts.flush()
        outcome = await send_mod.draft_for_contact(
            ts, contact=contact, subject="S", body="Hi Curtis,\n\nB.", user_id="u1",
        )
    assert outcome.ok


async def test_a_mailbox_that_cannot_hold_drafts_says_so(monkeypatch):
    """Custom SMTP has no IMAP host in its preset. "Saved" for something nobody can find is worse
    than a refusal that names the reason."""
    from nexus.models.account import Contact
    from nexus.outreach import send as send_mod
    from tests.conftest import tenant_session

    tid = await _workspace_with_mailbox("dr4", _mailbox(provider="smtp", host="mail.acme.com"))
    async with tenant_session(tid) as ts:
        contact = (await ts.list(Contact))[0]
        with pytest.raises(send_mod.SendRefused) as excinfo:
            await send_mod.draft_for_contact(
                ts, contact=contact, subject="S", body="Hi Curtis,\n\nB.", user_id="u1",
            )
    assert "imap" in str(excinfo.value).lower() or "drafts" in str(excinfo.value).lower()


async def test_the_endpoint_refuses_when_the_rep_has_no_mailbox(fresh_db, client):
    """Same 409 as sending: the request is fine, the state forbids it."""
    token = await signup(client, slug="dr5", email="rep@dr5.com", company="DR Five")
    accounts = await client.post(
        "/api/accounts", headers=auth(token), json={"name": "Marketjoy", "domain": "marketjoy.com"},
    )
    account_id = accounts.json()["id"]
    contact = await client.post(
        f"/api/accounts/{account_id}/contacts", headers=auth(token),
        json={"full_name": "Curtis Bent", "email": "curtis@marketjoy.com"},
    )
    r = await client.post(
        f"/api/contacts/{contact.json()['id']}/save-draft", headers=auth(token),
        json={"subject": "S", "body": "Hi Curtis,\n\nB."},
    )
    assert r.status_code == 409, r.text


def test_the_composer_offers_both_choices():
    src = pathlib.Path("frontend/src/components/EmailComposer.tsx").read_text(encoding="utf-8")
    assert "saveEmailDraft" in src, "the composer cannot save a draft"
    assert "Save to Drafts" in src
    assert "Send email" in src
