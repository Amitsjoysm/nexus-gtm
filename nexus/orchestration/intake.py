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

# A bare domain or full URL the user pastes as a reference ("here's our site: acme.com").
# Requires a real ≥2-char TLD so "e.g." / "i.e." / "vs." don't register as URLs.
_URL_RE = re.compile(
    r"(?:https?://)?(?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+[a-z]{2,}(?:/[^\s]*)?",
    re.IGNORECASE,
)
# A free-text location after an explicit cue ("based in Austin", "targeting the Bay Area").
# Case-sensitive so it keys on proper nouns; filtered against industry/size words below.
_GEO_CUE_RE = re.compile(
    r"\b(?:based in|located in|headquartered in|hq in|in|near|across|within|targeting|target)\s+"
    r"((?:the\s+)?[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,2})"
)
# Signals that the user is dictating a free-text ICP rather than answering a single slot.
_ICP_CUE_RE = re.compile(
    r"\b(?:icp(?:\s+is)?|ideal customer(?:\s+profile)?|we\s+(?:sell|target|serve)|"
    r"our\s+(?:customers|buyers|icp)|companies\s+(?:that|who|like)|"
    r"businesses\s+(?:that|who)|orgs?\s+(?:that|who))\b",
    re.IGNORECASE,
)
# Qualifier words that make a no-keyword message read as a descriptive ICP, not a one-word answer.
_DESCRIPTIVE_RE = re.compile(
    r"\b(?:that|who|which|using|uses|use|with|sell|selling|serve|serving|"
    r"provide|providing|build|building|offer|offering|based|focused)\b",
    re.IGNORECASE,
)
# Stop-words we never accept as a captured geo phrase (avoids industry/role bleed-through).
_GEO_STOPWORDS = {"saas", "fintech", "ai", "the", "sales", "marketing", "revops"}


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

    # A reference URL / domain the user pastes ("here's our site: acme.com"). Stored as a scalar
    # so the discovery agent can seed research from it. Normalized to an https:// URL.
    url_m = _URL_RE.search(text)
    if url_m:
        raw = url_m.group(0).rstrip(".,;)")
        delta["seed_url"] = raw if raw.lower().startswith("http") else f"https://{raw}"

    # Free-text geo after an explicit cue ("based in Austin", "across the Bay Area"), for locations
    # the alias gazetteer doesn't list. Filtered so industry/role words don't bleed through.
    for m in _GEO_CUE_RE.finditer(text):
        cand = re.sub(r"^the\s+", "", m.group(1).strip(), flags=re.IGNORECASE).strip()
        cl = cand.lower()
        if not cand or cl in _GEO_STOPWORDS or cl in _INDUSTRY_KEYWORDS or any(c.isdigit() for c in cand):
            continue
        existing = list(delta.get("geo", []))
        if cl not in {str(x).lower() for x in existing}:
            delta["geo"] = existing + [cand]

    # Free-text ICP: the user describes their buyer in prose rather than answering a single slot.
    # Captured as ``icp_description`` (which satisfies the industry requirement) when they use an
    # explicit ICP cue, or when there's no industry keyword yet the sentence is clearly descriptive
    # (≥6 words + a qualifier). Skipped while coercing an open-vocabulary slot so a direct answer wins.
    if pending_slot not in ("industries", "titles", "geo") and "industries" not in delta:
        if _ICP_CUE_RE.search(text) or (len(low.split()) >= 6 and _DESCRIPTIVE_RE.search(low)):
            delta["icp_description"] = text.strip()

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


