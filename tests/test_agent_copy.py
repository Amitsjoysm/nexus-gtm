# tests/test_agent_copy.py
"""Drafted copy has to be a sentence.

Reported from the live app. "Draft outreach" produced:

    Teams like Marketjoy use Accurate Lead Generation to Stale lists, Duplicate records,
    No signal on in-market accounts, Wasted time chasing wrong leads.

That is a real email that would have gone to a real prospect. The cause was one variable carrying
two incompatible grammatical forms: `pains_solved` holds problem NOUNS, but the template read
`use {vp} to {pain}` — which needs a verb — and the no-value-props fallback was "hit pipeline
goals", a verb phrase. So the sentence read correctly for workspaces that had configured nothing
and broke for every workspace that had filled the field in, which is exactly backwards.
"""
from __future__ import annotations

from nexus.agents.copy import DEFAULT_PAINS, first_pain, format_pains

# The value prop that produced the reported email.
REPORTED = [
    "Stale lists",
    "Duplicate records",
    "No signal on in-market accounts",
    "Wasted time chasing wrong leads",
]


def test_the_reported_sentence_now_reads():
    sentence = (
        f"Teams like Marketjoy use Accurate Lead Generation to get ahead of "
        f"{format_pains(REPORTED)}."
    )
    assert sentence == (
        "Teams like Marketjoy use Accurate Lead Generation to get ahead of "
        "stale lists and duplicate records."
    )


def test_a_long_list_is_capped_rather_than_recited():
    """Four problems in one sentence reads as a list being read at the prospect. The rest of the
    value prop still reaches the model through the prompt body."""
    assert format_pains(REPORTED).count(",") == 0
    assert "no signal on in-market accounts" not in format_pains(REPORTED)


def test_acronyms_keep_their_capitals():
    """The lowercasing rule tests the SECOND character, so a capital following a capital is left
    alone. A blanket `.lower()` would mangle every acronym a customer typed."""
    assert format_pains(["SOC2 gaps", "CRM hygiene"]) == "SOC2 gaps and CRM hygiene"
    assert format_pains(["Stale lists"]) == "stale lists"


def test_one_and_two_items_both_read():
    assert format_pains(["Stale lists"]) == "stale lists"
    assert format_pains(["Stale lists", "Duplicate records"]) == "stale lists and duplicate records"


def test_the_fallback_is_a_noun_phrase():
    """The old fallback was a VERB phrase, which is how the mismatch stayed invisible: with no
    value props the sentence read fine, so nobody saw it until a customer configured one."""
    assert format_pains([]) == DEFAULT_PAINS
    assert format_pains(None) == DEFAULT_PAINS
    sentence = f"use our platform to get ahead of {format_pains([])}."
    assert sentence == "use our platform to get ahead of the usual pipeline bottlenecks."


def test_blank_entries_are_ignored():
    assert format_pains(["", "  ", "Stale lists"]) == "stale lists"


def test_a_discovery_question_names_one_problem():
    """"How are you handling stale lists, duplicate records, no signal on in-market accounts and
    wasted time chasing wrong leads today?" is not a question a person can answer."""
    assert first_pain(REPORTED) == "stale lists"
    assert f"How are you handling {first_pain(REPORTED)} today?" == (
        "How are you handling stale lists today?"
    )
    assert first_pain([]) == DEFAULT_PAINS


# ---- through the template that actually renders it ----------------------------------------------

async def test_the_offline_draft_template_produces_a_sentence():
    """`llm.py`'s stub is the last fallback in the `auto` chain, so it is what a deployment with a
    dead LLM key sends. Its output is not a placeholder — it reaches prospects."""
    from nexus.agents.llm import LLMMessage, StubLLMProvider

    resp = await StubLLMProvider().complete(
        [LLMMessage(role="user", content="draft")],
        purpose="outreach_message",
        variables={
            "account": "Marketjoy",
            "contact": "Sam",
            "value_prop": "Accurate Lead Generation",
            "trigger": "your Series B",
            "pain": format_pains(REPORTED),
        },
    )
    assert "to get ahead of stale lists and duplicate records." in resp.text
    # The shape of the original bug: a capitalised noun straight after "to".
    assert "to Stale lists" not in resp.text


# ---- the prompt rules that make output trustworthy ----------------------------------------------
#
# Distilled from four public GTM prompt libraries. The value is not the wording — it is that each
# rule maps to a failure this product can have, and that the rules are stated in ONE place so the
# email and call agents cannot drift apart.


def test_the_output_contract_is_actually_requested():
    """The highest-value rule, and the one none of the reference libraries has.

    `_split_subject` has always parsed a leading "Subject:" line, and the prompt never asked for
    one — so a model that opened with the body produced an email with a blank subject and nothing
    reported a problem.
    """
    from nexus.agents.copy import OUTPUT_CONTRACT
    from nexus.agents.messaging import _split_subject

    assert "Subject:" in OUTPUT_CONTRACT
    # The parser and the contract have to agree on the exact shape.
    subject, body = _split_subject("Subject: Series B and hiring\n\nHi Sam, ...")
    assert subject == "Series B and hiring"
    assert body.startswith("Hi Sam")


def test_the_rules_ban_the_phrases_that_mark_an_email_as_automated():
    from nexus.agents.copy import EMAIL_RULES

    lowered = EMAIL_RULES.lower()
    for phrase in ("hope this finds you well", "circling back", "synergy", "leverage"):
        assert phrase in lowered, f"{phrase!r} is no longer banned"


def test_both_agents_forbid_inventing_facts():
    """The rule that matters most here: this product HAS the real facts, so omission is always
    available as the alternative to a fabricated metric or customer name."""
    from nexus.agents.copy import CALL_RULES, EMAIL_RULES

    for rules in (EMAIL_RULES, CALL_RULES):
        assert "only facts given above" in rules
        assert "metric" in rules and "case study" in rules


def test_the_email_rules_impose_a_hard_length_cap():
    from nexus.agents.copy import EMAIL_RULES, EMAIL_WORD_CAP

    assert 50 <= EMAIL_WORD_CAP <= 120, "a cold email cap outside this range is not a cold email"
    assert f"Under {EMAIL_WORD_CAP} words" in EMAIL_RULES


def test_the_system_grounding_names_the_specific_fabrications():
    """"Never invent value props" was already there. Naming the fabrications that actually cost a
    rep credibility — a made-up customer, an invented percentage — is what makes it enforceable."""
    from nexus.relevance.engine import RelevanceContext

    prompt = RelevanceContext(
        product_context="", icp_summary="", value_props=[], account_fit=None
    ).to_prompt()
    for term in ("customer name", "metric", "percentage", "case study", "integration"):
        assert term in prompt, f"grounding no longer names {term!r}"
    assert "leave it out" in prompt, "omission is no longer offered as the alternative"
