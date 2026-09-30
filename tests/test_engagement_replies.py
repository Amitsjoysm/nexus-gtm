"""Reply ingestion: parsing, matching, the privacy filter, classification rules, actions, bounces,
out-of-office, and the two webhooks (spec §6, §11, D3, D6, D7, D8, D22, D23).

Messages are real RFC 5322 bytes built with the standard library and served by `Mailbox`, a provider
double installed through the registry seam that keeps a Sent folder (for sends) and an inbox (for
what arrives). What Gmail and Graph actually deliver is covered by `tests_live/engagement/`.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

import pytest

from nexus.core.config import get_settings
from tests.conftest import tenant_session
from tests.test_engagement_sending import SentFolder
from tests.test_engagement_sequences import _enrollment, _launched, _run

UTC = timezone.utc  # datetime.UTC is 3.11+; this project supports 3.10

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


class Mailbox(SentFolder):
    """Sends land in a Sent folder; arrivals are served from an inbox in order."""

    def __init__(self):
        super().__init__()
        self.inbox: list = []

    def arrive(self, raw: bytes, *, thread_id: str = "thread-1", when: datetime = NOW) -> str:
        from nexus.engagement.mailboxes.provider import InboundMessage

        message_id = f"in-{len(self.inbox) + 1}"
        self.inbox.append(InboundMessage(provider_message_id=message_id,
                                         provider_thread_id=thread_id, raw=raw,
                                         received_at=when))
        return message_id

    async def fetch_changes(self, cursor):
        from nexus.engagement.mailboxes.provider import ChangeBatch

        start = int(cursor or 0)
        return ChangeBatch(message_ids=[m.provider_message_id for m in self.inbox[start:]],
                           next_cursor=str(len(self.inbox)))

    async def get_message(self, provider_message_id):
        return next(m for m in self.inbox if m.provider_message_id == provider_message_id)


@pytest.fixture
def mailbox_double():
    from nexus.engagement.mailboxes import registry

    box = Mailbox()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


def _mail(*, sender: str, body: str, subject: str = "Re: Quick question", in_reply_to: str = "",
          headers: dict | None = None, when: datetime = NOW) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "sam@seq.com"
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain="acme.io")
    message["Date"] = format_datetime(when)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(body)
    return message.as_bytes()


def _bounce(original: bytes, recipient: str) -> bytes:
    """A delivery status notification as Gmail sends one: a multipart/report carrying the status
    block and our original message."""
    crlf = "\r\n"
    head = crlf.join([
        "From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
        "To: sam@seq.com",
        "Subject: Delivery Status Notification (Failure)",
        "Message-ID: <bounce-1@googlemail.com>",
        "MIME-Version: 1.0",
        'Content-Type: multipart/report; report-type=delivery-status; boundary="B"',
        "",
        "--B",
        "Content-Type: text/plain; charset=utf-8",
        "",
        "Your message wasn't delivered.",
        "",
        "--B",
        "Content-Type: message/delivery-status",
        "",
        "Reporting-MTA: dns; googlemail.com",
        "",
        f"Final-Recipient: rfc822; {recipient}",
        "Action: failed",
        "Status: 5.1.1",
        "",
        "--B",
        "Content-Type: message/rfc822",
        "",
        "",
    ]).encode()
    return head + original + f"{crlf}--B--{crlf}".encode()


# ---- parsing ------------------------------------------------------------------------------------

def test_the_new_text_is_read_without_the_history_it_quotes():
    from nexus.engagement.replies.parse import parse

    raw = _mail(sender="Jane Buyer <Jane0@Acme.io>", in_reply_to="<m1@seq.com>", body=(
        "Sounds interesting — can you send pricing?\n\n"
        "On Mon, 21 Sep 2026 at 10:00, Sam Rep <sam@seq.com> wrote:\n> Hi Jane,\n> Worth a chat?"))
    parsed = parse(raw)
    assert parsed.from_addr == "jane0@acme.io" and parsed.from_name == "Jane Buyer"
    assert parsed.in_reply_to == "<m1@seq.com>" and parsed.replied_to_ids == ["<m1@seq.com>"]
    assert parsed.text == "Sounds interesting — can you send pricing?"
    assert "Worth a chat?" in parsed.full_text
    assert not parsed.is_auto and not parsed.is_bounce


def test_auto_replies_are_recognised_by_their_headers_not_by_a_model():
    from nexus.engagement.replies.parse import parse

    assert parse(_mail(sender="jane0@acme.io", body="Away",
                       headers={"Auto-Submitted": "auto-replied"})).is_auto
    assert parse(_mail(sender="jane0@acme.io", body="Away",
                       headers={"Precedence": "bulk"})).is_auto
    assert parse(_mail(sender="jane0@acme.io", body="Away",
                       subject="Automatic reply: Quick question")).is_auto
    assert not parse(_mail(sender="jane0@acme.io", body="Hi",
                           headers={"Auto-Submitted": "no"})).is_auto


def test_a_bounce_names_the_exact_message_that_failed():
    from nexus.engagement.replies.parse import parse

    original = _mail(sender="sam@seq.com", body="Hi Jane")
    ours = parse(original).message_id
    parsed = parse(_bounce(original, "jane0@acme.io"))
    assert parsed.is_bounce
    assert ours in parsed.bounced_message_ids
    assert parsed.bounced_recipients == ["jane0@acme.io"]


# ---- the rules the model is not trusted with --------------------------------------------------------

def _finalize(category, *, confidence=0.9, phrase="", text="", threshold=0.8):
    from nexus.engagement.replies.classify import Reading, finalize

    return finalize(Reading(category, confidence, phrase), text=text, threshold=threshold,
                    received_at=NOW, zone=UTC)


def test_a_refusal_with_a_timeframe_is_never_scheduled():
    verdict = _finalize("later", phrase="next June",
                        text="Not interested right now. Maybe next June.")
    assert verdict.category == "unclear" and "D8" in verdict.reasoning


def test_later_needs_confidence_and_a_date_that_resolves_one_way():
    confident = _finalize("later", phrase="next June", text="Try me next June.")
    assert confident.category == "later" and confident.resolved_date == date(2027, 6, 1)
    assert _finalize("later", confidence=0.6, phrase="next June",
                     text="Try me next June.").category == "unclear"
    assert _finalize("later", phrase="sometime", text="Sometime.").category == "unclear"


def test_the_actions_that_run_unattended_need_the_owners_confidence_bar():
    assert _finalize("declined", confidence=0.95).category == "declined"
    assert _finalize("declined", confidence=0.7).category == "unclear"
    assert _finalize("unsubscribe", confidence=0.7, threshold=0.6).category == "unsubscribe"
    # A human-facing category is never downgraded: a person reads it anyway.
    assert _finalize("interested", confidence=0.3).category == "interested"


def test_out_of_office_resumes_the_business_day_after_they_are_back():
    from nexus.engagement.replies.classify import Reading, finalize, return_phrase

    assert return_phrase("I am out of the office until Friday 2 October.") \
        .startswith("Friday 2 October")
    back = finalize(Reading("out_of_office", 1.0, "Friday 2 October"), text="",
                    threshold=0.8, received_at=NOW, zone=UTC)
    assert back.resolved_date == date(2026, 10, 2) and back.resume_on == date(2026, 10, 5)
    unknown = finalize(Reading("out_of_office", 1.0, ""), text="Away for a while",
                       threshold=0.8, received_at=NOW, zone=UTC, ooo_default_days=7)
    assert unknown.resolved_date == date(2026, 9, 29) and unknown.resume_on == date(2026, 9, 30)


def test_an_unusable_model_answer_is_unclear_not_a_guess():
    from nexus.engagement.replies.classify import parse_reading

    assert parse_reading("no json here").category == "unclear"
    reading = parse_reading('Sure! {"category": "question", "confidence": 0.8, "date_phrase": ""}')
    assert (reading.category, reading.confidence) == ("question", 0.8)


# ---- webhooks -------------------------------------------------------------------------------------

def _rsa_jwks():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwk

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    public = jwk.construct(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo), "RS256")
    public_jwk = {**public.to_dict(), "kid": "k1", "use": "sig"}
    return pem, {"keys": [public_jwk]}


def _token(pem, **claims):
    from jose import jwt

    body = {"iss": "https://accounts.google.com", "aud": "https://app.example.com/api/engagement/"
            "webhooks/gmail", "email": "push@proj.iam.gserviceaccount.com",
            # Signed "now" by the real clock: jose checks expiry against it.
            "email_verified": True, "exp": int(time.time()) + 3600, "iat": int(time.time())}
    body.update(claims)
    return jwt.encode(body, pem, algorithm="RS256", headers={"kid": "k1"})


def test_a_gmail_push_is_trusted_only_with_googles_signature_for_our_audience():
    from nexus.engagement.replies.notifications import PushRejected, verify_push_token

    pem, jwks = _rsa_jwks()
    audience = "https://app.example.com/api/engagement/webhooks/gmail"
    account = "push@proj.iam.gserviceaccount.com"
    claims = verify_push_token(_token(pem), jwks=jwks, audience=audience,
                               service_account=account)
    assert claims["email"] == account
    for bad in (_token(pem, aud="https://evil.example.com/hook"),
                _token(pem, email="someone@else.iam.gserviceaccount.com"),
                _token(pem, iss="https://evil.example.com"),
                _token(pem, email_verified=False), "not-a-token"):
        with pytest.raises(PushRejected):
            verify_push_token(bad, jwks=jwks, audience=audience, service_account=account)
    other_pem, _ = _rsa_jwks()
    with pytest.raises(PushRejected):
        verify_push_token(_token(other_pem), jwks=jwks, audience=audience, service_account=account)
    with pytest.raises(PushRejected):
        verify_push_token(_token(pem), jwks=jwks, audience=audience, service_account="")


async def test_graph_is_answered_during_the_handshake_and_refused_with_a_forged_client_state(
    client,
):
    from nexus.engagement.replies.notifications import client_state

    echo = await client.post("/api/engagement/webhooks/graph?validationToken=abc%20123")
    assert echo.status_code == 200 and echo.text == "abc 123"
    forged = await client.post("/api/engagement/webhooks/graph", json={"value": [
        {"subscriptionId": "sub-1", "clientState": "guess"}]})
    assert forged.status_code == 403
    genuine = await client.post("/api/engagement/webhooks/graph", json={"value": [
        {"subscriptionId": "sub-1", "clientState": client_state("sub-1")}]})
    assert genuine.status_code == 202


async def test_a_gmail_push_without_a_configured_service_account_is_refused(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_google_push_service_account", "")
    data = base64.b64encode(json.dumps({"emailAddress": "sam@seq.com"}).encode()).decode()
    response = await client.post("/api/engagement/webhooks/gmail",
                                 headers={"Authorization": "Bearer x"},
                                 json={"message": {"data": data}})
    assert response.status_code == 401


# ---- ingestion end to end ----------------------------------------------------------------------------

async def _sent_first(slug, monkeypatch, box):
    """A launched one-contact campaign whose first email has gone out through `box`."""
    tid, _campaign = await _launched(slug, monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    from email import message_from_bytes

    first = message_from_bytes(box.delivered[0])
    return tid, await _enrollment(tid), first["Message-ID"]


async def _sync(tid, mailbox_id, when=NOW):
    from nexus.engagement.replies.ingest import sync_mailbox
    from nexus.models.engagement import MailboxConnection

    async with tenant_session(tid) as ts:
        return await sync_mailbox(ts, await ts.get(MailboxConnection, mailbox_id), now=when)


async def _classification(tid):
    from nexus.models.engagement import ReplyClassification

    async with tenant_session(tid) as ts:
        return await ts.first(ReplyClassification)


async def test_an_interested_reply_stops_the_sequence_and_alerts_the_owner(
    mailbox_double, monkeypatch,
):
    from nexus.models.alerts import Alert
    from nexus.models.engagement import EngagementMessage

    tid, enrollment, our_id = await _sent_first("replyint", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="Jane0 Buyer <jane0@acme.io>", in_reply_to=our_id,
                                body="Sounds interesting, let's talk next week."))
    result = await _sync(tid, enrollment.mailbox_connection_id)
    assert result == {"classified:interested": 1}
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("stopped", "replied")
    classification = await _classification(tid)
    assert classification.category == "interested" and classification.status == "open"
    async with tenant_session(tid) as ts:
        stored = await ts.first(EngagementMessage, EngagementMessage.direction == "in")
        alerts = await ts.list(Alert)
    assert stored.inbound_kind == "human" and stored.thread_id == enrollment.current_thread_id
    assert any(a.meta.get("category") == "reply_interested" for a in alerts)
    # The same notification again is harmless: the message is already stored.
    assert await _sync(tid, enrollment.mailbox_connection_id) == {}


async def test_a_dated_later_snoozes_until_that_day_in_the_contacts_morning(
    mailbox_double, monkeypatch,
):
    tid, enrollment, our_id = await _sent_first("replylater", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Not now — try me in June please."))
    await _sync(tid, enrollment.mailbox_connection_id)
    enrollment = await _enrollment(tid)
    assert enrollment.status == "snoozed"
    local = enrollment.snoozed_until.astimezone(__import__("zoneinfo").ZoneInfo(
        enrollment.contact_timezone))
    assert (local.year, local.month, local.hour) == (2027, 6, 9)


async def test_a_refusal_with_a_date_goes_to_the_sdr_and_nothing_sends(mailbox_double,
                                                                        monkeypatch):
    tid, enrollment, our_id = await _sent_first("replymixed", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Not interested. Maybe reach out next year."))
    await _sync(tid, enrollment.mailbox_connection_id)
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("paused", "needs_decision")
    assert (await _classification(tid)).category == "unclear"


async def test_unsubscribe_is_permanent_and_blocks_every_future_send(mailbox_double, monkeypatch):
    from nexus.engagement.suppression.service import active_block

    tid, enrollment, our_id = await _sent_first("replyunsub", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Please remove me from your list."))
    await _sync(tid, enrollment.mailbox_connection_id)
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("stopped", "unsubscribed")
    async with tenant_session(tid) as ts:
        block = await active_block(ts, "jane0@acme.io")
    assert block is not None and block.reason == "unsubscribed"


async def test_out_of_office_pauses_then_resumes_the_same_step_when_they_are_back(
    mailbox_double, monkeypatch,
):
    tid, enrollment, our_id = await _sent_first("replyooo", monkeypatch, mailbox_double)
    step_before = enrollment.current_step_index
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                subject="Automatic reply: Quick question",
                                headers={"Auto-Submitted": "auto-replied"},
                                body="I am out of the office until Friday 2 October."))
    await _sync(tid, enrollment.mailbox_connection_id)
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("paused", "out_of_office")
    assert enrollment.snoozed_until.date() == date(2026, 10, 5)
    # Before they are back: nothing. After: the same step is sent, with no extra email.
    assert await _run(tid, enrollment.id, enrollment.snoozed_until - timedelta(hours=1)) \
        == "campaign_not_active" or (await _enrollment(tid)).status == "paused"
    outcome = await _run(tid, enrollment.id, enrollment.snoozed_until + timedelta(minutes=1))
    assert outcome == "sent"
    enrollment = await _enrollment(tid)
    assert enrollment.current_step_index == step_before + 1


async def test_a_bounce_marks_our_message_stops_the_sequence_and_blocks_the_address(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.suppression.service import active_block
    from nexus.models.engagement import EngagementMessage

    tid, enrollment, _our_id = await _sent_first("replybounce", monkeypatch, mailbox_double)
    mailbox_double.arrive(_bounce(mailbox_double.delivered[0], "jane0@acme.io"))
    assert await _sync(tid, enrollment.mailbox_connection_id) == {"bounced": 1}
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("stopped", "bounced")
    async with tenant_session(tid) as ts:
        ours = await ts.first(EngagementMessage, EngagementMessage.direction == "out")
        assert ours.status == "bounced"
        assert (await active_block(ts, "jane0@acme.io")).reason == "bounced"
        # The bounce itself is not stored: it is about our email, not a conversation.
        assert await ts.first(EngagementMessage, EngagementMessage.direction == "in") is None


async def test_mail_from_a_stranger_is_never_stored(mailbox_double, monkeypatch):
    from nexus.models.engagement import EngagementMessage

    tid, enrollment, _our_id = await _sent_first("replyprivacy", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="friend@example.org", subject="Dinner?",
                                body="Are we still on for Friday?"), thread_id="personal-9")
    assert await _sync(tid, enrollment.mailbox_connection_id) == {"discarded": 1}
    async with tenant_session(tid) as ts:
        assert await ts.first(EngagementMessage, EngagementMessage.direction == "in") is None


async def test_a_colleague_writing_pauses_everyone_at_the_company_as_a_referral(
    mailbox_double, monkeypatch,
):
    tid, enrollment, _our_id = await _sent_first("replycolleague", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="Pat Other <pat@acme.io>", subject="Re: Quick question",
                                body="Jane forwarded this — I own this area."),
                          thread_id="thread-99")
    assert await _sync(tid, enrollment.mailbox_connection_id) == {"classified:referral": 1}
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("paused", "colleague_replied")


def test_every_reply_category_has_a_readable_label_and_a_rule():
    from nexus.alerts.rules import ALERT_CATEGORIES, engagement_rule

    for category in ("reply_interested", "reply_needs_decision", "reply_bounced",
                     "mailbox_needs_reauth"):
        assert category in ALERT_CATEGORIES
        assert engagement_rule(category)[1]
