# nexus/orchestration/intake.py
"""The orchestrator brain: deterministic ICP slot-filling + token-frugal context envelope.

The control surface is intentionally split:
* **Deterministic core** (this is the bulk): the slot schema, ``missing_required`` truth table,
  the pure-Python ``extract_slots`` (country map + size regex + industry keywords + coercion on
  the pending slot), and the merge rules. No LLM, no DB, no network — fully unit-testable.
* **LLM phrasing only**: :class:`IntakeController` calls the provider for two things — phrasing the
  single next question (purpose ``clarify_question``) and folding the rolling summary (purpose
  ``chat_summary``). Both have deterministic stub branches so CI is reproducible.

The :class:`ContextEnvelope` is the token-frugal payload: structured state + a capped rolling
summary + the last K raw messages, hard-bounded by ``orch_chat_token_budget``. The full transcript
is persisted for display but never replayed to the model, so per-turn cost stays ~flat.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from nexus.agents.llm import LLMMessage, LLMProvider, get_llm_provider
from nexus.core.config import get_settings

# -- Slot schema -----------------------------------------------------------------------
# List-valued slots are merged by case-insensitive ordered union; scalar/dict slots override.
LIST_SLOTS = ("industries", "geo", "required_tech", "titles", "intent_signals", "exclusions")
TARGET_COMPANIES = "companies"
TARGET_CONTACTS = "contacts"


def _norm_target(target: str | None) -> str:
    return target if target in (TARGET_COMPANIES, TARGET_CONTACTS) else TARGET_COMPANIES


def missing_required(icp_state: dict, target: str | None) -> list[str]:
    """Pure-Python truth table: which required slots are still empty.

    Always-required: an industry signal (``industries`` or a free-text ``icp_description``) and
    ``geo``. Companies additionally require ``company_size``; contacts require ``titles``/seniority.
    Order is priority order — the controller asks for ``missing[0]`` first.
    """
    target = _norm_target(target)
    missing: list[str] = []
    if not (icp_state.get("industries") or icp_state.get("icp_description")):
        missing.append("industries")
    if not icp_state.get("geo"):
        missing.append("geo")
    if target == TARGET_COMPANIES:
        size = icp_state.get("company_size") or {}
        if not (size.get("min") or size.get("max")):
            missing.append("company_size")
    else:  # contacts
        if not (icp_state.get("titles") or icp_state.get("seniority")):
            missing.append("titles")
    return missing
