"""The content writers' email and call-script upgrade (2026-10-29, W7 of the SDR round 2 plan).

Sixteen items, each pinned here where a reader can point at it. Three groups matter most:

* **one anchor, more facts, only real facts** (7, 12, 9): one signal drives the hook, the value prop
  and `trigger_signal_id`; fit reasons and research highlights reach the prompt; and every number
  and proper noun in a draft must be in what the model was given. A house-style sample's "Globex cut
  onboarding 40%" leaking into another buyer's draft is the test case.
* **a reason and a thing** (1, 2): what the product is, and the problem as a pattern, not a claim.
* **less sameness** (3, 4, 10): no quoted examples in the rules, the AI tells banned and checked,
  and a seeded shape per contact.

The P.S. (16) is off by default and reaches a buyer only when a rep ticks "I confirm this is true".
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from nexus.agents.llm import LLMProvider, LLMResponse
from tests.conftest import make_tenant, seed_relevance_profile, tenant_session

UTC = timezone.utc
NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)   # a Tuesday

GOOD = (
    "Hi Jane,\n\n"
    "Saw Acme Robotics raised a $40M Series B.\n\n"
    "Teams that grow that fast usually find onboarding breaks first. We take that work off the "
    "team.\n\n"
    "Worth a look?\n\n"
    "Best,\nSam"
)

VALUE_PROPS = [
    {"name": "Capital efficiency", "description": "Spend tracking for finance teams.",
     "pains_solved": ["growth spend nobody can trace"]},
    {"name": "Faster ramp", "description": "Onboarding plans that get new hires productive.",
     "pains_solved": ["slow onboarding for new hires"]},
]


class _Capturing(LLMProvider):
    """Answers with the queued replies in turn (the last one repeats) and keeps every prompt."""

    def __init__(self, replies: list[str]):
        self.replies = replies
        self.prompts: list[str] = []

    async def complete(self, messages, *, temperature=0.2, max_tokens=800, purpose=None,
                       variables=None) -> LLMResponse:
        self.prompts.append("\n".join(m.content for m in messages))
        return LLMResponse(text=self.replies[min(len(self.prompts), len(self.replies)) - 1],
                           tokens=10)


@pytest.fixture
def model(monkeypatch):
    """Install a capturing model; put the process provider back afterwards (see the playbook test
    for what a leaked fake did to the next suite on the worker)."""
    from nexus.agents import llm as llm_module
    from nexus.agents.runtime import reset_agent_runtime

    def install(*replies: str) -> _Capturing:
        llm = _Capturing(list(replies))
        monkeypatch.setattr(llm_module, "_provider", llm)
        reset_agent_runtime()
        return llm

    yield install
    reset_agent_runtime()


def _email(body: str = GOOD, subject: str = "series b") -> str:
    return f"Subject: {subject}\n\n{body}"


async def _world(slug: str, **profile):
    """A workspace with two value props, an account with a funding signal and a hiring signal tied
    to the contact (weaker, but about her), an SDR and a mailbox."""
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import MailboxConnection
    from nexus.models.identity import User
    from nexus.models.signal import SignalEvent

    tid = await make_tenant(slug=slug, name=slug.title())
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, value_props=VALUE_PROPS, **profile)
        user = User(email=f"sam@{slug}.com", full_name="Sam Rep", password_hash="x")
        ts.session.add(user)
        await ts.session.flush()
        mailbox = MailboxConnection(owner_user_id=user.id, provider="google",
                                    email=f"sam@{slug}.com", display_name="Sam Rep",
                                    status="connected", timezone="Europe/London")
        account = Account(name="Acme Robotics", domain="acme.io", owner_user_id=user.id,
                          employee_count=120)
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email="jane@acme.io",
                          title="VP Engineering")
        ts.add(contact)
        await ts.flush()
        funding = SignalEvent(account_id=account.id, kind="funding", source="web",
                              title="Acme Robotics raises $40M Series B", strength=0.9,
                              occurred_at=NOW - timedelta(days=3), dedupe_key=f"{slug}-f")
        hiring = SignalEvent(account_id=account.id, contact_id=contact.id, kind="hiring",
                             source="web", title="Jane Buyer is hiring 12 engineers",
                             strength=0.6, occurred_at=NOW - timedelta(days=2),
                             dedupe_key=f"{slug}-h")
        ts.add(funding)
        ts.add(hiring)
        await ts.flush()
        return SimpleNamespace(tid=tid, user=user.id, mailbox=mailbox.id, account=account.id,
                               contact=contact.id, funding=funding.id, hiring=hiring.id)


async def _compose(w, **inputs):
    from nexus.agents.runtime import get_agent_runtime

    async with tenant_session(w.tid) as ts:
        result = await get_agent_runtime().run("messaging", ts, account_id=w.account,
                                               contact_id=w.contact, **inputs)
    assert result.status == "completed", result.error
    return result.output


# ---- 1. what we do ---------------------------------------------------------------------------

def test_the_structure_asks_for_one_plain_sentence_of_what_we_do():
    from nexus.agents.copy import STRUCTURE_RULE

    assert "one plain sentence saying what we do" in STRUCTURE_RULE


async def test_the_value_props_description_reaches_the_prompt(model):
    llm = model(_email())
    w = await _world("whatwedo")
    await _compose(w)
    assert "What we do (say it in one plain sentence, in a buyer's words): Onboarding plans" \
        in llm.prompts[-1]


# ---- 2. the problem as a pattern, never a claim about them ------------------------------------

def test_the_problem_is_asked_for_as_a_pattern():
    from nexus.agents.copy import STRUCTURE_RULE

    assert "as a pattern you have seen rather than as a fact about them" in STRUCTURE_RULE


@pytest.mark.parametrize("claim", [
    "Your team is struggling with onboarding.",
    "You're probably wasting hours on manual lists.",
    "You are losing deals to slow follow-up.",
])
def test_a_claim_about_their_problem_is_caught(claim):
    from nexus.agents.email_quality import check_draft

    body = GOOD.replace("Teams that grow that fast usually find onboarding breaks first.", claim)
    assert any("as a fact about them" in p
               for p in check_draft(subject="x", body=body, first_name="Jane"))
    assert check_draft(subject="x", body=GOOD, first_name="Jane") == []


# ---- 3. no literal examples in the rules or the complaints ------------------------------------

def test_the_rules_describe_and_never_quote_an_example():
    from nexus.agents import copy

    for rule in (copy.SUBJECT_RULE, copy.STRUCTURE_RULE, copy.EMAIL_CTA_RULE,
                 copy.MEETING_CTA_RULE, copy.REPLY_CTA_RULE, copy.SIGNAL_RULE,
                 *copy.OPENER_STYLES.values(), *copy.ASK_STYLES.values(),
                 *copy.SUBJECT_STYLES.values()):
        lowered = rule.lower()
        assert "for example" not in lowered and "such as '" not in lowered, rule
        assert "e.g." not in lowered, rule
        # A quoted question or quoted lowercase subject is an example the model would copy.
        assert not re.search(r"'[^']*\?'", rule), rule
    for gone in ("eu expansion", "new sdr pod", "worth a look?", "open to seeing how?",
                 "'hi sam,'"):
        assert gone not in copy.EMAIL_RULES.lower()


def test_the_retry_complaints_quote_no_example_either():
    from nexus.agents.email_quality import check_draft

    body = "Saw the news.\n\nCan we do 15 minutes on Thursday for a demo?"
    problems = " ".join(check_draft(subject="a whole sentence of a subject line here", body=body,
                                    first_name="Jane", touch="first"))
    assert problems                                  # there is plenty wrong with it
    for quoted in ("for example", "'hi", "'best,'", "worth a look", "new sdr pod"):
        assert quoted not in problems.lower()


# ---- 4. AI tells banned in the prompt AND checked ---------------------------------------------

def test_the_check_and_the_prompt_read_one_banned_list():
    from nexus.agents import copy, email_quality

    assert email_quality.BANNED_PHRASES is copy.BANNED_PHRASES
    for phrase in copy.BANNED_PHRASES:
        assert f"'{phrase}'" in copy.EMAIL_RULES


@pytest.mark.parametrize("tell", [
    "I came across your post", "That is impressive work", "We streamline onboarding",
    "a seamless handoff", "I'd love to show you", "I’d love to show you", "to unlock growth",
])
def test_each_ai_tell_is_caught(tell):
    from nexus.agents.email_quality import check_draft

    body = GOOD.replace("We take that work off the team.", f"{tell}.")
    assert any("automated" in p for p in check_draft(subject="x", body=body, first_name="Jane"))


# ---- 5. reference the idea, never the quote: one rule everywhere ------------------------------

def test_the_person_brief_and_the_signal_rule_say_the_same_thing():
    from nexus.agents.copy import ACTIVITY_RULE, SIGNAL_RULE
    from nexus.personalization.brief import PersonBrief

    brief = PersonBrief(name="Jane Buyer", title="VP Engineering", seniority=None,
                        linkedin_url=None, role_angle="x", signal_title=None,
                        signal_is_personal=False,
                        insights={"recent_posts": ["We rebuilt our onboarding from scratch."]})
    assert ACTIVITY_RULE in SIGNAL_RULE
    for channel in ("email", "call"):
        prompt = brief.to_prompt(channel=channel)
        assert ACTIVITY_RULE in prompt
        assert "in their own words" not in prompt


def test_quoting_their_post_back_is_caught_in_an_email_and_a_script():
    from nexus.agents.email_quality import check_call_script, check_draft

    post = "We rebuilt our whole onboarding programme from scratch this quarter."
    quoting = GOOD.replace("Saw Acme Robotics raised a $40M Series B.",
                           "Loved that you rebuilt our whole onboarding programme from scratch.")
    assert any("word for word" in p for p in check_draft(subject="x", body=quoting,
                                                         first_name="Jane", activity=[post]))
    assert not any("word for word" in p for p in check_draft(subject="x", body=GOOD,
                                                             first_name="Jane", activity=[post]))
    script = {"opener": "Hi Jane, it's Sam. Can I take thirty seconds?",
              "hook": "You rebuilt our whole onboarding programme from scratch."}
    assert any("word for word" in p for p in check_call_script(script, activity=[post]))


# ---- 6. never cite size or revenue ------------------------------------------------------------

def test_the_bands_are_labelled_as_register_only():
    from nexus.agents.copy import FIRMOGRAPHICS_RULE, account_facts
    from nexus.models.account import Account

    facts = account_facts(Account(tenant_id="t", name="Acme", employee_count=120,
                                  annual_revenue=20_000_000))
    assert "Size (sets your register; never mention it): 50-200 employees" in facts
    assert "Scale (sets your register; never mention it): $10-50M revenue" in facts
    assert FIRMOGRAPHICS_RULE in __import__("nexus.agents.copy", fromlist=["x"]).EMAIL_RULES


@pytest.mark.parametrize("cited", [
    "As a 50-200 employees company you feel this.", "A 40-person team feels this.",
    "At $10-50M revenue this matters.", "With revenue of $20M it matters.",
    "Teams of 5,000+ employees feel this.",
])
def test_citing_their_size_or_revenue_is_caught(cited):
    from nexus.agents.email_quality import check_draft

    body = GOOD.replace("We take that work off the team.", cited)
    assert any("size or revenue" in p for p in check_draft(subject="x", body=body,
                                                           first_name="Jane"))


@pytest.mark.parametrize("fine", ["Saw you are hiring 12 engineers.",
                                  "Saw you raised over $40M.", "Hiring 40 support staff."])
def test_a_signal_is_not_mistaken_for_their_size(fine):
    from nexus.agents.email_quality import check_draft

    body = GOOD.replace("Saw Acme Robotics raised a $40M Series B.", fine)
    assert not any("size or revenue" in p for p in check_draft(subject="x", body=body,
                                                               first_name="Jane"))


# ---- 7. one anchor drives the hook, the value prop and trigger_signal_id ----------------------

def _sig(**kw):
    kw.setdefault("contact_id", None)
    kw.setdefault("body", "")
    kw.setdefault("kind", "news")
    return SimpleNamespace(**kw)


def test_a_signal_about_the_person_is_the_anchor_even_when_weaker():
    from nexus.agents.copy import pick_anchor

    funding = _sig(id="f", title="Raised $40M", strength=0.9)
    hiring = _sig(id="h", title="Jane is hiring 12 engineers", strength=0.6, contact_id="c1")
    assert pick_anchor([funding, hiring], SimpleNamespace(id="c1")) is hiring
    assert pick_anchor([funding, hiring], SimpleNamespace(id="c2")) is funding
    assert pick_anchor([], None) is None


def test_the_value_prop_matches_the_kind_of_trigger_not_only_its_words():
    """A funding headline names investors, not the growth they pay for; its KIND does."""
    from nexus.agents.copy import select_value_prop

    funding = _sig(kind="funding", title="Acme closes round led by Sequoia", strength=0.9)
    assert select_value_prop(VALUE_PROPS, [funding])["name"] == "Capital efficiency"
    hiring = _sig(kind="hiring", title="Acme opens 12 roles", strength=0.6)
    assert select_value_prop(VALUE_PROPS, [hiring])["name"] == "Faster ramp"


def test_the_role_breaks_a_tie_instead_of_falling_back_to_the_first():
    from nexus.agents.copy import select_value_prop
    from nexus.personalization.brief import role_words

    news = _sig(kind="news", title="Acme opens a Berlin office", strength=0.5)
    assert select_value_prop(VALUE_PROPS, [news],
                             role=role_words("CFO", None))["name"] == "Capital efficiency"
    assert select_value_prop(VALUE_PROPS, [news],
                             role=role_words("Head of People", None))["name"] == "Faster ramp"


async def test_one_email_opens_pitches_and_logs_the_same_signal(model):
    llm = model(_email(GOOD.replace("Saw Acme Robotics raised a $40M Series B.",
                                    "Saw you are hiring 12 engineers.")))
    w = await _world("anchor")
    out = await _compose(w)
    prompt = llm.prompts[-1]
    assert out["trigger_signal_id"] == w.hiring
    assert out["value_prop"] == "Faster ramp"
    assert "The trigger to open on: Jane Buyer is hiring 12 engineers" in prompt
    assert "Open on what's timely: they personally — Jane Buyer is hiring 12 engineers" in prompt
    anchor_block = prompt.split("THE TRIGGER (the one signal this email is about):", 1)[1]
    assert anchor_block.split("\n\n", 1)[0].count("- [") == 1
    assert "OTHER SIGNALS (background only; do not open on them)" in prompt


# ---- 8. the gate's own bugs -------------------------------------------------------------------

@pytest.mark.parametrize("opening", ["Hiring 12 engineers is a big step.", "Historically, teams"])
def test_a_greeting_is_a_word_not_a_prefix(opening):
    from nexus.agents.email_quality import check_draft

    body = GOOD.replace("Hi Jane,", opening)
    assert any("no greeting" in p for p in check_draft(subject="x", body=body, first_name="Jane"))


def test_the_name_in_the_greeting_is_a_whole_word():
    from nexus.agents.email_quality import check_draft

    assert any("first name" in p for p in check_draft(subject="x", body=GOOD.replace(
        "Hi Jane,", "Hi Janet,"), first_name="Jane"))


async def test_the_retry_shows_the_draft_it_is_correcting(model):
    bad = GOOD.replace("Hi Jane,\n\n", "")
    llm = model(_email(bad), _email())
    w = await _world("retryshow")
    await _compose(w)
    assert len(llm.prompts) == 2
    assert "Your previous draft:\n-----\nSubject: series b\n\nSaw Acme Robotics" in llm.prompts[1]
    assert "no greeting" in llm.prompts[1]


# ---- 9. the fact verifier ---------------------------------------------------------------------

SAMPLE = "Hi Ann,\n\nWe helped Globex cut onboarding 40%. Worth a look?\n\nBest,\nJane"
LEAKED = GOOD.replace("We take that work off the team.",
                      "We helped Globex cut onboarding 40% last year.")


def test_a_name_and_a_number_nobody_gave_are_found():
    from nexus.agents.email_quality import unverified_terms

    context = "Acme Robotics raises $40M Series B. Jane Buyer, VP Engineering. Thursday 24."
    assert unverified_terms("We helped Globex cut onboarding 37%.", context) == ["Globex", "37%"]
    # What was given passes: the company, the round, the amount.
    assert unverified_terms(GOOD, context, allow=["Sam"]) == []


def test_a_number_keeps_its_kind():
    """The sample's "40%" is not the account's "$40M": comparing digits alone let a fabricated
    percentage through on any account that had raised the same number of millions."""
    from nexus.agents.email_quality import unverified_terms

    context = "Acme Robotics raises $40M Series B, hiring 12 engineers, up 30% this year."
    assert unverified_terms("We helped Globex cut onboarding 40%.", context) == ["Globex", "40%"]
    assert unverified_terms("They hired 30 people and grew 12%.", context) == ["12%"]
    assert unverified_terms("Saw the $40 million round and the 12 roles.", context) == []
    assert unverified_terms("A $12M budget.", context) == ["$12M"]


def test_a_ps_is_checked_from_its_first_word():
    from nexus.agents.email_quality import unverified_terms

    context = "Acme Robotics raises $40M Series B."
    assert unverified_terms("Globex saw faster onboarding.", context) == []
    assert unverified_terms("Globex saw faster onboarding.", context, strict=True) == ["Globex"]
    assert unverified_terms("Congrats on the $40M Series B.", context, strict=True) == []


def test_weekdays_months_times_and_the_meeting_length_are_not_facts():
    from nexus.agents.email_quality import check_draft

    reply = ("Hi Jane,\n\nThanks for the note.\n\nWould Thursday 24 September at 10am or Friday "
             "at 2:30 pm work for 15 minutes?\n\nBest,\nSam")
    assert check_draft(subject="x", body=reply, first_name="Jane", touch="response",
                       context="Jane Buyer at Acme Robotics", allow=["Sam Rep"]) == []
    # A duration outside the ask is a claim, and is checked.
    claim = reply.replace("Thanks for the note.", "We cut reporting to 20 minutes.")
    assert any("20" in p for p in check_draft(subject="x", body=claim, first_name="Jane",
                                              touch="response", context="Jane Buyer"))


async def test_a_samples_customer_and_metric_leaking_into_another_draft_cost_a_retry(model):
    """The house-style sample is in the prompt, and is NOT what the verifier counts as given."""
    from nexus.models.identity import Tenant

    llm = model(_email(LEAKED), _email())
    w = await _world("globex")
    async with tenant_session(w.tid) as ts:
        tenant = await ts.session.get(Tenant, w.tid)
        tenant.email_settings = {"style": {"samples": [SAMPLE]}}
    out = await _compose(w)
    assert "Globex" in llm.prompts[0]                        # the sample reached the model
    assert len(llm.prompts) == 2                             # ...and its leak cost one retry
    # 40% is caught although the account raised $40M: a percentage is not an amount.
    assert "not in the facts given: Globex, 40%." in llm.prompts[1]
    assert "Globex" not in out["body"]


async def test_an_automatic_follow_up_that_still_leaks_is_held(model):
    """A miss the retry did not fix stays a problem on the draft, and `advance.py` holds any
    follow-up with problems for review instead of sending it."""
    from nexus.engagement.drafting.drafter import draft
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        MailboxConnection,
    )

    model(_email(LEAKED))
    w = await _world("heldleak")
    async with tenant_session(w.tid) as ts:
        campaign = EngagementCampaign(name="Q4", owner_user_id=w.user,
                                      mailbox_connection_id=w.mailbox, status="active")
        ts.add(campaign)
        await ts.flush()
        enrollment = EngagementEnrollment(campaign_id=campaign.id, contact_id=w.contact,
                                          account_id=w.account, mailbox_connection_id=w.mailbox,
                                          status="active")
        ts.add(enrollment)
        await ts.flush()
        written = await draft(ts, enrollment=enrollment, contact=await ts.get(Contact, w.contact),
                              account=await ts.get(Account, w.account),
                              mailbox=await ts.get(MailboxConnection, w.mailbox),
                              kind="followup", now=NOW)
    assert written.ok
    assert any("Globex" in p for p in written.problems), written.problems


# ---- 10. a seeded shape per contact -----------------------------------------------------------

def test_the_shape_is_stable_per_contact_and_varied_across_a_list():
    from nexus.agents.copy import ASK_STYLES, OPENER_STYLES, SUBJECT_STYLES, shape_for

    assert shape_for("contact-1") == shape_for("contact-1")
    shapes = [shape_for(f"contact-{i}") for i in range(60)]
    assert {s["opener"] for s in shapes} == set(OPENER_STYLES)
    assert {s["ask"] for s in shapes} == set(ASK_STYLES)
    assert {s["subject"] for s in shapes} == set(SUBJECT_STYLES)


def test_the_shape_applies_where_it_belongs():
    from nexus.agents.copy import ASK_STYLES, OPENER_STYLES, email_rules

    shape = {"opener": "question", "ask": "how", "subject": "team"}
    first = email_rules("first", shape=shape)
    assert OPENER_STYLES["question"] in first and ASK_STYLES["how"] in first
    follow = email_rules("followup", shape=shape)
    assert OPENER_STYLES["question"] not in follow and ASK_STYLES["how"] in follow
    assert ASK_STYLES["how"] not in email_rules("response", shape=shape)
    assert ASK_STYLES["how"] not in email_rules("first", shape=shape, first_ask="meeting")


def test_the_offline_stub_follows_each_shape_and_its_own_checks():
    import asyncio

    from nexus.agents.copy import shape_for
    from nexus.agents.email_quality import check_draft
    from nexus.agents.llm import LLMMessage, StubLLMProvider
    from nexus.agents.messaging import _split_subject

    seen = set()
    for i in range(30):
        shape = shape_for(f"c{i}")
        text = asyncio.run(StubLLMProvider().complete(
            [LLMMessage(role="user", content="x")], purpose="outreach_message",
            variables={"account": "Acme", "contact": "Jane Buyer", "value_prop": "Faster GTM",
                       "trigger": "Acme raised a Series B", "pain": "stale lists",
                       "what": "Signal to action in one move.", "shape": shape})).text
        subject, body = _split_subject(text)
        assert check_draft(subject=subject, body=body, first_name="Jane", touch="first") == []
        seen.add(text)
    assert len(seen) > 5, "the stub sent every buyer the identical email"


async def test_the_shape_is_logged_on_the_message(model):
    from nexus.engagement.sequences.service import draft_first_emails
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        EngagementStep,
    )

    model(_email())
    w = await _world("shapelog")
    async with tenant_session(w.tid) as ts:
        campaign = EngagementCampaign(name="Q4", owner_user_id=w.user,
                                      mailbox_connection_id=w.mailbox, status="draft")
        ts.add(campaign)
        await ts.flush()
        ts.add(EngagementStep(campaign_id=campaign.id, step_index=0, channel="email"))
        ts.add(EngagementEnrollment(campaign_id=campaign.id, contact_id=w.contact,
                                    account_id=w.account, mailbox_connection_id=w.mailbox,
                                    status="awaiting_review"))
        await ts.flush()
        assert (await draft_first_emails(ts, campaign))["drafted"] == 1
        row = await ts.first(EngagementMessage, EngagementMessage.contact_id == w.contact)
    from nexus.agents.copy import shape_for, shape_label

    shapes = [f for f in row.draft_facts if f["kind"] == "shape"]
    assert shapes == [{"label": shape_label(shape_for(w.contact)), "url": "", "kind": "shape"}]


# ---- 11. role matching: whole words, seniority AND function -----------------------------------

@pytest.mark.parametrize(("title", "not_this"), [
    ("Vice President of Engineering", "executive outcomes"),
    ("Senior Vice President, Sales", "executive outcomes"),
    ("Product Owner", "executive outcomes"),
    ("Lead Generation Specialist", "operational quick wins"),
])
def test_a_word_inside_a_title_is_not_its_seniority(title, not_this):
    from nexus.personalization.brief import _role_angle

    assert not_this not in _role_angle(title, None)


def test_the_real_seniority_still_reads():
    from nexus.personalization.brief import role_profile

    assert role_profile("President & CEO", None)["seniority"] == "executive"
    assert role_profile("Owner", None)["seniority"] == "executive"
    assert role_profile("Vice President of Engineering", None)["seniority"] == "leader"
    assert role_profile("Sales Team Lead", None)["seniority"] == "manager"


def test_a_vp_sales_and_a_vp_engineering_get_different_angles():
    from nexus.personalization.brief import _role_angle

    sales, engineering = _role_angle("VP Sales", "VP"), _role_angle("VP Engineering", "VP")
    assert sales != engineering
    assert "quota" in sales and "delivery speed" in engineering
    assert "function-level" in sales and "function-level" in engineering


# ---- 12. fit reasons and research highlights reach the prompt ---------------------------------

def test_only_usable_fit_reasons_are_kept():
    from nexus.agents.copy import fit_lines

    fit = SimpleNamespace(reasons=[
        "industry 'SaaS' is in ICP", "headcount 120 within 50-500", "geo 'Germany' in ICP",
        "uses Salesforce, HubSpot", "tech stack unknown; not counted against fit",
        "industry 'Retail' not in ICP", "revenue 20,000,000 within range",
    ])
    lines = fit_lines(fit)
    assert lines == ["Their industry, SaaS, is one we sell to.",
                     "They are in Germany, a market we sell to.",
                     "They use Salesforce, HubSpot, tools our ideal customer profile names."]
    assert not any("120" in line or "20,000,000" in line for line in lines)


def test_only_usable_research_lines_are_kept_and_three_at_most():
    from nexus.agents.copy import extra_fact_lines, research_lines

    facts = [
        "Acme opened a second plant in Monterrey to serve automotive customers.",
        "https://acme.io/news/2026/plant",
        "Acme ...",
        "Acme's CEO said the company would double its field service team this year...",
        "No external facts retrieved.",
        "Acme signed a supply deal with a large European rail operator in March.",
        "Acme moved its headquarters to Austin to be closer to its largest customers.",
        "Acme launched a predictive maintenance product for its installed base.",
    ]
    kept = research_lines(facts)
    assert kept[0].startswith("Acme opened a second plant")
    assert not any("http" in k or k.endswith("...") or "No external" in k for k in kept)
    block = extra_fact_lines(research=facts, fit=SimpleNamespace(reasons=["uses Salesforce"]))
    assert block.count("\n- ") == 3


async def test_the_latest_research_brief_reaches_the_email_prompt(model):
    from nexus.models.intelligence import AgentRun

    llm = model(_email())
    w = await _world("research")
    async with tenant_session(w.tid) as ts:
        ts.add(AgentRun(tenant_id=w.tid, agent="research", account_id=w.account,
                        status="completed", output={"facts": [
                            "Acme opened a second plant in Monterrey to serve automotive buyers.",
                            "https://acme.io/news"]}))
        await ts.flush()
    await _compose(w)
    prompt = llm.prompts[-1]
    assert "MORE WE KNOW (use one only if it sharpens the email):" in prompt
    assert "second plant in Monterrey" in prompt and "https://acme.io/news" not in prompt


# ---- 13-15. the call script -------------------------------------------------------------------

def _script(**over) -> dict:
    script = {
        "opener": "Hi Jane, it's Sam from Playbook. This is a cold call. Can I take thirty "
                  "seconds to say why I called?",
        "hook": "Teams that hire twelve engineers at once usually find onboarding breaks first.",
        "value_prop": "We give new hires a plan for their first month.",
        "discovery_questions": ["How are new engineers onboarded today?"],
        "objections": [{"objection": "Not now.", "response": "Fair. Worth 15 minutes in May?"}],
        "first_reactions": [
            {"reaction": "Who is this?", "response": "Sam from Playbook, about onboarding."},
            {"reaction": "Just send me an email.", "response": "Happy to. Is onboarding a "
                                                                "priority?"},
            {"reaction": "I'm busy.", "response": "Understood. Is onboarding on your list?"},
        ],
        "cta": "Could we find 15 minutes on Thursday?",
        "voicemail": "Hi Jane, it's Sam from Playbook, about onboarding. I'll send a short email.",
    }
    script.update(over)
    return script


def test_a_good_script_passes_the_check():
    from nexus.agents.email_quality import check_call_script

    assert check_call_script(_script()) == []


@pytest.mark.parametrize(("over", "complaint"), [
    ({"opener": "Hi Jane " + "and more words " * 15 + "?"}, "ten seconds"),
    ({"opener": "Hi Jane, it's Sam from Playbook calling about onboarding."}, "ask permission"),
    ({"voicemail": "Hi Jane " + "and more words " * 25}, "twenty-five seconds"),
    ({"opener": "Hi Jane, this is <your name> from <your company>. Thirty seconds?"},
     "placeholder"),
    ({"hook": "How are you today? I wanted to reach out."}, "how are you today"),
    ({"first_reactions": []}, "first reactions"),
    ({"cta": "Would a short chat be valuable for you?"}, "valuable or helpful"),
])
def test_each_spoken_fault_is_named(over, complaint):
    from nexus.agents.email_quality import check_call_script

    problems = " ".join(check_call_script(_script(**over)))
    assert complaint in problems, problems


def test_the_fallback_script_has_no_placeholder_and_no_stock_line():
    from nexus.agents.call_script import _coerce
    from nexus.agents.email_quality import check_call_script

    for caller, company in (("Sam Rep", "Playbook"), ("", ""), ("Sam Rep", "")):
        out = _coerce('{"opener": "Hi', account="Acme", contact="Jane Buyer", caller=caller,
                      company=company, pain="slow onboarding")
        assert check_call_script(out) == [], (caller, company, out)
        assert "Sam" in out["opener"] if caller else "Sam" not in out["opener"]


def test_a_models_placeholder_is_filled_with_the_real_caller():
    from nexus.agents.call_script import _coerce

    raw = json.dumps(_script(opener="Hi Jane, this is [Your Name] from <Your Company>. "
                                    "Can I take thirty seconds?"))
    out = _coerce(raw, account="Acme", contact="Jane", caller="Sam Rep", company="Playbook")
    assert out["opener"] == "Hi Jane, this is Sam from Playbook. Can I take thirty seconds?"


def test_the_call_rules_ask_for_a_permission_opener_and_the_three_reactions():
    from nexus.agents.copy import CALL_RULES, FIRST_REACTIONS

    assert "asks permission" in CALL_RULES and "cold call in plain words" in CALL_RULES
    for reaction in FIRST_REACTIONS:
        assert f"'{reaction}'" in CALL_RULES
    assert "come back to one of the problems we remove" in CALL_RULES


async def test_the_offline_stub_script_names_the_caller_and_passes_its_check():
    from nexus.agents.email_quality import check_call_script
    from nexus.agents.llm import LLMMessage, StubLLMProvider

    for caller, company in (("Sam Rep", "Playbook"), ("", "")):
        raw = (await StubLLMProvider().complete(
            [LLMMessage(role="user", content="x")], purpose="call_script",
            variables={"account": "Acme", "contact": "Jane Buyer", "value_prop": "Faster ramp",
                       "trigger": "Acme raised a Series B", "pain": "slow onboarding",
                       "one_pain": "slow onboarding", "caller": caller,
                       "company": company})).text
        script = json.loads(raw)
        assert "<your" not in raw.lower()
        assert check_call_script(script) == [], script


async def test_the_script_prompt_names_the_caller_and_a_bad_script_is_redone_once(model):
    # The workspace is "Callscript" (`_world` names it from the slug): a script naming any other
    # company is caught by the verifier, which is how this test first failed.
    good = json.loads(json.dumps(_script()).replace("Playbook", "Callscript"))
    # A placeholder alone would not do: `_coerce` fills "<your name>" with the real caller. A
    # stock line is the model's own fault, and costs the one retry.
    llm = model(json.dumps({**good, "opener": "Hi Jane, how are you today? It's Sam."}),
                json.dumps(good))
    w = await _world("callscript")
    from nexus.agents.runtime import get_agent_runtime

    async with tenant_session(w.tid) as ts:
        result = await get_agent_runtime().run("call_script", ts, account_id=w.account,
                                               contact_id=w.contact, persist=False,
                                               caller_user_id=w.user)
    assert result.status == "completed", result.error
    assert "YOU (THE CALLER)\n- Name: Sam Rep. Introduce yourself by your first name, Sam." \
        in llm.prompts[0]
    assert "- Company: Callscript" in llm.prompts[0]
    assert len(llm.prompts) == 2 and "Your previous script:" in llm.prompts[1]
    out = result.output
    assert out["problems"] == [] and out["trigger_signal_id"] == w.hiring
    assert len(out["script"]["first_reactions"]) == 3


def test_the_api_keeps_the_first_reactions():
    from nexus.api.schemas import CallScriptOut

    assert CallScriptOut(**_script()).first_reactions[2]["reaction"] == "I'm busy."


# ---- 16. the optional P.S., confirmed by a person ---------------------------------------------

def test_a_ps_is_off_by_default_and_never_asked_for_on_a_follow_up_or_an_automatic_reply():
    from nexus.agents.copy import PS_RULE, email_rules
    from nexus.agents.email_style import ps_line_on

    assert not ps_line_on({}) and not ps_line_on({"style": {"ps_line": "true"}})
    assert ps_line_on({"style": {"ps_line": True}})
    assert PS_RULE not in email_rules("first")
    assert PS_RULE in email_rules("first", ps=True)
    assert PS_RULE not in email_rules("followup", ps=True)
    assert PS_RULE not in email_rules("first", ps=True, automatic=True)


def test_a_ps_is_split_off_the_body_and_its_sign_off_kept():
    from nexus.agents.copy import split_ps, with_ps

    assert split_ps(GOOD + "\n\nP.S. Congrats on the round.") == (GOOD, "Congrats on the round.")
    above = GOOD.replace("Best,\nSam", "PS: Congrats on the round.\n\nBest,\nSam")
    assert split_ps(above) == (GOOD, "Congrats on the round.")
    assert split_ps(GOOD) == (GOOD, "")
    assert with_ps(GOOD, "Congrats.").endswith("Best,\nSam\n\nP.S. Congrats.")


def test_a_confirmed_ps_is_sent_below_the_signature():
    from nexus.agents.copy import with_ps
    from nexus.engagement.sending.mime import compose_message_text

    text = compose_message_text(with_ps(GOOD, "Congrats on the round."),
                                "Kind regards,\nSam Rep\nPlaybook")
    assert text.endswith("Kind regards,\nSam Rep\nPlaybook\n\nP.S. Congrats on the round.")
    assert compose_message_text(text, "Kind regards,\nSam Rep\nPlaybook") == text


async def test_a_ps_the_model_adds_unasked_never_reaches_the_body(model):
    model(_email(GOOD + "\n\nP.S. Congrats on the Series B."))
    w = await _world("unaskedps")
    out = await _compose(w)
    assert "P.S." not in out["body"] and out["ps"] == ""


async def test_a_ps_is_kept_only_when_it_rests_on_a_given_fact(model):
    from nexus.models.identity import Tenant

    for ps, kept in (("Congrats on the $40M Series B.", True),
                     ("Globex saw 40% faster onboarding.", False)):
        model(_email(GOOD + f"\n\nP.S. {ps}"))
        w = await _world(f"psfact{int(kept)}")
        async with tenant_session(w.tid) as ts:
            (await ts.session.get(Tenant, w.tid)).email_settings = {"style": {"ps_line": True}}
        out = await _compose(w)
        assert out["ps"] == (ps if kept else ""), ps
        assert "P.S." not in out["body"]


async def test_review_sends_the_ps_only_with_the_reps_tick(client, model, monkeypatch):
    from nexus.core.config import get_settings
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementMessage, MailboxConnection
    from nexus.models.identity import Tenant
    from nexus.models.signal import SignalEvent
    from tests.conftest import auth, principal_from_token, signup

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)
    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    model(_email(GOOD + "\n\nP.S. Congrats on the $40M Series B."))
    token = await signup(client, slug="pstick", email="sam@pstick.com", company="PS Tick")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        await seed_relevance_profile(ts)
        (await ts.session.get(Tenant, me.tenant_id)).email_settings = {
            "style": {"ps_line": True}}
        mailbox = MailboxConnection(owner_user_id=me.user_id, provider="google",
                                    email="sam@pstick.com", status="connected",
                                    timezone="Europe/London")
        account = Account(name="Acme Robotics", domain="acme.io")
        ts.add(mailbox)
        ts.add(account)
        await ts.flush()
        ts.add(SignalEvent(account_id=account.id, kind="funding", source="web",
                           title="Acme Robotics raises $40M Series B", strength=0.9,
                           occurred_at=NOW, dedupe_key="pstick-funding"))
        ids = []
        for name in ("Jane Buyer", "Jane Other"):
            contact = Contact(account_id=account.id, full_name=name,
                              email=f"{name.split()[1].lower()}@acme.io")
            ts.add(contact)
            await ts.flush()
            ids.append(contact.id)
        mailbox_id = mailbox.id
    h = auth(token)
    template = await client.post("/api/engagement/templates", headers=h, json={
        "name": "One", "steps": [{"channel": "email"}]})
    campaign = (await client.post("/api/engagement/campaigns", headers=h, json={
        "name": "Q4", "mailbox_id": mailbox_id, "template_id": template.json()["id"]})).json()
    await client.post(f"/api/engagement/campaigns/{campaign['id']}/contacts", headers=h,
                      json={"contact_ids": ids})
    await client.post(f"/api/engagement/campaigns/{campaign['id']}/draft", headers=h)
    review = (await client.get(f"/api/engagement/campaigns/{campaign['id']}/review",
                               headers=h)).json()
    assert len(review) == 2
    for item in review:
        assert "P.S." not in item["body"]
        assert {"label": "Congrats on the $40M Series B.", "url": "", "kind": "ps"} \
            in item["draft_facts"]
    unticked, ticked = review
    for item, body in ((unticked, {}), (ticked, {"confirm_ps": True})):
        r = await client.post(f"/api/engagement/messages/{item['message_id']}/approve",
                              headers=h, json=body)
        assert r.status_code == 204, r.text
    async with tenant_session(me.tenant_id) as ts:
        without = await ts.get(EngagementMessage, unticked["message_id"])
        with_it = await ts.get(EngagementMessage, ticked["message_id"])
        assert "P.S." not in without.body_text
        assert with_it.body_text.endswith("\n\nP.S. Congrats on the $40M Series B.")
        # Confirming the AI's P.S. is not an edit of the AI's draft.
        assert with_it.ai_body == with_it.body_text


async def test_the_ps_switch_is_kept_when_an_older_client_omits_it(client):
    from tests.conftest import auth, signup

    h = auth(await signup(client, slug="psstyle", email="o@psstyle.com", company="PS"))
    base = {"default_signature": "", "tone": "", "length_words": None, "samples": []}
    assert (await client.get("/api/workspace/email/style", headers=h)).json()["ps_line"] is False
    r = await client.put("/api/workspace/email/style", headers=h, json={**base, "ps_line": True})
    assert r.status_code == 200 and r.json()["ps_line"] is True
    r = await client.put("/api/workspace/email/style", headers=h, json={**base, "tone": "plain"})
    assert r.json()["ps_line"] is True
    assert (await client.put("/api/workspace/email/style", headers=h,
                             json={**base, "ps_line": "true"})).status_code == 422


def test_the_review_screen_sends_the_ps_only_when_ticked():
    """No frontend test runner: the source is read, as the other UI tests here do."""
    from pathlib import Path

    source = Path("frontend/src/pages/engagement/ReviewQueue.tsx").read_text(encoding="utf-8")
    assert "I confirm this is true" in source
    assert "...(ps && psConfirmed ? { confirm_ps: true } : {})" in source
    assert 'useState(false);' in source.split("psConfirmed, setPsConfirmed] = ")[1][:20]
    # The P.S. and the shape are not facts the draft rests on.
    assert 'f.kind !== "ps" && f.kind !== "shape"' in source
