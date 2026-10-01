"""A mailbox connected with Google or Microsoft sends and saves drafts from the contact composer.

The composer read only the SMTP app-password mailboxes in `Tenant.email_settings`, so a rep who had
just pressed Connect Google on My mailboxes was told "you have not connected a sending mailbox".
These tests drive the real endpoints; the provider is `SentFolder`, the double installed through the
registry seam, and what Gmail and Graph do with the bytes is covered by `tests_live/engagement/`.
"""
from __future__ import annotations

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session
from tests.test_engagement_sending import SentFolder, message_from_bytes


@pytest.fixture
def folder():
    from nexus.engagement.mailboxes import registry

    box = SentFolder()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


async def _composer(client, slug: str, *, status: str = "connected", owner: str = "me",
                    smtp: bool = False):
    """A signed-up rep, a contact, and a Gmail mailbox connected by OAuth."""
    from nexus.models.engagement import MailboxConnection
    from nexus.models.identity import Tenant, User

    token = await signup(client, slug=slug, email=f"sam@{slug}.com", company=slug.title())
    me = principal_from_token(token)
    account = await client.post("/api/accounts", headers=auth(token),
                                json={"name": "Acme Robotics", "domain": "acme.io"})
    contact = await client.post(f"/api/accounts/{account.json()['id']}/contacts",
                                headers=auth(token),
                                json={"full_name": "Jane Buyer", "email": "jane@acme.io"})
    async with tenant_session(me.tenant_id) as ts:
        owner_id = me.user_id
        if owner != "me":
            colleague = User(email=f"other@{slug}.com", full_name="Other Rep", password_hash="x")
            ts.session.add(colleague)
            await ts.session.flush()
            owner_id = colleague.id
        mailbox = MailboxConnection(owner_user_id=owner_id, provider="google",
                                    email=f"sam@{slug}.com", display_name="Sam Rep",
                                    status=status, timezone="Europe/London",
                                    signature="Sam\nSDR, Seller Co")
        ts.add(mailbox)
        if smtp:
            tenant = await ts.session.get(Tenant, me.tenant_id)
            tenant.email_settings = {"accounts": [{
                "id": "smtp-1", "provider": "smtp", "host": "127.0.0.1", "port": 9,
                "username": f"sam@{slug}.com", "password": "app-password",
                "from_email": f"sam@{slug}.com", "owner_user_id": me.user_id, "enabled": True,
            }]}
        await ts.flush()
    return token, me, contact.json()["id"], mailbox.id


async def test_a_connected_gmail_mailbox_sends_from_the_composer(client, folder):
    from nexus.models.engagement import EngagementMessage

    token, me, contact_id, mailbox_id = await _composer(client, "oauthsend")
    r = await client.post(f"/api/contacts/{contact_id}/send-email", headers=auth(token),
                          json={"subject": "Quick question", "body": "Hi Jane,\n\nA real body."})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["from_email"] == "sam@oauthsend.com"

    sent = message_from_bytes(folder.delivered[0])
    assert sent["To"] == "jane@acme.io" and "sam@oauthsend.com" in sent["From"]
    # Plain text and HTML since 2026-09-30; CRLF on the wire, normalised here as a client would.
    text = sent.get_body(("plain",)).get_content().replace("\r\n", "\n")
    # The mailbox's own signature and the opt-out footer, as on every engine send.
    assert "Sam\nSDR, Seller Co" in text and "/api/u/" in text
    async with tenant_session(me.tenant_id) as ts:
        row = await ts.first(EngagementMessage, EngagementMessage.mailbox_connection_id == mailbox_id)
    # A real outbound row with a thread, so a reply to this email reaches the reply desk.
    assert (row.kind, row.status, row.direction) == ("oneoff", "sent", "out")
    assert row.thread_id


