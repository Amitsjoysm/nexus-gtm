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


# -- Deterministic extraction ----------------------------------------------------------
_COUNTRY_ALIASES = {
    "us": "United States", "u.s.": "United States", "usa": "United States",
    "u.s.a.": "United States", "united states": "United States", "america": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom", "united kingdom": "United Kingdom",
    "britain": "United Kingdom", "england": "United Kingdom",
    "canada": "Canada", "germany": "Germany", "france": "France", "spain": "Spain",
    "italy": "Italy", "netherlands": "Netherlands", "australia": "Australia",
    "india": "India", "singapore": "Singapore", "japan": "Japan", "brazil": "Brazil",
    "eu": "European Union", "europe": "Europe", "apac": "APAC", "emea": "EMEA",
}
_INDUSTRY_KEYWORDS = {
    "fintech": "Fintech", "saas": "SaaS", "healthcare": "Healthcare", "health": "Healthcare",
    "ecommerce": "E-commerce", "e-commerce": "E-commerce", "retail": "Retail",
    "manufacturing": "Manufacturing", "logistics": "Logistics", "edtech": "EdTech",
    "insurance": "Insurance", "banking": "Banking", "biotech": "Biotech",
    "cybersecurity": "Cybersecurity", "security": "Cybersecurity", "marketing": "Marketing",
    "real estate": "Real Estate", "gaming": "Gaming", "telecom": "Telecom", "energy": "Energy",
}
_NAMED_BANDS = {
    "startup": {"min": 1, "max": 50},
    "smb": {"min": 1, "max": 200},
    "small business": {"min": 1, "max": 200},
    "mid-market": {"min": 200, "max": 1000},
    "midmarket": {"min": 200, "max": 1000},
    "mid market": {"min": 200, "max": 1000},
    "enterprise": {"min": 1000, "max": None},
}
_TITLE_TOKENS = ("vp", "vice president", "cro", "cmo", "ceo", "cto", "cfo", "coo",
                 "head of", "director", "manager", "chief")
_RANGE_RE = re.compile(r"(\d[\d,]*)\s*(?:-|to|–)\s*(\d[\d,]*)")
_UNDER_RE = re.compile(r"(?:under|below|less than|fewer than|<)\s*(\d[\d,]*)")
_OVER_RE = re.compile(r"(?:over|above|more than|>|at least)\s*(\d[\d,]*)|(\d[\d,]*)\s*\+")


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def _parse_size(text: str) -> dict | None:
    t = text.lower()
    for name, band in _NAMED_BANDS.items():
        if name in t:
            return dict(band)
    m = _RANGE_RE.search(t)
    if m:
        return {"min": _int(m.group(1)), "max": _int(m.group(2))}
    m = _UNDER_RE.search(t)
    if m:
        return {"min": None, "max": _int(m.group(1))}
    m = _OVER_RE.search(t)
    if m:
        return {"min": _int(m.group(1) or m.group(2)), "max": None}
    return None


def _split_phrases(text: str) -> list[str]:
    """Split a free answer into clean phrases on commas / 'and' / slashes."""
    parts = re.split(r",|/|\band\b|\bor\b|;", text, flags=re.IGNORECASE)
    return [p.strip(" .\t").strip() for p in parts if p.strip(" .\t").strip()]


def _title_case_phrase(p: str) -> str:
    # Preserve well-known acronyms; otherwise title-case words.
    acronyms = {"vp": "VP", "cro": "CRO", "cmo": "CMO", "ceo": "CEO", "cto": "CTO",
                "cfo": "CFO", "coo": "COO", "us": "US", "uk": "UK", "saas": "SaaS"}
    words = []
    for w in p.split():
        words.append(acronyms.get(w.lower(), w if w[:1].isupper() else w.capitalize()))
    return " ".join(words)


def extract_slots(text: str, icp_state: dict, pending_slot: str | None) -> dict:
    """Pure-Python slot extraction. Returns a *delta* (only slots it learned).

    Two passes: (1) keyword/regex detection that fires anywhere in the message, so one rich
    sentence fills many slots; (2) coercion — the user is answering ``pending_slot``, so for
    open-vocabulary slots (``industries``/``titles``) the full answer phrases win over an
    incidental keyword hit, and ``geo`` is coerced only when alias detection missed it. Never
    raises: an unparseable message yields an empty delta.
    """
    delta: dict = {}
    low = text.lower()

    # (1) Global detection.
    industries = [v for k, v in _INDUSTRY_KEYWORDS.items() if re.search(rf"\b{re.escape(k)}\b", low)]
    if industries:
        delta["industries"] = list(dict.fromkeys(industries))

    geo = [v for k, v in _COUNTRY_ALIASES.items() if re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", low)]
    if geo:
        delta["geo"] = list(dict.fromkeys(geo))

    size = _parse_size(text)
    if size is not None:
        delta["company_size"] = size

    titles = [_title_case_phrase(p) for p in _split_phrases(text)
              if any(tok in p.lower() for tok in _TITLE_TOKENS)]
    if titles:
        delta["titles"] = list(dict.fromkeys(titles))

    # (2) Coercion on the pending slot. For open-vocabulary slots the full answer wins
    # unconditionally (the user is explicitly answering that question); for geo we keep the
    # normalized alias hit when we have one and only coerce phrases when detection missed.
    if pending_slot in ("industries", "titles"):
        phrases = [_title_case_phrase(p) for p in _split_phrases(text)]
        if phrases:
            delta[pending_slot] = phrases
    elif pending_slot == "geo" and "geo" not in delta:
        phrases = [_title_case_phrase(p) for p in _split_phrases(text)]
        if phrases:
            delta["geo"] = phrases
    # company_size: only the regex/named-band parser fills it; leave missing to re-ask.
    return delta


def _union_ci(existing: list, incoming: list) -> list:
    """Ordered, case-insensitive union (existing first, then new)."""
    out = list(existing or [])
    seen = {str(x).lower() for x in out}
    for x in incoming or []:
        if str(x).lower() not in seen:
            out.append(x)
            seen.add(str(x).lower())
    return out


def merge_icp(icp_state: dict, delta: dict) -> dict:
    """Merge a slot-delta into the working ICP. Lists union (CI), scalars/dicts override."""
    out = dict(icp_state)
    for k, v in delta.items():
        if k in LIST_SLOTS:
            out[k] = _union_ci(out.get(k), v)
        else:
            out[k] = v
    return out
