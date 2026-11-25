# tests/test_send_preview.py
"""The preview is the email, not an impression of it.

The composer showed the body the rep wrote and then said "your signature is added when this goes
out" — an admission that what was on screen was not what the buyer receives. The signature is the
part carrying the rep's real name, title and phone number, so a rep approving an email they have
not fully seen is approving a guess.

The property worth testing is not that a preview renders. It is that the preview and the send
compose the SAME text: the preview shares the send's mailbox resolution and signature composition
rather than reproducing them, because a second source of truth for what the buyer reads would
drift, and the first thing to drift would be the signature.
"""
from __future__ import annotations

from tests.conftest import auth, principal_from_token, signup, tenant_session

SIGNATURE = "Sam Seller\nSDR, Acme\n+44 20 7946 0000"
BODY = "Saw the Series B. Worth a look?"


async def _workspace(client, slug: str):
    from nexus.models.account import Account, Contact
    from nexus.models.identity import Tenant

    token = await signup(client, slug=slug, email=f"sam@{slug}.com", company=slug.title())
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        tenant = await ts.session.get(Tenant, me.tenant_id)
        tenant.email_settings = {"accounts": [{
            "id": "m1", "provider": "gmail", "username": f"sam@{slug}.com",
            "password": "app-password", "from_email": f"sam@{slug}.com", "from_name": "Sam",
            "enabled": True, "default": True, "owner_user_id": me.user_id,
            "signature": SIGNATURE,
        }]}
        await ts.flush()
        account = Account(name="Acme", domain="acme.io")
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer",
                          email="jane@acme.io", email_status="valid")
        ts.add(contact)
        await ts.flush()
        contact_id = contact.id
    return token, me, contact_id


async def test_the_preview_shows_the_signature_the_composer_only_promised(client):
    token, _me, contact_id = await _workspace(client, "pv1")

    r = await client.post(f"/api/contacts/{contact_id}/preview-email", headers=auth(token),
                          json={"subject": "quick question", "body": BODY})

    assert r.status_code == 200, r.text
    out = r.json()
    assert out["to"] == "jane@acme.io"
    assert BODY in out["body"]
    assert "Sam Seller" in out["body"], "the preview omits the signature it exists to show"
    assert "+44 20 7946 0000" in out["body"]
    assert out["signed"] is True
    assert out["mailbox_kind"] == "smtp"


async def test_the_preview_is_exactly_what_the_send_composes(client, monkeypatch):
    """The one that matters. If these two ever differ, the preview is a lie."""
    sent: list = []

    async def _fake(mailbox, *, to, subject, body, **kw):
        sent.append(body)
        return type("R", (), {"ok": True, "detail": "sent"})()

    from nexus.integrations import email_sender

    monkeypatch.setattr(email_sender, "send_email", _fake)
    token, _me, contact_id = await _workspace(client, "pv2")

    preview = (await client.post(f"/api/contacts/{contact_id}/preview-email", headers=auth(token),
                                 json={"subject": "quick question", "body": BODY})).json()
    r = await client.post(f"/api/contacts/{contact_id}/send-email", headers=auth(token),
                          json={"subject": "quick question", "body": BODY})

    assert r.status_code == 200, r.text
    assert len(sent) == 1
    assert sent[0] == preview["body"], (
        "the rep approved one email and another was sent:\n"
        f"previewed: {preview['body']!r}\nsent:      {sent[0]!r}"
    )


async def test_previewing_sends_nothing_and_charges_nothing(client, monkeypatch):
    sent: list = []

    async def _fake(mailbox, **kw):
        sent.append(kw)
        return type("R", (), {"ok": True, "detail": "sent"})()

    from nexus.integrations import email_sender
    from nexus.models.billing import BillingUsageEvent

    monkeypatch.setattr(email_sender, "send_email", _fake)
    token, me, contact_id = await _workspace(client, "pv3")

    r = await client.post(f"/api/contacts/{contact_id}/preview-email", headers=auth(token),
                          json={"subject": "quick question", "body": BODY})

    assert r.status_code == 200
    assert sent == [], "previewing sent an email"
    async with tenant_session(me.tenant_id) as ts:
        events = await ts.list(BillingUsageEvent)
    assert [e.capability_id for e in events if e.capability_id == "outreach.email_send"] == []


async def test_a_doubtful_address_can_still_be_previewed(client):
    """The risky refusal belongs to sending. Refusing to RENDER would leave a rep guessing at
    wording they are allowed to fix."""
    from nexus.models.account import Contact

    token, me, contact_id = await _workspace(client, "pv4")
    async with tenant_session(me.tenant_id) as ts:
        contact = await ts.session.get(Contact, contact_id)
        contact.email_status = "invalid"
        await ts.flush()

    r = await client.post(f"/api/contacts/{contact_id}/preview-email", headers=auth(token),
                          json={"subject": "quick question", "body": BODY})

    assert r.status_code == 200, r.text
    # And the send still refuses it.
    s = await client.post(f"/api/contacts/{contact_id}/send-email", headers=auth(token),
                          json={"subject": "quick question", "body": BODY})
    assert s.status_code == 422


async def test_an_empty_draft_is_refused_rather_than_previewed_blank(client):
    token, _me, contact_id = await _workspace(client, "pv5")

    r = await client.post(f"/api/contacts/{contact_id}/preview-email", headers=auth(token),
                          json={"subject": "quick question", "body": "   "})

    assert r.status_code == 422, r.text
    assert "empty" in r.text.lower()


async def test_an_unknown_contact_is_a_404(client):
    token, _me, _contact_id = await _workspace(client, "pv6")

    r = await client.post("/api/contacts/nope/preview-email", headers=auth(token),
                          json={"subject": "x", "body": BODY})

    assert r.status_code == 404
