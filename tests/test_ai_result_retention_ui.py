"""The screens actually USE the retained results, and the run page names what it shows.

There is no frontend test runner, so — as elsewhere in this suite — these read the source. They pin
the wiring whose absence was the bug: every AI result was saved server-side and no page loaded one
back. The behaviour of the endpoints themselves is in test_ai_result_retention.py.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "frontend" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_the_run_page_never_stringifies_a_nested_output_value():
    """`String(v)` on `counts: {own: 1, new: 0}` rendered "counts: [object Object]" on the
    orchestrator's discovery step. The summary goes through a formatter with an object branch."""
    src = _read("pages/RunDetailPage.tsx")
    step_output = src[src.index("function StepOutput"):]
    step_output = step_output[: step_output.index("\n}\n")]
    assert "String(v)" not in step_output, "a raw String(v) is back in the step summary"
    assert "summarizeValue(v)" in step_output
    formatter = src[src.index("function summarizeValue"):]
    assert 'typeof v === "object"' in formatter[: formatter.index("\n}\n")]


def test_the_account_page_loads_saved_results_and_resets_between_accounts():
    src = _read("pages/AccountDetailPage.tsx")
    assert "latestAgentRuns(id" in src, "the account page no longer loads saved AI results"
    effect = src[src.index("latestAgentRuns(id") - 400: src.index("latestAgentRuns(id")]
    assert "setAgents({})" in effect, (
        "results must be reset on account change, or one account's brief shows on the next"
    )


def test_the_composer_reuses_todays_draft_before_writing_one():
    src = _read("components/EmailComposer.tsx")
    open_draft = src[src.index("async function openDraft"):]
    open_draft = open_draft[: open_draft.index("\n  }\n")]
    assert "latestAgentRuns(" in open_draft
    assert "fresh" in open_draft, "a draft from before today must never be reused"
    assert re.search(r"useEffect\(\(\) => \{\s*void openDraft\(\)", src), (
        "opening the composer must try the saved draft first"
    )


def test_opening_a_call_does_not_force_a_new_script():
    """Regenerate forces one; opening a call reuses today's."""
    src = _read("components/CallConsole.tsx")
    assert "if (autoGenerate) void generate();" in src
    assert "generate(Boolean(script))" in src


def test_the_relevance_page_offers_the_last_analysis_back():
    src = _read("pages/RelevancePage.tsx")
    assert "api.lastWebsiteAnalysis(" in src
    assert "Re-apply this draft" in src
