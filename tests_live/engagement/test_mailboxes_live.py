"""The two mailbox adapters against a real Gmail and a real Microsoft 365 test mailbox (D21).

Mail flows only between the two test mailboxes. Every subject carries ``[nexus-live <run id>]``; the
Gmail test mailbox has a filter that deletes such mail on arrival (see docs/engagement/live-tests.md),
and Microsoft messages are deleted at the end of the test, so the mailboxes stay empty.

One scenario, in order, because each step needs the previous step's real ids:
Gmail sends → Gmail finds its own send by X-Nexus-Ref → Microsoft sees it arrive → Microsoft replies
in the same conversation → Gmail sees the reply in the original thread → Gmail follows up in that
thread → drafts on both sides → Sent-folder search finds the first send.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import make_msgid

import pytest

from nexus.engagement.config import GOOGLE_SCOPES, MICROSOFT_SCOPES, OAuthApp
from nexus.engagement.mailboxes import oauth, transport
from nexus.engagement.mailboxes.gmail import GmailProvider
from nexus.engagement.mailboxes.graph import GRAPH, IMMUTABLE, GraphProvider
from nexus.engagement.mailboxes.provider import CursorExpired, ThreadRef
from nexus.engagement.subjects import reply_subject
from tests_live.engagement.conftest import redirect_uri, require_env

POLL_SECONDS = 150


async def _gmail() -> GmailProvider:
    env = require_env("NEXUS_LIVE_GOOGLE_CLIENT_ID", "NEXUS_LIVE_GOOGLE_CLIENT_SECRET",
                      "NEXUS_LIVE_GMAIL_ADDRESS", "NEXUS_LIVE_GMAIL_REFRESH_TOKEN")
    app = OAuthApp("google", env["NEXUS_LIVE_GOOGLE_CLIENT_ID"],
                   env["NEXUS_LIVE_GOOGLE_CLIENT_SECRET"], "", redirect_uri("google"),
                   GOOGLE_SCOPES, ())
    data = await oauth.refresh(app, env["NEXUS_LIVE_GMAIL_REFRESH_TOKEN"])
    return GmailProvider(access_token=data["access_token"], email=env["NEXUS_LIVE_GMAIL_ADDRESS"])


async def _graph() -> GraphProvider:
    env = require_env("NEXUS_LIVE_MICROSOFT_CLIENT_ID", "NEXUS_LIVE_MICROSOFT_CLIENT_SECRET",
                      "NEXUS_LIVE_MICROSOFT_TENANT", "NEXUS_LIVE_M365_ADDRESS",
                      "NEXUS_LIVE_M365_REFRESH_TOKEN")
    app = OAuthApp("microsoft", env["NEXUS_LIVE_MICROSOFT_CLIENT_ID"],
                   env["NEXUS_LIVE_MICROSOFT_CLIENT_SECRET"], env["NEXUS_LIVE_MICROSOFT_TENANT"],
                   redirect_uri("microsoft"), MICROSOFT_SCOPES, ())
    data = await oauth.refresh(app, env["NEXUS_LIVE_M365_REFRESH_TOKEN"])
    return GraphProvider(access_token=data["access_token"], email=env["NEXUS_LIVE_M365_ADDRESS"])


def _mime(*, sender: str, to: str, subject: str, body: str, ref: str,
          in_reply_to: str = "", references: str = "") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain="nexus-live.invalid")
    message["X-Nexus-Ref"] = ref
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = references or in_reply_to
    message.set_content(body)
    return message.as_bytes()


async def _wait_for(provider, cursor: str, marker: str):
    """Poll ``fetch_changes`` until a message whose raw text contains ``marker`` arrives."""
    deadline = asyncio.get_running_loop().time() + POLL_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        batch = await provider.fetch_changes(cursor)
        for message_id in batch.message_ids:
            message = await provider.get_message(message_id)
            if marker.encode() in message.raw:
                return message, batch.next_cursor
        cursor = batch.next_cursor or cursor
        await asyncio.sleep(10)
    pytest.fail(f"no message containing {marker!r} arrived within {POLL_SECONDS}s")


async def test_both_mailboxes_identify_themselves():
    gmail, graph = await _gmail(), await _graph()
    assert (await gmail.profile()).email == gmail.email
    assert (await graph.profile()).email == graph.email


async def test_a_conversation_round_trip_between_gmail_and_microsoft():
    gmail, graph = await _gmail(), await _graph()
    run = uuid.uuid4().hex[:10]
    subject = f"[nexus-live {run}] quick question"
    graph_created: list[str] = []
    try:
        gmail_cursor = (await gmail.fetch_changes(None)).next_cursor
        graph_cursor = (await graph.fetch_changes(None)).next_cursor

        # 1. Gmail sends and can find its own send by our reference header.
        ref = f"ref-{run}-1"
        before = datetime.now(timezone.utc)
        first = await gmail.send(
            _mime(sender=gmail.email, to=graph.email, subject=subject, ref=ref,
                  body=f"Hi,\n\nLive test {run}.\n\nBest,\nSam\n"),
            thread=None,
        )
        assert first.provider_message_id and first.provider_thread_id and first.rfc_message_id
        found = None
        for _ in range(12):
            found = await gmail.find_sent(ref_header=ref, to=graph.email, around=before)
            if found:
                break
            await asyncio.sleep(5)
        assert found and found.provider_message_id == first.provider_message_id

        # 2. Microsoft sees it arrive, with our header intact.
        arrived, graph_cursor = await _wait_for(graph, graph_cursor, run)
        graph_created.append(arrived.provider_message_id)
        assert message_from_bytes(arrived.raw)["X-Nexus-Ref"] == ref
        assert not arrived.outgoing

        # 3. Microsoft replies in the same conversation.
        reply = await graph.send(
            _mime(sender=graph.email, to=gmail.email, subject=reply_subject(subject),
                  ref=f"ref-{run}-2", in_reply_to=first.rfc_message_id,
                  body=f"Hi Sam,\n\nTry me in June. ({run})\n\nJane\n"),
            thread=ThreadRef(provider_thread_id=arrived.provider_thread_id,
                             reply_to_provider_message_id=arrived.provider_message_id,
                             in_reply_to=first.rfc_message_id),
        )
        graph_created.append(reply.provider_message_id)
        assert reply.provider_thread_id == arrived.provider_thread_id, (
            "Exchange started a new conversation: the Thread-Index child block was not honoured"
        )

        # 4. Gmail sees the reply in the original thread.
        answer, gmail_cursor = await _wait_for(gmail, gmail_cursor, "Try me in June")
        assert answer.provider_thread_id == first.provider_thread_id

        # 5. Gmail follows up in that thread, with exactly one "Re:".
        follow = await gmail.send(
            _mime(sender=gmail.email, to=graph.email, subject=reply_subject(reply_subject(subject)),
                  ref=f"ref-{run}-3", in_reply_to=message_from_bytes(answer.raw)["Message-ID"],
                  references=f"{first.rfc_message_id} {message_from_bytes(answer.raw)['Message-ID']}",
                  body=f"Hi Jane,\n\nNoted, June it is. ({run})\n\nBest,\nSam\n"),
            thread=ThreadRef(provider_thread_id=first.provider_thread_id),
        )
        assert follow.provider_thread_id == first.provider_thread_id

        # 6. Drafts on both sides.
        assert await gmail.create_draft(
            _mime(sender=gmail.email, to=graph.email, subject=reply_subject(subject),
                  ref=f"ref-{run}-4", body="draft"),
            thread=ThreadRef(provider_thread_id=first.provider_thread_id))
        draft_id = await graph.create_draft(
            _mime(sender=graph.email, to=gmail.email, subject=reply_subject(subject),
                  ref=f"ref-{run}-5", body="draft"),
            thread=ThreadRef(provider_thread_id=arrived.provider_thread_id,
                             reply_to_provider_message_id=arrived.provider_message_id))
        assert draft_id
        graph_created.append(draft_id)

        # 7. The cutover's Sent-folder search finds the first send by recipient and subject.
        searched = await gmail.search_sent(to=graph.email, subject=subject, around=before)
        assert searched and searched.provider_thread_id == first.provider_thread_id
    finally:
        for message_id in graph_created:
            try:
                await transport.request("DELETE", f"{GRAPH}/me/messages/{message_id}",
                                        token=graph._token, headers=IMMUTABLE)
            except Exception:
                pass


async def test_a_forgotten_gmail_cursor_is_reported_so_the_sync_can_resync():
    gmail = await _gmail()
    with pytest.raises(CursorExpired):
        await gmail.fetch_changes("1")
