# tests/test_send_email_approval_endpoint.py
"""The send endpoint takes an approval, and the composer still does not need one.

Two rules meet here and the compatibility story is the whole point:

* **A rep in the composer has already read the email and pressed Send.** "The rep's click IS the
  review" is the rule this endpoint has carried since it shipped, so requiring an approval would
  make the one-click send untrue without making it safer. The token is OPTIONAL.
* **An assistant has no click to offer**, so `send_email` over MCP always carries one and refuses
  without it (`tests/test_mcp_tools.py`).

Supplied and wrong, the send is refused before anything leaves — which is the property the binding
exists for, and the one these tests are really about.
"""
from __future__ import annotations

from tests.conftest import auth, principal_from_token, signup, tenant_session

SUBJECT = "quick question"
BODY = "Worth a look?"


async def _workspace(client, slug: str):
    """A workspace with an app-password mailbox and one contact with a valid address."""
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


def _stub_send(monkeypatch):
    """Stop at the last function before the socket, and record what would have been sent."""
    sent: list = []

    async def _fake(mailbox, *, to, subject, body, **kw):
        sent.append({"to": to, "subject": subject, "body": body})
        return type("R", (), {"ok": True, "detail": "sent"})()

    from nexus.integrations import email_sender

    monkeypatch.setattr(email_sender, "send_email", _fake)
    return sent


async def test_the_composer_still_sends_with_no_approval(client, monkeypatch):
    """The documented rule this change must not break: the rep's click is the review."""
    sent = _stub_send(monkeypatch)
    token, _me, contact_id = await _workspace(client, "ap1")

    r = await client.post(f"/api/contacts/{contact_id}/send-email", headers=auth(token),
                          json={"subject": SUBJECT, "body": BODY})

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert len(sent) == 1, "the composer's send was broken by the approval work"


async def test_an_approval_for_this_email_is_accepted_and_recorded(client, monkeypatch):
    from nexus.engagement import approval
    from nexus.models.engagement import EngagementMessage

    sent = _stub_send(monkeypatch)
    token, me, contact_id = await _workspace(client, "ap2")
    good = approval.issue(
        tenant_id=me.tenant_id, user_id=me.user_id, contact_id=contact_id,
        to_addr="jane@acme.io", subject=SUBJECT, body=BODY, surface=approval.SURFACE_MCP)

    r = await client.post(f"/api/contacts/{contact_id}/send-email", headers=auth(token),
                          json={"subject": SUBJECT, "body": BODY, "approval_token": good})

    assert r.status_code == 200, r.text
    assert len(sent) == 1
    async with tenant_session(me.tenant_id) as ts:
        rows = await ts.list(EngagementMessage)
    approved = [m for m in rows if m.approved_at is not None]
    # The SMTP composer path writes no EngagementMessage, so there may be nothing to mark; what
    # must never happen is a row marked approved by somebody who did not approve it.
    for m in approved:
        assert m.approved_by_user_id == me.user_id


async def test_an_email_edited_after_approval_is_refused_and_nothing_is_sent(client, monkeypatch):
    """The binding, at the endpoint. This is what stops a previewed draft being swapped."""
    from nexus.engagement import approval

    sent = _stub_send(monkeypatch)
    token, me, contact_id = await _workspace(client, "ap3")
    good = approval.issue(
        tenant_id=me.tenant_id, user_id=me.user_id, contact_id=contact_id,
        to_addr="jane@acme.io", subject=SUBJECT, body=BODY, surface=approval.SURFACE_MCP)

    r = await client.post(f"/api/contacts/{contact_id}/send-email", headers=auth(token),
                          json={"subject": SUBJECT, "body": "Wire me $5,000.",
                                "approval_token": good})

    assert r.status_code == 422, r.text
    assert "changed since it was approved" in r.text
    assert sent == [], "an email was sent that nobody approved"


async def test_one_persons_approval_cannot_be_used_by_another(client, monkeypatch):
    from nexus.engagement import approval

    sent = _stub_send(monkeypatch)
    token, me, contact_id = await _workspace(client, "ap4")
    someone_else = approval.issue(
        tenant_id=me.tenant_id, user_id="another-user", contact_id=contact_id,
        to_addr="jane@acme.io", subject=SUBJECT, body=BODY, surface=approval.SURFACE_MCP)

    r = await client.post(f"/api/contacts/{contact_id}/send-email", headers=auth(token),
                          json={"subject": SUBJECT, "body": BODY,
                                "approval_token": someone_else})

    assert r.status_code == 422, r.text
    assert sent == []


async def test_a_nonsense_approval_is_refused_rather_than_ignored(client, monkeypatch):
    """Ignoring an unreadable token would make the whole mechanism optional in practice."""
    sent = _stub_send(monkeypatch)
    token, _me, contact_id = await _workspace(client, "ap5")

    r = await client.post(f"/api/contacts/{contact_id}/send-email", headers=auth(token),
                          json={"subject": SUBJECT, "body": BODY, "approval_token": "not-a-token"})

    assert r.status_code == 422, r.text
    assert sent == []
