"""What a reply means, and how sure we are (spec §6, D6, D7, D8, D23).

Two halves, deliberately separate:

* **`read()`** asks the model for a category, a confidence, and — for "later" and out-of-office —
  the *phrase* that names the date. Never the date itself: date arithmetic is code (D15).
  Auto-replies never reach the model for their category; parse.py already knows a machine wrote them.
* **`finalize()`** is pure and is where the rules live, so they hold whatever the model says:
  - A reply with refusal language AND a timeframe is always `unclear`, never `later` (D8): "not
    interested — maybe next year" must reach a person, not schedule itself.
  - `later`, `declined` and `unsubscribe` act without a human, so each needs the mailbox owner's
    confidence bar (D23); below it they become `unclear`.
  - `later` needs a date that resolves exactly one way; otherwise `unclear`.
  - Out-of-office resumes the business day after the return date, or after the workspace default
    (7 days) when no date is found.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

CATEGORIES = ("interested", "question", "referral", "later", "out_of_office", "declined",
              "unsubscribe", "other_auto", "unclear")
#: Categories whose actions run with nobody looking: the confidence bar applies to exactly these.
AUTONOMOUS = ("later", "declined", "unsubscribe")

REFUSAL = re.compile(
    r"\b(not interested|no thanks|no,? thank you|not a (?:good )?fit|not for us|we(?:'re| are) "
    r"(?:all set|good)|don'?t (?:contact|email|reach)|stop (?:emailing|contacting)|remove me|"
    r"take me off|unsubscribe|no need)\b", re.IGNORECASE)
_RETURN_PHRASE = re.compile(
    r"\b(?:back|return(?:ing)?|in the office|available again)\b[^.\n]{0,12}?"
    r"\b(on|from|after|by|until)?\s*([^.\n]{3,40})", re.IGNORECASE)
_UNTIL_PHRASE = re.compile(r"\buntil\s+([^.\n]{3,40})", re.IGNORECASE)

PROMPT = """You read replies to sales emails and classify them. Return ONLY a JSON object:
{"category": one of interested|question|referral|later|out_of_office|declined|unsubscribe|other_auto|unclear,
 "confidence": a number from 0 to 1,
 "date_phrase": the exact words naming when to follow up or when they are back ("" if none),
 "reasoning": one short sentence}
Rules: "later" only for a clear "not now" WITH a timeframe. A refusal mixed with a timeframe is
"unclear". "unsubscribe" is a request to stop emailing. "declined" is a clear no. Never invent a
date phrase that is not in the reply."""


@dataclass(slots=True)
class Reading:
    category: str
    confidence: float
    date_phrase: str = ""
    reasoning: str = ""
    source: str = "ai"          # ai | deterministic


@dataclass(slots=True)
class Verdict:
    category: str
    confidence: float
    date_phrase: str
    resolved_date: date | None
    reasoning: str
    label_source: str
    resume_on: date | None = None     # out-of-office: the business day they are back


def return_phrase(text: str) -> str:
    """The words an out-of-office uses for its return date, for the date resolver."""
    for pattern in (_UNTIL_PHRASE, _RETURN_PHRASE):
        found = pattern.search(text or "")
        if found:
            return found.group(found.lastindex).strip(" ,:;")
    return ""


def finalize(reading: Reading, *, text: str, threshold: float, received_at: datetime, zone,
             ooo_default_days: int = 7) -> Verdict:
    """Apply the rules the model is not trusted with. Pure."""
    from nexus.engagement.dates import resolve_date
    from nexus.engagement.timekeeping import next_business_day, to_local

    category = reading.category if reading.category in CATEGORIES else "unclear"
    confidence = max(0.0, min(1.0, float(reading.confidence or 0)))
    reasons = [reading.reasoning] if reading.reasoning else []
    resolved: date | None = None
    resume: date | None = None

    if category == "out_of_office":
        found = resolve_date(reading.date_phrase or return_phrase(text), received_at, zone)
        back = found.date if found else (to_local(received_at, zone).date()
                                         + timedelta(days=max(1, ooo_default_days)))
        resume = next_business_day(back)
        return Verdict("out_of_office", confidence, reading.date_phrase, back,
                       "; ".join(reasons), reading.source, resume_on=resume)

    if category == "later" and REFUSAL.search(text or ""):
        category = "unclear"
        reasons.append("refusal together with a timeframe goes to the SDR (D8)")
    if category in AUTONOMOUS and confidence < threshold:
        reasons.append(f"{category} read below the {threshold:.2f} confidence bar")
        category = "unclear"
    if category == "later":
        found = resolve_date(reading.date_phrase, received_at, zone)
        if found is None:
            category = "unclear"
            reasons.append("the timeframe does not resolve to one date")
        else:
            resolved = found.date
    return Verdict(category, confidence, reading.date_phrase, resolved, "; ".join(reasons),
                   reading.source)


def deterministic(parsed) -> Reading | None:
    """The readings no model is asked for."""
    if parsed.is_auto:
        text = f"{parsed.subject}\n{parsed.text}"
        looks_away = bool(re.search(r"out of (?:the )?office|away|vacation|holiday|leave|"
                                    r"abwesen|absent|automatic reply", text, re.IGNORECASE))
        if looks_away:
            return Reading("out_of_office", 1.0, return_phrase(parsed.text),
                           parsed.auto_reason, "deterministic")
        return Reading("other_auto", 1.0, "", parsed.auto_reason, "deterministic")
    return None


def parse_reading(text: str) -> Reading:
    """The model's JSON, tolerantly. Anything unusable is an `unclear` the SDR will see."""
    raw = (text or "").strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    try:
        data = json.loads(match.group(0) if match else raw)
    except (ValueError, AttributeError):
        return Reading("unclear", 0.0, "", "the classifier returned no JSON")
    category = str(data.get("category") or "unclear").strip().lower()
    try:
        confidence = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return Reading(category, confidence, str(data.get("date_phrase") or "").strip(),
                   str(data.get("reasoning") or "").strip()[:300])


async def read(ts, *, parsed, context_text: str = "", user_id: str | None = None) -> Reading:
    """One reading. Metered as `ai.reply_classify` when the model is asked. Never raises."""
    from nexus.agents.llm import LLMMessage, get_llm_provider
    from nexus.billing.errors import QuotaExceeded
    from nexus.billing.meter import metered

    known = deterministic(parsed)
    if known is not None:
        return known
    text = parsed.text or parsed.full_text
    messages = [
        LLMMessage(role="system", content=PROMPT),
        LLMMessage(role="user", content=(
            (f"{context_text}\n\n" if context_text else "")
            + f"THE REPLY TO CLASSIFY:\nSubject: {parsed.subject}\n\n{text[:4000]}")),
    ]
    try:
        async with metered(ts, "ai.reply_classify", user_id=user_id, source="engagement"):
            response = await get_llm_provider().complete(
                messages, purpose="reply_classify", max_tokens=600, temperature=0.0,
                variables={"text": text})
    except QuotaExceeded:
        return Reading("unclear", 0.0, "", "the workspace could not cover classification")
    except Exception as exc:
        return Reading("unclear", 0.0, "", f"classification failed: {type(exc).__name__}")
    return parse_reading(response.text)
