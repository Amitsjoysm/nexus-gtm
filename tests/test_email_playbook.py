"""Drafted emails follow the B2B cold email playbook the product owner supplied (2026-10-01).

What it asks for, and where each part lives:

* a 2-4 word lowercase subject, short paragraphs, one signal, plain text  -> `copy.email_rules`
* one interest-based ask in an unanswered email, two concrete times in a reply -> the CTA rules
* every follow-up adds something new, and the last one closes the loop -> `drafting/context.py`
* the parts a reader can point at are CHECKED, and a miss costs one regeneration -> `email_quality`

The last section is the one that matters: it captures the prompt the model actually receives for
each kind of touch, because a rule that is defined and never sent changes nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexus.agents.llm import LLMProvider, LLMResponse
from tests.conftest import make_tenant, seed_relevance_profile, tenant_session

UTC = timezone.utc
NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)   # a Tuesday

GOOD = (
    "Hi Curtis,\n\n"
    "Saw Marketjoy opened a second SDR pod last month.\n\n"
    "Teams doing that usually hit the same wall on list hygiene, and we take that work off them.\n\n"
    "Worth a look?\n\n"
    "Best,\nJane"
)

#: What the capturing model answers in the engine tests: about THIS account, so the personalisation
#: check (D17) passes and only the rule under test can fail.
ACME = (
    "Hi Jane,\n\n"
    "Saw Acme Robotics raised a $40M Series B.\n\n"
    "New rounds usually mean engineers hired faster than onboarding keeps up, and we take that "
    "work off the team.\n\n"
    "Worth a look?\n\n"
    "Best,\nSam"
)


# ---- the rules ------------------------------------------------------------------------------------

def test_a_cold_email_asks_for_interest_not_a_meeting():
    from nexus.agents.copy import EMAIL_CTA_RULE, EMAIL_RULES

    assert EMAIL_CTA_RULE in EMAIL_RULES
    lowered = EMAIL_CTA_RULE.lower()
    assert "worth a look?" in lowered
    assert "do not ask for a meeting" in lowered and "duration" in lowered


def test_the_email_rules_ask_for_paragraphs_a_short_subject_and_one_signal():
    from nexus.agents.copy import EMAIL_RULES, SIGNAL_RULE, SUBJECT_RULE

    lowered = EMAIL_RULES.lower()
    assert "blank line" in lowered and "paragraphs" in lowered
    assert SUBJECT_RULE in EMAIL_RULES and "2 to 4 words" in SUBJECT_RULE
    assert SIGNAL_RULE in EMAIL_RULES and "one signal" in SIGNAL_RULE.lower()
    assert "plain text" in lowered and "at most one link" in lowered


def test_a_reply_offers_two_times_and_skips_the_cold_rules():
    from nexus.agents.copy import EMAIL_CTA_RULE, SIGNAL_RULE, SUBJECT_RULE, email_rules

    rules = email_rules("response")
    assert "two specific times" in rules
    assert EMAIL_CTA_RULE not in rules and SIGNAL_RULE not in rules
    # Threaded: the subject is the thread's "Re: ...", so a subject rule would only cost a retry.
    assert SUBJECT_RULE not in rules


@pytest.mark.parametrize("touch", ["followup", "signal", "reengage"])
def test_an_unanswered_threaded_touch_keeps_the_soft_ask_and_drops_the_subject_rule(touch):
    from nexus.agents.copy import EMAIL_CTA_RULE, SUBJECT_RULE, email_rules

    rules = email_rules(touch)
    assert EMAIL_CTA_RULE in rules and SUBJECT_RULE not in rules


def test_an_unknown_touch_is_a_first_email():
    from nexus.agents.copy import EMAIL_RULES, email_rules

    assert email_rules("whatever") == EMAIL_RULES


def test_the_call_script_still_books_a_meeting():
    """A call is already a conversation; the soft ask is for email only."""
    from nexus.agents.copy import CALL_CTA_RULE, CALL_RULES, EMAIL_CTA_RULE

    assert CALL_CTA_RULE in CALL_RULES and EMAIL_CTA_RULE not in CALL_RULES
    assert "duration" in CALL_CTA_RULE


# ---- the check ------------------------------------------------------------------------------------

def test_a_playbook_draft_passes_for_every_touch():
    from nexus.agents.email_quality import check_draft

    for touch in ("", "first", "followup", "signal", "reengage", "response"):
        assert check_draft(subject="second sdr pod", body=GOOD, first_name="Curtis",
                           touch=touch) == [], touch


@pytest.mark.parametrize("ask", [
    "Open to 15 minutes on Thursday?", "Worth a quick call?", "Can I book a demo?",
    "Free for a 20-min chat?",
])
def test_a_meeting_ask_in_an_unanswered_email_is_caught(ask):
    from nexus.agents.email_quality import check_draft

    body = GOOD.replace("Worth a look?", ask)
    for touch in ("first", "followup", "signal"):
        problems = " ".join(check_draft(subject="x", body=body, first_name="Curtis", touch=touch))
        assert "meeting" in problems.lower(), (touch, ask)
    # A reply to an interested buyer is exactly where the times belong.
    assert check_draft(subject="x", body=body, first_name="Curtis", touch="response") == []


def test_a_number_in_the_proof_is_not_a_meeting_ask():
    """Only the question sentence is read: "20 minutes" in the proof point is not a meeting."""
    from nexus.agents.email_quality import check_draft

    body = GOOD.replace("Worth a look?", "We cut it from 3 hours to 20 minutes. Worth a look?")
    assert check_draft(subject="x", body=body, first_name="Curtis", touch="first") == []


def test_a_sentence_for_a_subject_is_caught_but_not_on_a_threaded_touch():
    from nexus.agents.email_quality import check_draft

    long = "Saw your team opened a second SDR pod last month"
    assert any("subject" in p.lower()
               for p in check_draft(subject=long, body=GOOD, first_name="Curtis", touch="first"))
    assert check_draft(subject=long, body=GOOD, first_name="Curtis", touch="followup") == []
    # Callers that pass no touch are checked as before.
    assert check_draft(subject=long, body=GOOD, first_name="Curtis") == []


def test_one_block_of_text_is_caught():
    from nexus.agents.email_quality import check_draft

    block = ("Hi Curtis, saw Marketjoy opened a second SDR pod last month. Teams doing that "
             "usually hit the same wall on list hygiene, duplicate records and stale data, and "
             "we take that work off them so the new reps spend their first month selling rather "
             "than cleaning. Worth a look? Best, Jane")
    assert any("paragraph" in p.lower()
               for p in check_draft(subject="x", body=block, first_name="Curtis"))


@pytest.mark.parametrize("phrase", ["just following up", "just bumping", "just checking in"])
def test_an_empty_follow_up_phrase_is_caught(phrase):
    from nexus.agents.email_quality import check_draft

    body = GOOD.replace("Saw Marketjoy", f"I am {phrase}. Saw Marketjoy")
    assert any(phrase in p for p in check_draft(subject="x", body=body, first_name="Curtis"))


def test_the_offline_stub_follows_its_own_rules():
    """The stub is what a deployment with a dead key sends to a real buyer."""
    from nexus.agents.email_quality import check_draft
    from nexus.agents.llm import LLMMessage, StubLLMProvider
    from nexus.agents.messaging import _split_subject

    async def go():
        return await StubLLMProvider().complete(
            [LLMMessage(role="user", content="x")], purpose="outreach_message",
            variables={"account": "Acme", "contact": "Jane Buyer", "value_prop": "Faster GTM",
                       "trigger": "Acme raised a Series B", "pain": "stale lists"})

    import asyncio

    subject, body = _split_subject(asyncio.run(go()).text)
    assert check_draft(subject=subject, body=body, first_name="Jane", touch="first") == []
    assert body.startswith("Hi Jane,")


# ---- each follow-up adds something new ------------------------------------------------------------

def test_follow_ups_change_angle_by_touch_and_the_last_one_closes_the_loop():
    from nexus.engagement.drafting.context import (
        FOLLOWUP_BY_TOUCH,
        LAST_TOUCH,
        followup_instruction,
    )

    assert followup_instruction(2, 5) == FOLLOWUP_BY_TOUCH[2]
    assert followup_instruction(3, 5) == FOLLOWUP_BY_TOUCH[3]
    assert followup_instruction(4, 5) == FOLLOWUP_BY_TOUCH[4]
    assert followup_instruction(5, 5) == LAST_TOUCH
    assert followup_instruction(3, 3) == LAST_TOUCH
    assert followup_instruction(7, 9) == LAST_TOUCH
    # Two emails is too short a sequence to announce the end after one silence.
    assert followup_instruction(2, 2) == FOLLOWUP_BY_TOUCH[2]


# ---- the prompt the model actually receives -------------------------------------------------------

class _Capturing(LLMProvider):
    def __init__(self):
        self.prompts: list[str] = []

    async def complete(self, messages, *, temperature=0.2, max_tokens=800, purpose=None,
                       variables=None) -> LLMResponse:
        self.prompts.append("\n".join(m.content for m in messages))
        return LLMResponse(text=f"Subject: series b\n\n{ACME}", tokens=10)


async def _world():
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementStep,
        MailboxConnection,
    )
    from nexus.models.identity import User
    from nexus.models.signal import SignalEvent

    tid = await make_tenant(slug="playbook", name="Playbook")
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts)
        user = User(email="sam@playbook.com", full_name="Sam Rep", password_hash="x")
        ts.session.add(user)
        await ts.session.flush()
        mailbox = MailboxConnection(owner_user_id=user.id, provider="google",
                                    email="sam@playbook.com", display_name="Sam Rep",
                                    status="connected", timezone="Europe/London")
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        ts.add(SignalEvent(account_id=account.id, kind="funding", source="web",
                           title="Acme Robotics raises $40M Series B", strength=0.9,
                           occurred_at=NOW - timedelta(days=3), dedupe_key="pb-funding"))
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io",
                          title="VP Engineering")
        campaign = EngagementCampaign(name="Q4", owner_user_id=user.id,
                                      mailbox_connection_id=mailbox.id, status="active")
        ts.add(contact)
        ts.add(campaign)
        await ts.flush()
        steps = []
        for index in range(5):
            step = EngagementStep(campaign_id=campaign.id, step_index=index, channel="email")
            ts.add(step)
            steps.append(step)
        enrollment = EngagementEnrollment(campaign_id=campaign.id, contact_id=contact.id,
                                          account_id=account.id, mailbox_connection_id=mailbox.id,
                                          status="active", contact_timezone="Europe/Berlin")
        ts.add(enrollment)
        await ts.flush()
        return tid, [s.id for s in steps], enrollment.id, contact.id, account.id, mailbox.id


@pytest.fixture
def capturing(monkeypatch):
    """Install the capturing model as the process LLM, and put the previous one BACK afterwards.

    `conftest` resets the agent runtime between tests but not the provider, so a bare
    `set_llm_provider` here left this fake answering for every later test on the worker: the reply
    desk's suite, next alphabetically, failed seventeen tests in a full run and passed alone.
    """
    from nexus.agents import llm as llm_module
    from nexus.agents.runtime import reset_agent_runtime

    llm = _Capturing()
    monkeypatch.setattr(llm_module, "_provider", llm)
    reset_agent_runtime()
    yield llm
    reset_agent_runtime()


async def _draft(tid, ids, *, kind, step_number=None):
    from nexus.engagement.drafting.drafter import draft
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementEnrollment, EngagementStep, MailboxConnection

    step_ids, enrollment_id, contact_id, account_id, mailbox_id = ids
    async with tenant_session(tid) as ts:
        step = await ts.get(EngagementStep, step_ids[step_number - 1]) if step_number else None
        return await draft(ts, enrollment=await ts.get(EngagementEnrollment, enrollment_id),
                           contact=await ts.get(Contact, contact_id),
                           account=await ts.get(Account, account_id),
                           mailbox=await ts.get(MailboxConnection, mailbox_id),
                           step=step, kind=kind, now=NOW)


async def test_a_first_email_prompt_carries_the_playbook(capturing):
    from nexus.agents.copy import EMAIL_CTA_RULE, SIGNAL_RULE, STRUCTURE_RULE, SUBJECT_RULE

    tid, *ids = await _world()
    written = await _draft(tid, ids, kind="first", step_number=1)
    assert written.ok and written.problems == [], written
    prompt = capturing.prompts[-1]
    for rule in (STRUCTURE_RULE, SUBJECT_RULE, SIGNAL_RULE, EMAIL_CTA_RULE):
        assert rule in prompt
    assert "Write a cold email" in prompt


async def test_each_follow_up_prompt_names_its_place_and_what_it_adds(capturing):
    from nexus.agents.copy import EMAIL_CTA_RULE, SUBJECT_RULE
    from nexus.engagement.drafting.context import FOLLOWUP_BY_TOUCH, LAST_TOUCH

    tid, *ids = await _world()
    expected = {2: FOLLOWUP_BY_TOUCH[2], 3: FOLLOWUP_BY_TOUCH[3], 4: FOLLOWUP_BY_TOUCH[4],
                5: LAST_TOUCH}
    for number, instruction in expected.items():
        await _draft(tid, ids, kind="followup", step_number=number)
        prompt = capturing.prompts[-1]
        assert f"This is email {number} of 5." in prompt and instruction in prompt, number
        assert "Write a follow-up email" in prompt
        assert EMAIL_CTA_RULE in prompt and SUBJECT_RULE not in prompt


async def test_a_reply_prompt_offers_times_instead_of_the_cold_ask(capturing):
    from nexus.agents.copy import EMAIL_CTA_RULE, REPLY_CTA_RULE

    tid, *ids = await _world()
    await _draft(tid, ids, kind="response")
    prompt = capturing.prompts[-1]
    assert REPLY_CTA_RULE in prompt and EMAIL_CTA_RULE not in prompt
    assert "Write a reply" in prompt
    assert "two specific times" in prompt


async def test_the_composer_draft_is_a_first_email_too(capturing):
    """Outside the engine (the contact composer, plays) nobody passes a touch: it is a first email."""
    from nexus.agents.copy import EMAIL_CTA_RULE, SUBJECT_RULE
    from nexus.agents.runtime import get_agent_runtime

    tid, _steps, _enrollment, contact_id, account_id, _mailbox = await _world()
    async with tenant_session(tid) as ts:
        result = await get_agent_runtime().run("messaging", ts, account_id=account_id,
                                               contact_id=contact_id)
    assert result.status == "completed", result.error
    assert EMAIL_CTA_RULE in capturing.prompts[-1] and SUBJECT_RULE in capturing.prompts[-1]


async def test_a_draft_that_asks_for_a_meeting_is_regenerated_with_the_reason(capturing):
    """The check is what makes the rule real: the model's meeting ask is sent back once, naming it."""
    tid, *ids = await _world()
    meeting = ACME.replace("Worth a look?", "Open to 15 minutes Thursday?")
    replies = [f"Subject: series b\n\n{meeting}", f"Subject: series b\n\n{ACME}"]

    async def complete(messages, **kwargs):
        capturing.prompts.append("\n".join(m.content for m in messages))
        return LLMResponse(text=replies[min(len(capturing.prompts), 2) - 1], tokens=10)

    capturing.complete = complete
    written = await _draft(tid, ids, kind="first", step_number=1)
    assert len(capturing.prompts) == 2
    assert "requests a meeting" in capturing.prompts[1]
    assert "Worth a look?" in written.body and written.problems == []