async def test_a_one_off_send_is_charged_once(client, folder, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.models.billing import BillingUsageEvent

    await sync_catalog()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    token, me, contact_id, _mailbox = await _composer(client, "oauthbill")
    r = await client.post(f"/api/contacts/{contact_id}/send-email", headers=auth(token),
                          json={"subject": "Hello", "body": "A real body."})
    assert r.json()["ok"] is True
    async with tenant_session(me.tenant_id) as ts:
        events = await ts.list(BillingUsageEvent,
                               BillingUsageEvent.capability_id == "outreach.email_send")
    # The engine's sender meters inside the provider call; the SMTP path's after-the-fact meter
    # must not run as well.
    assert len(events) == 1


async def test_it_saves_to_the_providers_drafts_and_charges_nothing(client, folder, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.models.billing import BillingUsageEvent

    await sync_catalog()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    token, me, contact_id, _mailbox = await _composer(client, "oauthdraft")
    r = await client.post(f"/api/contacts/{contact_id}/save-draft", headers=auth(token),
                          json={"subject": "For later", "body": "Hi Jane,\n\nOne to read first."})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["from_email"] == "sam@oauthdraft.com"
    assert folder.delivered == [] and len(folder.drafts) == 1
    draft = message_from_bytes(folder.drafts[0])
    plain = draft.get_body(("plain",)).get_content().replace("\r\n", "\n")
    assert draft["To"] == "jane@acme.io" and "Sam\nSDR, Seller Co" in plain
    async with tenant_session(me.tenant_id) as ts:
        assert await ts.list(BillingUsageEvent,
                             BillingUsageEvent.capability_id == "outreach.email_send") == []


async def test_a_connected_mailbox_is_preferred_to_an_smtp_one(client, folder):
    """The SMTP entry points at a closed local port: had the composer used it, nothing would reach
    the provider double and the send would report the socket error."""
    token, _me, contact_id, _mailbox = await _composer(client, "oauthfirst", smtp=True)
    r = await client.post(f"/api/contacts/{contact_id}/send-email", headers=auth(token),
                          json={"subject": "Hello", "body": "A real body."})
    assert r.json()["ok"] is True and len(folder.delivered) == 1


async def test_a_mailbox_that_needs_reconnecting_says_so(client, folder):
    token, _me, contact_id, _mailbox = await _composer(client, "oauthstale", status="needs_reauth")
    for path in ("send-email", "save-draft"):
        r = await client.post(f"/api/contacts/{contact_id}/{path}", headers=auth(token),
                              json={"subject": "Hello", "body": "A real body."})
        assert r.status_code == 409, r.text
        # The fix is one click on the mailbox they have, not a second mailbox.
        assert "reconnect" in r.text.lower() and "My mailboxes" in r.text
    assert folder.delivered == [] and folder.drafts == []


async def test_a_colleagues_connected_mailbox_is_never_used(client, folder):
    token, _me, contact_id, _mailbox = await _composer(client, "oauthother", owner="colleague")
    r = await client.post(f"/api/contacts/{contact_id}/send-email", headers=auth(token),
                          json={"subject": "Hello", "body": "A real body."})
    assert r.status_code == 409 and folder.delivered == []
    assert "My mailboxes" in r.text


async def test_do_not_contact_holds_on_a_one_off_send_too(client, folder):
    from nexus.engagement.suppression.service import suppress

    token, me, contact_id, _mailbox = await _composer(client, "oauthdnc")
    async with tenant_session(me.tenant_id) as ts:
        await suppress(ts, email="jane@acme.io", reason="unsubscribed", contact_id=contact_id)
    r = await client.post(f"/api/contacts/{contact_id}/send-email", headers=auth(token),
                          json={"subject": "Hello", "body": "A real body."})
    assert r.status_code == 422 and "do-not-contact" in r.text
    assert folder.delivered == []


async def test_an_expired_grant_is_a_failed_send_that_asks_for_a_reconnect(client, folder):
    from nexus.engagement.mailboxes.provider import AuthExpired
    from nexus.models.engagement import MailboxConnection

    token, me, contact_id, mailbox_id = await _composer(client, "oauthexpired")
    folder.fail_next = AuthExpired("invalid_grant")
    r = await client.post(f"/api/contacts/{contact_id}/send-email", headers=auth(token),
                          json={"subject": "Hello", "body": "A real body."})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "reconnect" in r.json()["detail"]
    async with tenant_session(me.tenant_id) as ts:
        assert (await ts.get(MailboxConnection, mailbox_id)).status == "needs_reauth"
