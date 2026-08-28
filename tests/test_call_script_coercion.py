# tests/test_call_script_coercion.py
"""A call script is READ ALOUD, so a degraded one must still be speakable.

Two failures measured live against Groq (openai/gpt-oss-120b, 2026-08-27) for a real account:

* At the inherited default of 800 output tokens the model's JSON stopped mid-sentence after
  2,483 characters. `json.loads` refused it and the agent fell back — so the rep got no value
  prop, no discovery questions and no objection handling. At 1,800 the same prompt closed and
  produced 5 discovery questions and 4 objection/response pairs.
* The fallback then assigned `hook = raw[:280]`, which put the truncated JSON itself in the
  talking-point slot: the rep's screen read `{ "opener": "Hi Brian, this is [Your Name]...`.
"""
from __future__ import annotations

import json

from nexus.agents.call_script import _coerce


def test_a_complete_object_is_parsed():
    raw = json.dumps({
        "opener": "Hi Brian, this is Sam from InfoJoy.",
        "hook": "I saw the Hg Capital deal close.",
        "value_prop": "Verified contact data.",
        "discovery_questions": ["How do you handle stale records today?"],
        "objections": [{"objection": "Not now.", "response": "Fair — worth 15 minutes?"}],
        "cta": "Open to Thursday?",
        "voicemail": "Calling about OneStream.",
    })
    out = _coerce(raw, account="OneStream", contact="Brian")
    assert out["value_prop"] == "Verified contact data."
    assert len(out["discovery_questions"]) == 1
    assert out["objections"][0]["objection"] == "Not now."


def test_a_fenced_or_prefaced_object_is_still_parsed():
    """Models add ``` fences and a lead-in sentence however firmly the prompt says not to.

    Discarding an otherwise-complete script over its wrapper throws away the whole call.
    """
    inner = json.dumps({
        "opener": "Hi Brian.", "hook": "Saw the deal.", "value_prop": "Verified data.",
        "discovery_questions": ["How do you handle it today?"],
        "objections": [{"objection": "No budget.", "response": "Understood."}],
        "cta": "Thursday?", "voicemail": "Calling about OneStream.",
    })
    out = _coerce(f"Sure — here you go:\n```json\n{inner}\n```", account="OneStream", contact="Brian")
    assert out["value_prop"] == "Verified data.", "a fenced but complete script was discarded"
    assert out["objections"], "objection handling was lost to a code fence"


def test_a_truncated_response_never_becomes_a_talking_point():
    """The rep must never be shown raw JSON to say out loud."""
    truncated = '{\n  "opener": "Hi Brian, this is [Your Name] from InfoJoy. I saw the news",\n  "va'
    out = _coerce(truncated, account="OneStream", contact="Brian")
    for field in ("opener", "hook", "value_prop", "cta", "voicemail"):
        value = out[field]
        assert "{" not in value and '"' not in value, (
            f"{field} carries raw model output a rep would read aloud: {value!r}"
        )
    # Still usable: an opener, a close and a voicemail are the minimum a rep can dial with.
    assert out["opener"] and out["cta"] and out["voicemail"]
