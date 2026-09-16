# tests/test_draft_structure.py
"""A drafted email has to read like one a competent SDR sent.

Reported 2026-09-16: drafts arrive with no salutation and do not read like real outreach. The
prompt never asked for a greeting — `OUTPUT_CONTRACT` requested `Subject:` and a body, and nothing
else — so a model that opened with the observation produced an email starting mid-thought.

Three things are enforced here, and the third is the one with teeth: the draft is CHECKED before
the rep sees it, and a malformed one is regenerated once. A rule in a prompt is a request; a check
is a guarantee.
"""
from __future__ import annotations

import pytest

GOOD = (
    "Hi Curtis,\n\n"
    "Saw Marketjoy opened a second SDR pod last month. Teams doing that usually hit the same "
    "wall on list hygiene.\n\n"
    "We cut that work for teams your size. Open to 15 minutes on Thursday?\n\n"
    "Best,\nJane"
)


# ---- what the rules ask for ---------------------------------------------------------------------

def test_the_output_contract_asks_for_a_greeting_and_a_sign_off():
    """`_split_subject` has always parsed a leading Subject line and the prompt now also has to
    name the two parts a human notices first."""
    from nexus.agents.copy import OUTPUT_CONTRACT, STRUCTURE_RULE

    contract = f"{OUTPUT_CONTRACT} {STRUCTURE_RULE}".lower()
    assert "greeting" in contract
    assert "first name" in contract
    assert "sign-off" in contract


def test_the_rules_still_forbid_inventing_facts():
    """The samples added below are style references. This rule is what stops them becoming a
    source of claims about a customer we do not have."""
    from nexus.agents.copy import EMAIL_RULES

    assert "invent" in EMAIL_RULES.lower()


# ---- the check ----------------------------------------------------------------------------------

def test_a_good_draft_passes():
    from nexus.agents.email_quality import check_draft

    assert check_draft(subject="Second SDR pod", body=GOOD, first_name="Curtis") == []


@pytest.mark.parametrize(("body", "complaint"), [
    ("Saw Marketjoy opened a second pod. Open to 15 minutes Thursday?\n\nBest,\nJane", "greeting"),
    ("Hi Curtis,\n\nSaw the pod news. We help teams like yours.", "ask"),
    ("Hi Curtis,\n\nSaw the pod news. Open to 15 minutes Thursday?", "sign-off"),
    ("Hi Curtis,\n\nI hope this finds you well. Open to 15 minutes?\n\nBest,\nJane", "phrase"),
])
def test_each_failure_is_named_so_the_retry_can_fix_it(body, complaint):
    from nexus.agents.email_quality import check_draft

    problems = " ".join(check_draft(subject="S", body=body, first_name="Curtis")).lower()
    assert complaint in problems, problems


def test_a_missing_subject_is_a_problem():
    from nexus.agents.email_quality import check_draft

    assert any("subject" in p.lower() for p in check_draft(subject="", body=GOOD,
                                                           first_name="Curtis"))


def test_a_greeting_for_the_wrong_person_is_caught():
    """Mail-merge's most expensive failure: the right email to the wrong name."""
    from nexus.agents.email_quality import check_draft

    body = GOOD.replace("Hi Curtis,", "Hi Derek,")
    assert any("first name" in p.lower() or "greeting" in p.lower()
               for p in check_draft(subject="S", body=body, first_name="Curtis"))


def test_an_over_long_draft_is_caught():
    from nexus.agents.copy import EMAIL_WORD_CAP
    from nexus.agents.email_quality import check_draft

    body = "Hi Curtis,\n\n" + ("word " * (EMAIL_WORD_CAP + 40)) + "\n\nOpen to 15 minutes?\n\nBest,\nJane"
    assert any("words" in p.lower() for p in check_draft(subject="S", body=body,
                                                         first_name="Curtis"))


def test_the_check_never_raises_on_odd_input():
    from nexus.agents.email_quality import check_draft

    assert check_draft(subject=None, body=None, first_name=None)  # problems, not an exception


# ---- the workspace's own voice ------------------------------------------------------------------

def test_sample_emails_are_offered_as_structure_not_as_facts():
    """A rep's sample will name a real customer and a real number. Copying those into a draft for
    a different buyer is the fabrication `EMAIL_RULES` exists to prevent, so the block that carries
    samples has to say so itself."""
    from nexus.agents.email_style import style_prompt

    block = style_prompt({
        "style": {"tone": "direct", "length_words": 80,
                  "samples": ["Hi Ann,\n\nWe helped Globex cut onboarding 40%. 15 minutes?"]}
    })
    assert "Globex" in block                      # the sample reaches the model
    lowered = block.lower()
    assert "structure" in lowered or "voice" in lowered
    assert "fact" in lowered or "claim" in lowered  # ...with the guard attached


def test_the_style_block_is_empty_when_a_workspace_has_configured_nothing():
    """No samples must mean no instructions, not an empty scaffold the model tries to satisfy."""
    from nexus.agents.email_style import style_prompt

    assert style_prompt({}).strip() == ""
    assert style_prompt(None).strip() == ""


def test_tone_and_length_reach_the_prompt():
    from nexus.agents.email_style import style_prompt

    block = style_prompt({"style": {"tone": "warm and brief", "length_words": 60}})
    assert "warm and brief" in block
    assert "60" in block


def test_a_workspace_cannot_drown_the_prompt_in_samples():
    """Every sample is prompt tokens on every draft. Three is a voice; thirty is a bill."""
    from nexus.agents.email_style import MAX_SAMPLES, style_prompt

    block = style_prompt({"style": {"samples": [f"sample {i}" for i in range(MAX_SAMPLES + 5)]}})
    assert block.count("sample ") <= MAX_SAMPLES


# ---- the agent retries once ---------------------------------------------------------------------

async def test_a_malformed_draft_is_regenerated_once_before_the_rep_sees_it(monkeypatch):
    """The whole point of checking: the rep gets the corrected draft, not the broken one."""
    from nexus.agents import messaging

    bad = "Subject: Second pod\n\nSaw the pod news. We help teams like yours."
    good = f"Subject: Second pod\n\n{GOOD}"
    calls: list[str] = []

    async def fake_complete(self, messages, **kwargs):
        calls.append(messages[-1].content)
        return bad if len(calls) == 1 else good

    monkeypatch.setattr(messaging, "_complete_with_retry", None, raising=False)
    from nexus.agents.email_quality import check_draft

    # The corrective instruction must name what was wrong, or the second attempt is a coin flip.
    problems = check_draft(subject="Second pod", body=bad.split("\n\n", 1)[1], first_name="Curtis")
    assert problems
    retry = messaging.retry_instruction(problems)
    assert "greeting" in retry.lower() or "sign-off" in retry.lower()
