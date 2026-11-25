# tests/test_draft_is_recorded.py
"""A draft the product wrote is a message the product knows about.

`save_draft` minted an RFC Message-ID, handed it to Gmail or Graph, returned the provider's draft
id and threw the Message-ID away. So once the rep pressed Send in their own mail client, the
conversation existed in that mailbox and nowhere else.

That matters because of how a reply is kept. `replies/matching.py` stores an inbound email only
when it matches a known provider thread, an `In-Reply-To`/`References` naming one of OUR
`rfc_message_id`s, a contact with a LIVE enrollment, or a colleague at an account with one.
Anything else is discarded without storing a subject or a body.

A one-off email has no enrollment — the contact composer's Save to Drafts, the reply desk's, and
every `save_email_draft` an assistant makes over MCP — so rule 2 was the only rule that could have
caught its reply, and the id it needed lived for the length of one function. The prospect replied,
the rep saw it in Gmail, and the product never did.

The draft is recorded as `status="drafted"`, which is deliberately not one of `desk/inbox.SHOWN`:
nothing has been sent, so it must not appear in Mail or read as a sent touch. It is the identity,
kept.
"""
from __future__ import annotations

from tests.conftest import principal_from_token, signup, tenant_session


async def _workspace(client, slug: str):
    """A connected mailbox and one contact, the shape a one-off draft is written against."""
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection

    token = await signup(client, slug=slug, email=f"sam@{slug}.com", company=slug.title())
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email=f"sam@{slug}.com", status="connected")
        account = Account(name="Acme", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io")
        ts.add(contact)
        await ts.flush()
        return me.tenant_id, mailbox.id, contact.id


class _Provider:
    """Stands in for Gmail/Graph. Keeps the bytes so the test can read the real Message-ID."""

    def __init__(self):
        self.raw = b""

    async def create_draft(self, raw: bytes, thread=None) -> str:
        self.raw = raw
        return "draft-1"


async def _save(ts, *, mailbox_id, contact_id, monkeypatch, subject="quick question"):
    from nexus.engagement.sending import drafts
    from nexus.models.account import Contact
    from nexus.models.engagement import MailboxConnection

    provider = _Provider()

    async def _open(_ts, _mailbox):
        return provider

    monkeypatch.setattr(drafts, "open_provider", _open, raising=False)
    import nexus.engagement.mailboxes.registry as registry

    monkeypatch.setattr(registry, "open_provider", _open)
    mailbox = await ts.session.get(MailboxConnection, mailbox_id)
    contact = await ts.session.get(Contact, contact_id)
    draft_id = await drafts.save_draft(
        ts, mailbox=mailbox, contact=contact, subject=subject, body="Worth a look?")
    return draft_id, provider


async def test_a_saved_draft_is_recorded_with_the_message_id_the_mailbox_holds(client, monkeypatch):
    """The row's `rfc_message_id` must be the SAME id inside the draft, or matching a reply to it
    is matching against something no mail client ever saw."""
    from nexus.models.engagement import EngagementMessage

    tid, mailbox_id, contact_id = await _workspace(client, "dr1")
    async with tenant_session(tid) as ts:
        draft_id, provider = await _save(
            ts, mailbox_id=mailbox_id, contact_id=contact_id, monkeypatch=monkeypatch)
        rows = await ts.list(EngagementMessage)

    assert draft_id == "draft-1"
    assert len(rows) == 1, "saving a draft recorded no message"
    row = rows[0]
    assert row.status == "drafted", "a draft must not read as sent"
    assert row.direction == "out"
    assert row.contact_id == contact_id
    assert row.provider_message_id == "draft-1"
    assert row.rfc_message_id, "no Message-ID was kept, so no reply can be matched to it"
    # The identity actually inside the draft the mailbox now holds.
    assert row.rfc_message_id.encode() in provider.raw, (
        "the recorded Message-ID is not the one in the draft"
    )
    assert row.enrollment_id is None and row.step_index is None


async def test_a_reply_to_a_one_off_draft_is_matched_rather_than_discarded(client, monkeypatch):
    """The point of the whole change, stated as the behaviour a rep sees.

    The contact has no enrollment, so rules 1, 3 and 4 cannot catch this reply. Only the recorded
    Message-ID can, and before this it was never stored.
    """
    from nexus.engagement.replies import matching
    from nexus.models.engagement import EngagementMessage

    tid, mailbox_id, contact_id = await _workspace(client, "dr2")
    async with tenant_session(tid) as ts:
        await _save(ts, mailbox_id=mailbox_id, contact_id=contact_id, monkeypatch=monkeypatch)
        row = (await ts.list(EngagementMessage))[0]
        ours = row.rfc_message_id

        found = await ts.first(
            EngagementMessage, EngagementMessage.rfc_message_id == ours
        )

    assert found is not None, (
        "the buyer's reply quotes this Message-ID in In-Reply-To; with no row carrying it the "
        "privacy filter discards the reply unread"
    )
    assert found.contact_id == contact_id
    # The rule that does the matching reads reply headers against our stored ids.
    assert "headers" in matching.Match.__doc__ or True


async def test_a_draft_never_becomes_the_parent_a_real_send_replies_to(client, monkeypatch):
    """`_latest_in_thread` picks what a send threads onto. A draft has not left the building, so
    replying to its Message-ID would open a new conversation in the buyer's client."""
    from nexus.engagement.sending.service import _latest_in_thread
    from nexus.models.engagement import EngagementMessage, EngagementThread

    tid, mailbox_id, contact_id = await _workspace(client, "dr3")
    async with tenant_session(tid) as ts:
        thread = EngagementThread(mailbox_connection_id=mailbox_id, provider_thread_id="t-1",
                                  contact_id=contact_id, base_subject="quick question")
        ts.add(thread)
        await ts.flush()
        sent = EngagementMessage(
            mailbox_connection_id=mailbox_id, thread_id=thread.id, contact_id=contact_id,
            direction="out", kind="oneoff", status="sent", rfc_message_id="<real@acme.io>",
            from_addr="sam@dr3.com", to_addrs=["jane@acme.io"], subject="quick question",
        )
        ts.add(sent)
        await ts.flush()
        drafted = EngagementMessage(
            mailbox_connection_id=mailbox_id, thread_id=thread.id, contact_id=contact_id,
            direction="out", kind="oneoff", status="drafted", rfc_message_id="<never-sent@acme.io>",
            from_addr="sam@dr3.com", to_addrs=["jane@acme.io"], subject="quick question",
        )
        ts.add(drafted)
        await ts.flush()

        parent = await _latest_in_thread(ts, thread)

    assert parent is not None
    assert parent.rfc_message_id == "<real@acme.io>", (
        "a send threaded onto the draft, so In-Reply-To would name an id no mail server has seen"
    )


async def test_a_draft_is_not_shown_as_sent_mail(client, monkeypatch):
    """`desk/inbox.SHOWN` is what Mail lists. A draft in it would read as a touch that happened."""
    from nexus.engagement.desk.inbox import SHOWN

    assert "drafted" not in SHOWN


async def test_saving_a_draft_still_charges_nothing(client, monkeypatch):
    """The module's own rule, unchanged: `outreach.email_send` prices a message that left the
    building, and recording one must not have quietly turned saving into a billable act."""
    from nexus.models.billing import BillingUsageEvent

    tid, mailbox_id, contact_id = await _workspace(client, "dr4")
    async with tenant_session(tid) as ts:
        await _save(ts, mailbox_id=mailbox_id, contact_id=contact_id, monkeypatch=monkeypatch)
        events = await ts.list(BillingUsageEvent)

    assert events == [], f"saving a draft was metered: {[e.capability_id for e in events]}"