# ---- the contact composer signs with the rep's own name -------------------------------------------

async def test_the_composer_draft_is_signed_by_the_rep_who_asked(client, capturing):
    """A live composer draft on 2026-10-01 signed "Best, Alex": the composer calls the agent
    directly, so the sender block the engagement drafter adds never reached it."""
    from tests.conftest import auth, principal_from_token, signup

    token = await signup(client, slug="composer", email="sam@composer.com", company="Composer")
    async with tenant_session(principal_from_token(token).tenant_id) as ts:
        await seed_relevance_profile(ts)
    account = await client.post("/api/accounts", headers=auth(token),
                                json={"name": "Acme Robotics", "domain": "acme.io"})
    contact = await client.post(f"/api/accounts/{account.json()['id']}/contacts",
                                headers=auth(token),
                                json={"full_name": "Jane Buyer", "email": "jane@acme.io"})
    r = await client.post("/api/agents/messaging/run", headers=auth(token),
                          json={"account_id": account.json()["id"],
                                "inputs": {"contact_id": contact.json()["id"]}})
    assert r.status_code == 200, r.text
    # `signup` names the owner "Rep".
    assert "YOU (THE SENDER)\n- Name: Rep" in capturing.prompts[-1]
    assert "your first name, Rep." in capturing.prompts[-1]


def test_markdown_line_breaks_are_removed():
    """A live draft ended every line with two spaces (markdown's forced break)."""
    from nexus.agents.copy import tidy_text

    assert tidy_text("Hi David,  \nOur AI\u2011powered scoring.  \n\nBest,  \nSam  ") == \
        "Hi David,\nOur AI-powered scoring.\n\nBest,\nSam"
