# tests/test_llm_json_token_budget.py
"""A JSON-returning prompt must be given room to close its JSON.

`_parse_obj` / `_parse_people` both need a COMPLETE document: the object regex needs a closing
brace and `json.loads` needs valid syntax. A `max_tokens` too small for the schema being asked
for truncates the model mid-value, both parsers return empty, and the caller cannot tell that
apart from "the web had nothing to say about this company" — so it degrades silently.

Measured against live Groq (openai/gpt-oss-120b, 2026-08-27) for the account enricher's own
11-key schema: at max_tokens=400 the response stopped inside `"description"` after 267 chars and
parsed to `{}`; at 1200 it closed and parsed all 11 keys, `employee_count` among them. That empty
dict is what capped every discovered candidate's ICP-fit at 65 against a hard gate of 70, so
daily ICP discovery persisted zero accounts.
"""
from __future__ import annotations

import ast
from pathlib import Path

# Call sites whose prompt asks the model for JSON, and the floor each needs to close it.
# The floors are deliberately generous: the cost of a few hundred unused tokens is nothing
# beside a parse that silently yields nothing.
JSON_CALL_SITES = {
    ("nexus/enrichment/account.py", "account_enrich"): 1200,
    ("nexus/integrations/contact_search.py", "contact_extract"): 1600,
}


def _complete_calls(path: Path):
    """Every `llm.complete(...)` in the file, as (purpose, max_tokens)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "complete"):
            continue
        kwargs = {k.arg: k.value for k in node.keywords if k.arg}
        purpose = kwargs.get("purpose")
        max_tokens = kwargs.get("max_tokens")
        if not isinstance(purpose, ast.Constant) or not isinstance(max_tokens, ast.Constant):
            continue
        yield purpose.value, max_tokens.value


def test_json_extraction_prompts_have_room_to_close_their_json():
    root = Path(__file__).resolve().parents[1]
    seen: set[tuple[str, str]] = set()
    for (rel, purpose), floor in JSON_CALL_SITES.items():
        path = root / rel
        assert path.exists(), f"{rel} moved; update JSON_CALL_SITES"
        found = dict(_complete_calls(path))
        assert purpose in found, (
            f"no llm.complete(purpose={purpose!r}) in {rel}; update JSON_CALL_SITES"
        )
        seen.add((rel, purpose))
        assert found[purpose] >= floor, (
            f"{rel} purpose={purpose!r} allows only {found[purpose]} output tokens; "
            f"its JSON schema needs at least {floor} to close. A truncated document parses "
            f"to empty and the caller reads that as 'nothing found'."
        )
    assert seen == set(JSON_CALL_SITES), "a JSON call site went unchecked"
