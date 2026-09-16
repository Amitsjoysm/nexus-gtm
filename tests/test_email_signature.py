# tests/test_email_signature.py
"""A rep's signature reaches the email they send.

Reported 2026-09-16: drafts arrive with no signature and no way to add one. There was none to add:
`signature` appeared nowhere in the backend, so every send left the rep's name, title and phone off
a message written to a buyer.

A signature belongs to the MAILBOX, because the mailbox is what identifies the sender: a reply goes
back to whoever sent it (`nexus/outreach/send.py`). The workspace default exists so a rep who has
not written one still signs off as the company rather than anonymously.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup

SIGNATURE = "Jane Roe\nSDR, Acme\n+1 415 555 2671"
WORKSPACE_SIGNATURE = "The Acme team\nacme.com"


# ---- resolution ---------------------------------------------------------------------------------

def test_the_mailbox_signature_wins_over_the_workspace_default():
    from nexus.outreach.signature import resolve_signature

    settings = {"default_signature": WORKSPACE_SIGNATURE}
    assert resolve_signature(settings, {"signature": SIGNATURE}) == SIGNATURE


def test_a_mailbox_without_one_falls_back_to_the_workspace_default():
    from nexus.outreach.signature import resolve_signature

    settings = {"default_signature": WORKSPACE_SIGNATURE}
    assert resolve_signature(settings, {"signature": ""}) == WORKSPACE_SIGNATURE


def test_no_signature_anywhere_is_empty_not_an_error():
    from nexus.outreach.signature import resolve_signature

    assert resolve_signature({}, {}) == ""


# ---- appending ----------------------------------------------------------------------------------

def test_the_signature_is_appended_below_the_standard_delimiter():
    """`-- ` on its own line is the RFC 3676 sign-off marker: mail clients hide or grey what
    follows it, so a signature added any other way reads as part of the message."""
    from nexus.outreach.signature import append_signature

    out = append_signature("Hi Curtis,\n\nShort body.\n\nBest,\nJane", SIGNATURE)
    assert out.endswith(SIGNATURE)
    assert "\n-- \n" in out


def test_appending_twice_does_not_sign_twice():
    """The composer shows the signature and the send path adds it. Without this the buyer gets the
    rep's phone number twice."""
    from nexus.outreach.signature import append_signature

    once = append_signature("Body", SIGNATURE)
    assert append_signature(once, SIGNATURE) == once


def test_a_rep_who_typed_their_own_sign_off_is_not_given_a_second_one():
    from nexus.outreach.signature import append_signature

    body = f"Hi Curtis,\n\nShort body.\n\n{SIGNATURE}"
    assert append_signature(body, SIGNATURE) == body


def test_an_empty_signature_leaves_the_body_untouched():
    from nexus.outreach.signature import append_signature

    assert append_signature("Body", "") == "Body"


# ---- the API ------------------------------------------------------------------------------------

async def test_a_rep_can_save_a_signature_on_their_mailbox_and_read_it_back(client):
    token = await signup(client, slug="sig1", email="rep@sig1.com", company="Sig One")
    created = await client.post(
        "/api/workspace/email/accounts", headers=auth(token),
        json={"provider": "gmail", "username": "rep@sig1.com", "password": "app-password",
              "from_name": "Jane", "signature": SIGNATURE},
    )
    assert created.status_code == 201, created.text
    assert created.json()["signature"] == SIGNATURE

    listed = (await client.get("/api/workspace/email/accounts", headers=auth(token))).json()
    assert listed[0]["signature"] == SIGNATURE


async def test_the_workspace_default_signature_round_trips(client):
    token = await signup(client, slug="sig2", email="rep@sig2.com", company="Sig Two")
    saved = await client.put(
        "/api/workspace/email/style", headers=auth(token),
        json={"default_signature": WORKSPACE_SIGNATURE},
    )
    assert saved.status_code == 200, saved.text
    read = (await client.get("/api/workspace/email/style", headers=auth(token))).json()
    assert read["default_signature"] == WORKSPACE_SIGNATURE


async def test_a_signature_is_never_html_yet(client):
    """Plain text now, HTML later (decided with the product owner 2026-09-16). Accepting markup
    today would send raw tags to a buyer, because the sender writes a text part."""
    token = await signup(client, slug="sig3", email="rep@sig3.com", company="Sig Three")
    r = await client.put(
        "/api/workspace/email/style", headers=auth(token),
        json={"default_signature": "<b>Jane</b><br/>Acme"},
    )
    assert r.status_code == 400
    assert "plain text" in r.text.lower()


# ---- the send path ------------------------------------------------------------------------------

async def test_the_sent_email_carries_the_signature(monkeypatch):
    """The point of the feature: what leaves the building is signed."""
    from nexus.integrations import email_sender
    from nexus.models.identity import Tenant
    from nexus.outreach import send as send_mod
    from tests.conftest import make_tenant, tenant_session

    sent: dict = {}

    async def fake_send(settings, *, to, subject, body, html=None):
        sent.update({"to": to, "subject": subject, "body": body})
        return email_sender.SendResult(True, "sent")

    monkeypatch.setattr(send_mod, "send_email", fake_send, raising=False)
    monkeypatch.setattr(email_sender, "send_email", fake_send)

    tid = await make_tenant(slug="sig4", name="Sig Four")
    async with tenant_session(tid) as ts:
        tenant = await ts.session.get(Tenant, tid)
        tenant.email_settings = {
            "default_signature": WORKSPACE_SIGNATURE,
            "accounts": [{
                "id": "m1", "provider": "gmail", "username": "rep@sig4.com",
                "password": "app-password", "from_email": "rep@sig4.com", "from_name": "Jane",
                "enabled": True, "default": True, "owner_user_id": "u1",
                "signature": SIGNATURE,
            }],
        }
        await ts.flush()

        from nexus.models.account import Account, Contact

        account = Account(tenant_id=tid, name="Marketjoy")
        ts.add(account)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=account.id, full_name="Curtis Bent",
                          email="curtis@marketjoy.com", email_status="valid")
        ts.add(contact)
        await ts.flush()

        outcome = await send_mod.send_to_contact(
            ts, contact=contact, subject="Quick question", body="Hi Curtis,\n\nShort body.",
            user_id="u1",
        )

    assert outcome.ok, outcome.detail
    assert sent["body"].endswith(SIGNATURE)


@pytest.mark.parametrize("stored", [{"signature": SIGNATURE}, {}])
def test_resolution_never_raises_on_a_malformed_mailbox(stored):
    """`email_settings` is a JSON blob an operator can edit. A bad shape must cost the signature,
    not the send."""
    from nexus.outreach.signature import resolve_signature

    assert isinstance(resolve_signature(None, stored), str)
    assert isinstance(resolve_signature({"default_signature": None}, stored), str)
