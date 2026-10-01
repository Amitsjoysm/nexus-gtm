"""Sending: the MIME we hand to Gmail and Graph, the pre-send checks, and exactly-once (spec §5).

The provider here is `SentFolder`, a test double installed through
`registry.set_provider_factory` — the same kind of seam as `set_crm_connector`. It keeps a real Sent
folder in memory, so the reconciliation tests exercise the thing that matters: after a timeout, is
the message found by its `X-Nexus-Ref`, and is it never delivered twice? What Gmail and Graph
actually do with the bytes is proved against the real services in `tests_live/engagement/`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email import message_from_bytes as _parse
from email.policy import default as _modern

import pytest

from tests.conftest import make_tenant, tenant_session

UTC = timezone.utc  # datetime.UTC is 3.11+; this project supports 3.10

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


def message_from_bytes(raw: bytes):
    """Parsed with the modern policy, which is what gives a message `get_content()`."""
    return _parse(raw, policy=_modern)


class SentFolder:
    """A mailbox that records every delivery and can be told to fail the next call."""

    provider = "google"

    def __init__(self):
        self.delivered: list[bytes] = []
        self.drafts: list[bytes] = []
        self.calls = 0
        self.fail_next: Exception | None = None
        self.deliver_then_fail = False

    async def send(self, mime: bytes, *, thread=None):
        from nexus.engagement.mailboxes.provider import SentRef

        self.calls += 1
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            if self.deliver_then_fail:
                # The provider accepted the message and the answer never reached us.
                self.delivered.append(mime)
            raise error
        self.delivered.append(mime)
        return SentRef(provider_message_id=f"pm-{len(self.delivered)}",
                       provider_thread_id=getattr(thread, "provider_thread_id", "") or "thread-1")

    async def create_draft(self, mime: bytes, *, thread=None) -> str:
        self.drafts.append(mime)
        return f"draft-{len(self.drafts)}"

    async def find_sent(self, *, ref_header: str, to: str, around):
        from nexus.engagement.mailboxes.provider import SentRef

        for index, raw in enumerate(self.delivered, 1):
            if message_from_bytes(raw)["X-Nexus-Ref"] == ref_header:
                return SentRef(provider_message_id=f"pm-{index}", provider_thread_id="thread-1")
        return None


@pytest.fixture
def folder():
    from nexus.engagement.mailboxes import registry

    box = SentFolder()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


async def _world(slug: str):
    """A workspace with an SDR, a connected mailbox, an account and a contact."""
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection
    from nexus.models.identity import User

    tid = await make_tenant(slug=slug, name=slug.title())
    async with tenant_session(tid) as ts:
        user = User(email=f"sam@{slug}.com", full_name="Sam Rep", password_hash="x")
        ts.session.add(user)
        await ts.session.flush()
        mailbox = MailboxConnection(owner_user_id=user.id, provider="google",
                                    email=f"sam@{slug}.com", display_name="Sam Rep",
                                    status="connected", timezone="Europe/London",
                                    signature="Sam\nSDR, Seller Co")
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io")
        ts.add(contact)
        await ts.flush()
        return tid, mailbox.id, contact.id, user.id


async def _load(ts, mailbox_id, contact_id):
    from nexus.models.account import Contact
    from nexus.models.engagement import MailboxConnection

    return await ts.get(MailboxConnection, mailbox_id), await ts.get(Contact, contact_id)


# ---- the message ----------------------------------------------------------------------------------

def test_the_message_carries_threading_unsubscribe_and_reconciliation_headers():
    from nexus.engagement.sending import mime

    message = mime.build_message(
        from_addr="sam@seller.com", from_name="Sam Rep", to_addr="jane@acme.io",
        subject="Re: Quick question", body="Hi Jane,\n\nFollowing up.", message_id="<m2@seller>",
        ref="01JREF", unsubscribe_url="https://app.example.com/api/u/tok",
        unsubscribe_mailto="sam@seller.com", signature="Sam", in_reply_to="<m1@seller>",
        references="<m1@seller>")
    parsed = message_from_bytes(mime.to_bytes(message))
    assert parsed["Message-ID"] == "<m2@seller>"
    assert parsed["In-Reply-To"] == "<m1@seller>" and parsed["References"] == "<m1@seller>"
    assert parsed["List-Unsubscribe"] == ("<https://app.example.com/api/u/tok>, "
                                          "<mailto:sam@seller.com>")
    assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert parsed["X-Nexus-Ref"] == "01JREF"
    # Plain text first, HTML last: a client shows the LAST part it can render (RFC 2046).
    assert parsed.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in parsed.iter_parts()] == ["text/plain", "text/html"]


# ---- how the message reads in Outlook and Gmail (reported 2026-09-30) ----------------------------
# A real send read "Scaling pro=uct development ... resource co=straints ... a 15-minute cal=
# Thursday", signed "Best, Alex" above the rep's own "Kind Regards, Amit Singh", with the full
# unsubscribe URL (also broken by "=") pasted under it.

LONG = ("Hi Jeff,\n\nI saw Carnegie Foundry\u2019s recent $10.3M SEC Form D filing. Scaling product "
        "development and accelerating market entry can be hampered by resource constraints. We "
        "partner with ISVs to supply engineering talent and launch support, removing those "
        "bottlenecks. Are you available for a 15\u2011minute call Thursday 1 October?\n\nBest,\nAlex")
SIGNATURE = "Kind Regards,\nAmit Singh\nDeveloper\nDevbay"
URL = "https://app.example.com/api/u/v1.b03e58cd9f4f4f4f9f26bed452f886a.7cc65d556bc1451383"


def _built(body=LONG, signature=SIGNATURE, from_name="Amit Singh"):
    from nexus.engagement.sending import mime

    return mime.build_message(
        from_addr="amit@devbay.io", from_name=from_name, to_addr="jeff@cf.com",
        subject="Engineering capacity", body=body, message_id="<m1@devbay>", ref="01JREF",
        unsubscribe_url=URL, unsubscribe_mailto="amit@devbay.io", signature=signature)


def test_every_line_ends_crlf_so_exchange_decodes_quoted_printable():
    """With bare LF, Exchange read a soft break `=\\n` plus the next character as a broken escape:
    it kept the `=` and dropped the character, once every ~76 characters."""
    from nexus.engagement.sending import mime

    raw = mime.to_bytes(_built())
    assert b"\n" in raw and raw.count(b"\n") == raw.count(b"\r\n"), "a bare LF reached the wire"
    assert b"=\r\n" in raw, "the long paragraph is still wrapped, now with a proper soft break"
    plain = message_from_bytes(raw).get_body(("plain",)).get_content()
    assert "product development" in plain and "resource constraints" in plain
    assert "15-minute call" in plain, "a non-breaking hyphen is sent as a plain one"


def test_the_signature_replaces_the_models_sign_off_instead_of_stacking():
    plain = message_from_bytes(_built().as_bytes()).get_body(("plain",)).get_content()
    assert "Alex" not in plain and "Best," not in plain
    assert plain.count("Kind Regards,") == 1 and "Amit Singh\nDeveloper\nDevbay" in plain


def test_a_signature_without_a_closing_keeps_the_closing_and_the_real_name():
    plain = message_from_bytes(_built(signature="Amit Singh\nDeveloper").as_bytes()) \
        .get_body(("plain",)).get_content()
    assert "Best,\nAmit Singh\nDeveloper" in plain and "Alex" not in plain


def test_with_no_signature_the_sign_off_names_the_sender():
    plain = message_from_bytes(_built(signature="").as_bytes()).get_body(("plain",)).get_content()
    assert "Best,\nAmit" in plain and "Alex" not in plain


def test_the_html_part_reads_like_an_email_and_links_unsubscribe_in_small_print():
    parsed = message_from_bytes(mime_bytes := _to_bytes(_built()))
    html = parsed.get_body(("html",)).get_content()
    assert html.count(URL) == 1 and f'href="{URL}"' in html, "the URL only as a link target"
    assert ">unsubscribe</a>" in html
    footer = html[html.index("Not relevant?") - 200:html.index("Not relevant?")]
    assert "font-size:11px" in footer
    assert "Hi Jeff,</p>" in html or "Hi Jeff,<br>" in html
    assert "=\r\n" not in html and mime_bytes


def _to_bytes(message):
    from nexus.engagement.sending import mime

    return mime.to_bytes(message)


def test_draft_text_is_cleaned_of_look_alike_and_invisible_characters():
    from nexus.engagement.sending.mime import tidy_text

    assert tidy_text("a 15\u2011minute call\u00a0on\u202fMonday\u200b.") == "a 15-minute call on Monday."
    assert tidy_text("Carnegie\u2019s round \u2014 big") == "Carnegie\u2019s round \u2014 big"


def test_a_draft_saved_over_imap_uses_crlf_too():
    from email import policy as _policy

    from nexus.integrations import email_sender

    src = (email_sender.__file__)
    text = open(src, encoding="utf-8").read()
    assert "msg.as_bytes(policy=policy.SMTP)" in text or "as_bytes(policy=SMTP)" in text
    assert _policy.SMTP.linesep == "\r\n"


def test_the_body_ends_with_the_signature_then_one_opt_out_line():
    from nexus.engagement.sending import mime

    url = "https://app.example.com/api/u/tok"
    text = mime.compose_body("Hi Jane,\n\nWorth a chat?", "Sam", url)
    assert text.index("Worth a chat?") < text.index("Sam") < text.index(url)
    assert text.count('Just reply "no"') == 1
    # Shown in the composer with its footer, sent again: still one footer and one signature.
    assert mime.compose_body(text, "Sam", url) == text


def test_references_keep_the_thread_root_and_stay_bounded():
    from nexus.engagement.sending import mime

    chain = " ".join(f"<m{i}@x>" for i in range(30))
    refs = mime.references_for(chain, "<m30@x>").split()
    assert refs[0] == "<m0@x>" and refs[-1] == "<m30@x>"
    assert len(refs) == mime.MAX_REFERENCES
    assert mime.references_for("", "<first@x>") == "<first@x>"


# ---- the checks -----------------------------------------------------------------------------------

def test_stops_come_before_holds_and_a_first_touch_is_not_held_for_quality():
    from nexus.engagement.sending import checks

    stopped = checks.decide(suppressed_reason="unsubscribed", mailbox_status="needs_reauth")
    assert stopped.outcome == checks.STOP and "do-not-contact" in stopped.reason
    assert checks.decide(inbound_since_scheduled=True).outcome == checks.STOP
    assert checks.decide(enrollment_status="completed").outcome == checks.STOP
    assert checks.decide(paused_by_colleague=True).outcome == checks.HOLD
    assert checks.decide(mailbox_status="needs_reauth").outcome == checks.HOLD
    assert checks.decide(mailbox_paused_until=NOW + timedelta(minutes=5), now=NOW).outcome \
        == checks.HOLD
    assert checks.decide(mailbox_paused_until=NOW - timedelta(minutes=5), now=NOW).ok
    # A follow-up nobody is watching waits for review; a reviewed first email does not (D17).
    assert checks.decide(quality_problems=["no greeting"]).outcome == checks.HOLD
    assert checks.decide(quality_problems=["no greeting"], is_first_touch=True).ok
    assert checks.decide(credits_ok=False).outcome == checks.HOLD


# ---- volume ---------------------------------------------------------------------------------------

def test_the_volume_warning_appears_only_above_fifty_and_never_blocks():
    from nexus.engagement.sending.limits import volume_warning

    assert volume_warning("sam@x.com", 30, planned=20) == ""
    warning = volume_warning("sam@x.com", 40, planned=20)
    assert "about 60 emails from sam@x.com today" in warning and "spam" in warning


async def test_today_is_the_mailbox_owners_day_not_the_utc_day():
    from nexus.engagement.sending.limits import sent_today
    from nexus.models.engagement import EngagementMessage

    tid, mailbox_id, contact_id, _user = await _world("vol")
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        mailbox.timezone = "America/Los_Angeles"
        # 06:00 UTC on the 22nd is 23:00 on the 21st in Los Angeles: yesterday, locally.
        for sent_at in (datetime(2026, 9, 22, 6, 0, tzinfo=UTC),
                        datetime(2026, 9, 22, 8, 0, tzinfo=UTC),
                        datetime(2026, 9, 22, 18, 0, tzinfo=UTC)):
            ts.add(EngagementMessage(mailbox_connection_id=mailbox.id, contact_id=contact.id,
                                     direction="out", status="sent", sent_at=sent_at))
        await ts.flush()
        assert await sent_today(ts, mailbox, now=datetime(2026, 9, 22, 19, 0, tzinfo=UTC)) == 2


# ---- exactly once ---------------------------------------------------------------------------------

async def test_a_message_is_sent_once_threaded_and_asking_again_does_not_resend(folder):
    from nexus.engagement.sending.service import send
    from nexus.models.engagement import EngagementMessage, EngagementThread

    tid, mailbox_id, contact_id, user_id = await _world("once")
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="Quick question",
                            body="Hi Jane,\n\nWorth a chat?", idempotency_key="k-1",
                            user_id=user_id)
        assert result.sent and not result.reconciled
        row = await ts.get(EngagementMessage, result.message_id)
        assert row.status == "sent" and row.provider_message_id == "pm-1"
        thread = await ts.get(EngagementThread, row.thread_id)
        assert thread.provider_thread_id == "thread-1" and thread.base_subject == "Quick question"
        delivered = message_from_bytes(folder.delivered[0])
        assert delivered["X-Nexus-Ref"] == row.ref_header
        assert delivered["Message-ID"] == row.rfc_message_id
        # CRLF is the wire form; a mail client normalises it, and so does this read.
        plain = delivered.get_body(("plain",)).get_content().replace("\r\n", "\n")
        assert "Sam\nSDR, Seller Co" in plain

        again = await send(ts, mailbox=mailbox, contact=contact, subject="Quick question",
                           body="Hi Jane,\n\nWorth a chat?", idempotency_key="k-1")
    assert again.sent and again.reconciled and again.message_id == result.message_id
    assert folder.calls == 1 and len(folder.delivered) == 1


async def test_a_timeout_after_delivery_is_reconciled_from_the_sent_folder(folder):
    from nexus.engagement.mailboxes.provider import TransientError
    from nexus.engagement.sending.service import send

    tid, mailbox_id, contact_id, _user = await _world("recon")
    folder.fail_next = TransientError("read timed out")
    folder.deliver_then_fail = True
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi Jane",
                            idempotency_key="k-2")
    assert result.sent and result.reconciled
    assert len(folder.delivered) == 1, "a timed-out send must never become a second email"


async def test_a_timeout_before_delivery_is_retried_with_the_same_ids(folder):
    from nexus.engagement.mailboxes.provider import TransientError
    from nexus.engagement.sending.service import send
    from nexus.models.engagement import EngagementMessage

    tid, mailbox_id, contact_id, _user = await _world("retry")
    folder.fail_next = TransientError("connection reset")
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        first = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi Jane",
                           idempotency_key="k-3")
        assert first.outcome == "held" and not folder.delivered
        row = await ts.get(EngagementMessage, first.message_id)
        ref, message_id = row.ref_header, row.rfc_message_id

        second = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi Jane",
                            idempotency_key="k-3")
    assert second.sent and second.message_id == first.message_id
    assert len(folder.delivered) == 1
    delivered = message_from_bytes(folder.delivered[0])
    assert delivered["X-Nexus-Ref"] == ref and delivered["Message-ID"] == message_id


async def test_a_provider_limit_pauses_the_mailbox_and_the_step_stays_due(folder):
    from nexus.engagement.mailboxes.provider import ProviderLimit
    from nexus.engagement.sending.service import send
    from nexus.models.engagement import EngagementMessage

    tid, mailbox_id, contact_id, _user = await _world("limit")
    reset = datetime(2030, 1, 1, tzinfo=UTC)
    folder.fail_next = ProviderLimit("quota", status=429, retry_at=reset)
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi Jane",
                            idempotency_key="k-4")
        assert result.outcome == "held" and "limit" in result.reason
        assert mailbox.paused_until == reset
        assert (await ts.get(EngagementMessage, result.message_id)).status == "queued"
        # While paused, the check holds it before the provider is even asked.
        again = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi Jane",
                           idempotency_key="k-4")
    assert again.outcome == "held" and "paused" in again.reason
    assert folder.calls == 1 and not folder.delivered


async def test_a_suppressed_address_is_stopped_before_anything_is_written(folder):
    from nexus.engagement.sending.service import send
    from nexus.engagement.suppression.service import suppress
    from nexus.models.engagement import EngagementMessage

    tid, mailbox_id, contact_id, _user = await _world("dnc")
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        await suppress(ts, email="Jane@Acme.io", reason="unsubscribed", contact_id=contact.id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi",
                            idempotency_key="k-5")
        assert result.outcome == "stopped" and "do-not-contact" in result.reason
        assert await ts.list(EngagementMessage) == []
    assert folder.calls == 0


async def test_an_expired_grant_holds_the_send_and_marks_the_mailbox(folder):
    from nexus.engagement.mailboxes.provider import AuthExpired
    from nexus.engagement.sending.service import send

    tid, mailbox_id, contact_id, _user = await _world("reauth")
    folder.fail_next = AuthExpired("invalid_grant", status=401)
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi",
                            idempotency_key="k-6")
        assert result.outcome == "held" and mailbox.status == "needs_reauth"


async def test_a_follow_up_answers_the_latest_message_in_its_thread(folder):
    from nexus.engagement.sending.service import send
    from nexus.models.engagement import EngagementMessage, EngagementThread

    tid, mailbox_id, contact_id, _user = await _world("follow")
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        first = await send(ts, mailbox=mailbox, contact=contact, subject="Quick question",
                           body="Hi Jane", idempotency_key="k-7")
        first_row = await ts.get(EngagementMessage, first.message_id)
        thread = await ts.get(EngagementThread, first_row.thread_id)
        follow = await send(ts, mailbox=mailbox, contact=contact, subject="Re: Quick question",
                            body="Following up", thread=thread, idempotency_key="k-8")
        follow_row = await ts.get(EngagementMessage, follow.message_id)
    assert follow_row.in_reply_to == first_row.rfc_message_id
    assert follow_row.references_header == first_row.rfc_message_id
    assert follow_row.thread_id == first_row.thread_id
    delivered = message_from_bytes(folder.delivered[1])
    assert delivered["In-Reply-To"] == first_row.rfc_message_id


async def test_a_sent_message_is_recorded_in_the_ledger_for_a_consenting_workspace(folder):
    from nexus.engagement.ledger import consent
    from nexus.engagement.sending.service import send
    from nexus.models.ledger import LedgerOutbox

    tid, mailbox_id, contact_id, _user = await _world("ledg")
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        result = await send(ts, mailbox=mailbox, contact=contact, subject="Hi", body="Hi Jane",
                            step_index=None, idempotency_key="k-9",
                            context={"context_pack": "Account: Acme Robotics"})
        events = await ts.list(LedgerOutbox)
    sent = [e for e in events if e.event_type == "message.sent"]
    assert len(sent) == 1
    payload = sent[0].payload["payload"]
    assert payload["context_pack"] == "Account: Acme Robotics"
    assert payload["mailbox_provider"] == "google"
    assert sent[0].payload["refs"]["message_id"] == result.message_id


async def test_the_draft_is_told_who_it_is_written_as():
    """The model was told to sign off with "your first name" and never given one, so it invented
    "Alex". The context now names the sender, from the mailbox, else its owner."""
    from types import SimpleNamespace

    from nexus.engagement.drafting.context import build_context
    from nexus.models.account import Account

    tid, mailbox_id, contact_id, _user = await _world("ctxname")
    async with tenant_session(tid) as ts:
        mailbox, contact = await _load(ts, mailbox_id, contact_id)
        account = await ts.get(Account, contact.account_id)
        enrollment = SimpleNamespace(contact_timezone="", current_thread_id=None)
        pack = await build_context(ts, enrollment=enrollment, contact=contact, account=account,
                                   mailbox=mailbox, kind="first")
        assert "YOU (THE SENDER)\n- Name: Sam Rep" in pack.text
        assert "first name, Sam" in pack.text

        mailbox.display_name = ""
        pack = await build_context(ts, enrollment=enrollment, contact=contact, account=account,
                                   mailbox=mailbox, kind="first")
        assert "- Name: Sam Rep" in pack.text, "falls back to the mailbox owner's name"
