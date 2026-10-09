"""A suggested reply is written from the conversation the reply arrived in, and reads what the buyer
actually wrote (2026-10-02).

Asked: "do automated replies have complete context of previous mails?" Two gaps answered it:

* The conversation came from the ENROLLMENT's current thread, not the reply's. Ingestion moves the
  current thread when a person writes, so the two usually agree, but a reply to an older email in
  the sequence, or a desk item whose enrollment has since moved on, drafted against the wrong
  thread: the model answered a conversation the buyer was not in.
* An inbound message is stored whole, quoting our own emails back underneath. Those emails are
  already in the conversation block, so the model read them twice and the buyer's own words, the
  part it must answer, were clipped first when the message ran long.

The playbook rules for the answer itself (two specific times, greeting, paragraphs, sign-off) are
pinned in `test_email_playbook.py`.
"""
from __future__ import annotations

from tests.conftest import tenant_session
from tests import test_engagement_desk as _desk
from tests.test_engagement_desk import _fetch, _owner_of, _replied  # noqa: F401

# Shared fixtures, bound by assignment: pytest collects them either way, and a test parameter named
# like an IMPORTED fixture is a redefinition to ruff (F811), which fails the CI lint job.
engine_on = _desk.engine_on
mailbox_double = _desk.mailbox_double

REPLY = ("Sounds interesting, can you send pricing first?\n\n"
         "On Mon, 21 Sep 2026 at 10:00, Sam Rep <sam@seq.com> wrote:\n"
         "> Hi Jane0,\n> This is the email we sent, quoted back at us.\n> Best,\n> Sam")


async def _drafted_context(tid, classification_id, monkeypatch) -> str:
    from nexus.engagement.desk.service import draft_response
    from nexus.engagement.drafting import context

    seen: list[str] = []
    real = context.build_context

    async def recording(*args, **kwargs):
        pack = await real(*args, **kwargs)
        seen.append(pack.text)
        return pack

    monkeypatch.setattr(context, "build_context", recording)
    owner = await _owner_of(tid)
    async with tenant_session(tid) as ts:
        await draft_response(ts, await _fetch(ts, classification_id), user_id=owner)
    return seen[-1]


async def test_the_answer_is_drafted_from_the_thread_the_reply_came_in(mailbox_double, monkeypatch):
    from nexus.models.engagement import EngagementEnrollment

    tid, classification_id, _mailbox = await _replied("ctxthread", monkeypatch, mailbox_double,
                                                       body=REPLY)
    async with tenant_session(tid) as ts:
        # The enrollment has since moved on (a later thread, or none at all).
        enrollment = await ts.first(EngagementEnrollment)
        enrollment.current_thread_id = None
    text = await _drafted_context(tid, classification_id, monkeypatch)
    conversation = text.split("THIS CONVERSATION", 1)[1].split("THE PERSON", 1)[0]
    assert "Nothing has been sent" not in conversation
    assert "WE WROTE" in conversation and "THEY WROTE" in conversation
    assert "can you send pricing first?" in conversation


async def test_the_buyers_own_words_are_shown_without_our_email_quoted_back(mailbox_double,
                                                                             monkeypatch):
    tid, classification_id, _mailbox = await _replied("ctxquote", monkeypatch, mailbox_double,
                                                       body=REPLY)
    text = await _drafted_context(tid, classification_id, monkeypatch)
    they = [line for line in text.splitlines() if "THEY WROTE" in line]
    assert len(they) == 1
    reply_block = text.split(they[0], 1)[1].split("\n- ", 1)[0]
    assert "can you send pricing first?" in reply_block
    assert "quoted back at us" not in text


def test_a_message_that_is_all_quote_keeps_its_text():
    """Bottom-posting, or a client the stripper does not recognise: nothing is better than the
    whole message, so the whole message is kept."""
    from types import SimpleNamespace

    from nexus.engagement.drafting.context import conversation_block

    message = SimpleNamespace(direction="in", subject="Re: hi", sent_at=None, received_at=None,
                              created_at=None, body_text="> only a quote\n> of us")
    assert "only a quote" in conversation_block([message])