# -- Token-frugal context envelope -----------------------------------------------------
def _approx_tokens(text: str) -> int:
    """Cheap, monotonic token estimate (~4 chars/token). Good enough for a budget guard."""
    return max(1, (len(text) + 3) // 4)


@dataclass(slots=True)
class ContextEnvelope:
    icp_state: dict
    target: str
    account_id: str | None
    missing_slots: list[str]
    context_summary: str
    recent_messages: list[dict]
    token_estimate: int = 0

    @classmethod
    def build(
        cls,
        *,
        icp_state: dict,
        target: str | None,
        account_id: str | None,
        missing_slots: list[str],
        context_summary: str,
        recent_messages: list[dict],
        budget: int,
        recency_window: int,
        summary_token_cap: int,
    ) -> "ContextEnvelope":
        """Assemble the per-turn payload and enforce the budget.

        Structured state is authoritative and always kept (it is tiny). The summary is capped at
        ``summary_token_cap``. On overflow we **trim the recency window first** (oldest dropped),
        then truncate the summary — matching §4.3.
        """
        structured = json.dumps(
            {"icp_state": icp_state, "target": _norm_target(target),
             "account_id": account_id, "missing_slots": missing_slots},
            separators=(",", ":"),
        )
        # Pre-cap the summary to its own ceiling.
        summary = context_summary or ""
        if _approx_tokens(summary) > summary_token_cap:
            summary = summary[: summary_token_cap * 4]
        recent = list(recent_messages or [])[-recency_window:]

        base = _approx_tokens(structured)

        def total(rs: list[dict], summ: str) -> int:
            return base + _approx_tokens(summ) + sum(_approx_tokens(m.get("content", "")) for m in rs)

        # 1) Trim recency window (oldest first).
        while recent and total(recent, summary) > budget:
            recent = recent[1:]
        # 2) Truncate the summary if still over.
        if total(recent, summary) > budget:
            room = max(0, budget - base - sum(_approx_tokens(m.get("content", "")) for m in recent))
            summary = summary[: room * 4]
        return cls(
            icp_state=icp_state, target=_norm_target(target), account_id=account_id,
            missing_slots=missing_slots, context_summary=summary, recent_messages=recent,
            token_estimate=total(recent, summary),
        )


# -- Controller ------------------------------------------------------------------------
_SUGGESTIONS = {
    "industries": ["SaaS", "Fintech", "Healthcare", "E-commerce", "Manufacturing"],
    "geo": ["United States", "Canada", "United Kingdom", "Germany", "Australia"],
    "company_size": ["1–50", "51–200", "201–1000", "1001–5000", "5000+"],
    "titles": ["VP Sales", "CRO", "Head of RevOps", "CMO"],
}
_AFFIRMATIVE_PREFIXES = ("go", "yes", "yep", "yeah", "launch", "start", "run", "find",
                         "sure", "ok", "okay", "proceed", "do it")


def _is_affirmative(text: str) -> bool:
    t = text.strip().lower()
    return any(t == p or t.startswith(p + " ") for p in _AFFIRMATIVE_PREFIXES)


# Vocabulary that signals the user wants individual people vs. accounts. "prospect/lead/buyer"
# are the words real reps use, and their absence was why "I want prospects not companies" failed.
_CONTACT_WORDS = (
    "contact", "contacts", "people", "person", "persona", "personas", "title", "titles",
    "decision maker", "decision-maker", "decision makers", "decision-makers",
    "prospect", "prospects", "lead", "leads", "buyer", "buyers", "champion", "champions",
    "individual", "individuals", "stakeholder", "stakeholders",
)
_COMPANY_WORDS = (
    "company", "companies", "account", "accounts", "organization", "organizations",
    "org", "orgs", "vendor", "vendors", "business", "businesses", "firm", "firms",
)
# "not companies", "don't want accounts", "instead of orgs" — negation flips intent away from a
# target. The gap excludes commas so the negation can't leap across a clause boundary into the
# *positive* half of "I don't want companies, I want prospects".
_NEG_COMPANY_RE = re.compile(
    r"\b(?:not|no|don'?t|do\s+not|dont|instead\s+of|rather\s+than|without)\b[^.,]*?\b"
    r"(?:compan(?:y|ies)|accounts?|organi[sz]ations?|orgs?|vendors?|businesses?|firms?)\b",
    re.IGNORECASE,
)
_NEG_CONTACT_RE = re.compile(
    r"\b(?:not|no|don'?t|do\s+not|dont|instead\s+of|rather\s+than|without)\b[^.,]*?\b"
    r"(?:contacts?|people|persons?|personas?|prospects?|leads?|buyers?)\b",
    re.IGNORECASE,
)


def _has_word(low: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", low) for w in words)


def infer_target(text: str, current: str | None) -> str:
    """Infer whether the user wants ``companies`` or ``contacts`` — and crucially, allow a *re-target*.

    The old logic was sticky: once a target was set it returned unconditionally, so "actually I want
    prospects, not companies" never took effect. Now an explicit signal on this turn always wins:
    negation ("not companies") + an opposing word, or a plain contact/company word. With no signal we
    fall back to the current target, then default to companies on a cold start.
    """
    low = text.lower()
    neg_company = bool(_NEG_COMPANY_RE.search(low))
    neg_contact = bool(_NEG_CONTACT_RE.search(low))
    contact_hit = _has_word(low, _CONTACT_WORDS)
    company_hit = _has_word(low, _COMPANY_WORDS)

    # Negating one target is a strong vote for the other (handles "I don't want companies, I want
    # prospects" and the bare "not companies").
    if neg_company and not neg_contact:
        return TARGET_CONTACTS
    if neg_contact and not neg_company:
        return TARGET_COMPANIES
    # Explicit mention with no contradicting negation.
    if contact_hit and not company_hit:
        return TARGET_CONTACTS
    if company_hit and not contact_hit:
        return TARGET_COMPANIES
    # Ambiguous or silent turn: keep what we had, else default to companies.
    if current in (TARGET_COMPANIES, TARGET_CONTACTS):
        return current
    return TARGET_COMPANIES


def _has_target_signal(text: str) -> bool:
    """True when the turn explicitly names or negates a target — used to gate LLM re-targeting so
    it never overrides a deterministic, high-confidence signal the user just gave."""
    low = text.lower()
    return bool(
        _NEG_COMPANY_RE.search(low) or _NEG_CONTACT_RE.search(low)
        or _has_word(low, _CONTACT_WORDS) or _has_word(low, _COMPANY_WORDS)
    )


# Slots the LLM understanding pass is allowed to fill — but only when the deterministic layer left
# them empty (it never overrides a regex/keyword hit).
_AUGMENTABLE_SLOTS = set(LIST_SLOTS) | {"company_size", "icp_description", "seed_url", "seniority"}


def _icp_phrase(icp_state: dict, target: str) -> str:
    bits = []
    if icp_state.get("industries"):
        bits.append(", ".join(map(str, icp_state["industries"])))
    elif icp_state.get("icp_description"):
        bits.append(str(icp_state["icp_description"]))
    if icp_state.get("geo"):
        bits.append("in " + ", ".join(map(str, icp_state["geo"])))
    size = icp_state.get("company_size") or {}
    if size.get("min") or size.get("max"):
        bits.append(f"{size.get('min', 0)}–{size.get('max', '∞')} employees")
    return " ".join(bits) or "your ICP"


@dataclass(slots=True)
class IntakeDecision:
    icp_state: dict
    missing_slots: list[str]
    target: str
    action: str           # "clarify" | "ready" | "launch"
    assistant_kind: str   # KIND_* string
    assistant_text: str
    data: dict = field(default_factory=dict)
    summary: str = ""


@dataclass(slots=True)
class Understanding:
    """The optional LLM read of a turn: a (possibly) re-targeted intent + a slot delta. Both default
    empty so a stub provider (or any parse failure) is a no-op and the deterministic path stands."""
    target: str | None = None
    slots: dict = field(default_factory=dict)


class IntakeController:
    """The brain. Pure-Python decisioning over structured state; LLM only phrases + summarizes."""

    def __init__(self, llm: LLMProvider | None = None) -> None:
        self.llm = llm or get_llm_provider()

    async def advance(
        self,
        *,
        icp_state: dict,
        target: str | None,
        missing_slots: list[str],
        context_summary: str,
        user_text: str,
        is_first_turn: bool,
    ) -> IntakeDecision:
        target = infer_target(user_text, target)
        # ``pending`` is the slot we last *asked* about — only then does the user's reply get
        # coerced into it. On the first turn nothing has been asked, so the opening message is
        # parsed by global detection alone (no coercion clobbering a clean multi-slot sentence).
        pending = None if is_first_turn else (missing_slots[0] if missing_slots else None)
        new_state = merge_icp(icp_state, extract_slots(user_text, icp_state, pending))

        # Optional LLM understanding pass: fills only the gaps the deterministic layer left, and
        # may re-target only when the user gave no explicit company/contact signal. On the stub
        # provider this is empty, so the offline/CI path is byte-for-byte unchanged.
        understanding = await self._understand(user_text, context_summary, new_state, target)
        if understanding.target and not _has_target_signal(user_text):
            target = understanding.target
        if understanding.slots:
            gaps = {k: v for k, v in understanding.slots.items()
                    if k in _AUGMENTABLE_SLOTS and not new_state.get(k)}
            if gaps:
                new_state = merge_icp(new_state, gaps)

        missing = missing_required(new_state, target)

        if missing:
            slot = missing[0]
            question = await self._phrase(slot, new_state)
            summary = await self._summarize(context_summary, new_state, target)
            return IntakeDecision(
                icp_state=new_state, missing_slots=missing, target=target,
                action="clarify", assistant_kind="clarifying_question",
                assistant_text=question,
                data={"slot": slot, "suggestions": _SUGGESTIONS.get(slot, [])},
                summary=summary,
            )

        summary = await self._summarize(context_summary, new_state, target)
        if is_first_turn or _is_affirmative(user_text):
            return IntakeDecision(
                icp_state=new_state, missing_slots=[], target=target,
                action="launch", assistant_kind="run_launched",
                assistant_text=f"Finding {target} matching {_icp_phrase(new_state, target)}…",
                data={}, summary=summary,
            )
        return IntakeDecision(
            icp_state=new_state, missing_slots=[], target=target,
            action="ready", assistant_kind="text",
            assistant_text=(
                f"I can find {target} matching {_icp_phrase(new_state, target)}. "
                "Reply 'go' to start, or refine the criteria."
            ),
            data={}, summary=summary,
        )

    async def _understand(
        self, user_text: str, summary: str, icp_state: dict, target: str
    ) -> Understanding:
        """Ask the LLM to read the turn as JSON ``{"target": ..., "slots": {...}}``. Defensive by
        design: any provider error, non-JSON, or unexpected shape collapses to an empty Understanding
        so the deterministic layer is authoritative. The stub returns ``{}`` → guaranteed no-op."""
        try:
            resp = await self.llm.complete(
                [LLMMessage(role="user", content=user_text)],
                purpose="intake_understanding",
                variables={"summary": summary, "icp_state": icp_state, "target": target},
                max_tokens=200,
            )
            data = json.loads((resp.text or "").strip() or "{}")
        except (json.JSONDecodeError, ValueError, TypeError):
            return Understanding()
        except Exception:
            # Provider/network failure must never break intake — degrade to deterministic only.
            return Understanding()
        if not isinstance(data, dict):
            return Understanding()
        raw_target = data.get("target")
        tgt = raw_target if raw_target in (TARGET_COMPANIES, TARGET_CONTACTS) else None
        slots = data.get("slots")
        return Understanding(target=tgt, slots=slots if isinstance(slots, dict) else {})

    async def _phrase(self, slot: str, icp_state: dict) -> str:
        resp = await self.llm.complete(
            [LLMMessage(role="user", content=f"Ask for the {slot} slot.")],
            purpose="clarify_question",
            variables={"slot": slot, "icp_state": icp_state},
            max_tokens=80,
        )
        return resp.text.strip()

    async def _summarize(self, prior: str, icp_state: dict, target: str) -> str:
        s = get_settings()
        resp = await self.llm.complete(
            [LLMMessage(role="user", content="Fold the ICP into a compact summary.")],
            purpose="chat_summary",
            variables={"prior": prior, "icp_state": icp_state, "target": target},
            max_tokens=s.orch_chat_summary_token_cap,
        )
        text = resp.text.strip()
        cap = s.orch_chat_summary_token_cap * 4
        return text[:cap]
