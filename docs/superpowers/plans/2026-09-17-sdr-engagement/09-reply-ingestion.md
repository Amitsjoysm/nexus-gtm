# Phase 09: Reply Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every reply to an engagement email is read within minutes, matched to its conversation (or discarded unstored), classified, and acted on — stopping, pausing, snoozing or suppressing the right sequences and alerting the right person — without anything ever being sent to the person who wrote.

**Architecture:** `nexus/engagement/replies/` has one module per decision. `parse.py` (pure) reads RFC 5322 and decides the two things a model must not: bounce and auto-reply. `matching.py` applies the five rules of §6, ending in the privacy filter. `classify.py` asks the model for a category and a *date phrase* only, then `finalize()` (pure) enforces D8 (refusal + timeframe → unclear), the owner's confidence bar (D23) and date resolution (code, not the model). `actions.py` maps each category to its effects; `alerts.py` routes them through the existing alert system. `ingest.py` pulls changes from the stored cursor; `notifications.py` keeps Gmail watches and Graph subscriptions alive and verifies what they send. Two webhooks enqueue a sync; the heartbeat polls every mailbox as the fallback.

**Tech Stack:** Python `email` (modern policy), python-jose (Google OIDC), HMAC (Graph `clientState`), the phase 03 providers, the existing alert routing.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §6, §11, D3, D6, D7, D8, D12, D22, D23. **Depends on:** phases 03, 04, 07, 08.

**Verified:** implemented on `feat/sdr-engagement` on top of phases 01–08 and run in the CI image: `tests/test_engagement_replies.py` (20 passed — parsing, the D8 and confidence rules, OOO resumption, Google OIDC verification against a locally generated RSA key, the Graph handshake and `clientState`, and end-to-end ingestion of interested, dated-later, mixed, unsubscribe, out-of-office, bounce, stranger and colleague mail), plus the sequences, sending, alert, billing-catalog and scheduler suites it touches (177 passed), and `ruff`.

---

## Decisions this phase makes

- **`ai.reply_classify` is added here**, priced at 1 credit (COGS $0.0008, one short structured completion), because this is where it is metered — `tests/test_billing_metering_coverage.py` refuses a priced capability with no call site. Auto-replies and bounces are never sent to the model and never charged.
- **The out-of-office resume is carried on the enrollment as `paused/out_of_office` plus `snoozed_until`**, and the worker resumes the step it was on at that time (§8). No extra email is added.
- **A bounce is not stored.** It is about our email, not a conversation; its only effects are on our outbound row, the do-not-contact list and the enrollment.
- **Both webhooks answer 2xx for anything they ignore** (engine dark, unknown mailbox, malformed body): Pub/Sub and Graph retry a non-2xx for days, and retrying cannot make an ignored notification relevant.

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Create | `nexus/engagement/replies/__init__.py`, `parse.py`, `matching.py`, `classify.py`, `actions.py`, `alerts.py`, `ingest.py`, `notifications.py` | the pipeline |
| Create | `nexus/api/routers/engagement_webhooks.py` | Gmail push + Graph notifications |
| Modify | `nexus/engagement/mailboxes/gmail.py`, `graph.py` | `watch()` |
| Modify | `nexus/alerts/rules.py`, `frontend/src/components/alerts/vocabulary.ts` | the eleven engagement alert categories |
| Modify | `nexus/agents/llm.py` | the offline model's `reply_classify` reading |
| Modify | `nexus/billing/catalog.py`, `nexus/billing/rates.py` | `ai.reply_classify` |
| Modify | `nexus/workers/tasks.py`, `nexus/workers/scheduler.py`, `nexus/engagement/sequences/advance.py`, `nexus/api/routers/__init__.py` | jobs, OOO resumption, router |
| Create | `tests/test_engagement_replies.py` | everything above |

---

### Task 1: Parsing

- [ ] **Step 1: Write the failing tests** — create `tests/test_engagement_replies.py` with the header, `Mailbox`, fixtures, `_mail`, `_bounce` and the three parsing tests from the final file (Task 7).
- [ ] **Step 2: Run** `pytest tests/test_engagement_replies.py -n0 -q` — expected FAIL: `No module named 'nexus.engagement.replies'`.
- [ ] **Step 3: Implement** `nexus/engagement/replies/__init__.py`:

```python
"""Reply ingestion: parse, match, classify, act, and the notification webhooks (spec §6)."""
```

`nexus/engagement/replies/parse.py`:

```python
"""Read an inbound email: who, what it answers, what it says, and whether a person wrote it (§6).

Pure: raw RFC 5322 bytes in, a `Parsed` out. The decisions that must not be left to a model live
here, because they are cheap, reliable and checkable:

* **Bounces** — a delivery status notification (``multipart/report; report-type=delivery-status``)
  or a mailer-daemon/postmaster sender. The ids of OUR messages it reports come from the embedded
  original (``message/rfc822`` or ``text/rfc822-headers``), so a bounce is tied to the exact email
  that failed, not guessed from a subject line.
* **Auto-replies** — ``Auto-Submitted`` other than ``no``, ``X-Autoreply``, ``X-Autorespond``,
  ``Precedence: auto_reply|bulk|junk``, and the subject prefixes every major client puts on an
  out-of-office. The AI never decides whether a human wrote a message.
* **The new text** — the reply with the quoted history cut off, which is what classification
  reads. The whole body is kept for the conversation timeline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as _modern
from email.utils import getaddresses, parsedate_to_datetime

_AUTO_PRECEDENCE = {"auto_reply", "bulk", "junk"}
_OOO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto(?:matic)?[- ]?reply|out of (?:the )?office|ooo\b|abwesenheit|"
    r"absence|réponse automatique|respuesta automática|risposta automatica)", re.IGNORECASE)
_MAILER = re.compile(r"^(mailer-daemon|postmaster)@", re.IGNORECASE)
_MESSAGE_ID = re.compile(r"<[^<>\s]+>")
#: Where quoted history begins, in the forms the common clients write it.
_QUOTE_MARKERS = (
    re.compile(r"^\s*On .{3,200}wrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*Am .{3,200}schrieb .{0,100}:\s*$", re.IGNORECASE),
    re.compile(r"^\s*Le .{3,200}a écrit\s*:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*_{5,}\s*$"),
    re.compile(r"^\s*From:\s.+$", re.IGNORECASE),
)


@dataclass(slots=True)
class Parsed:
    message_id: str = ""
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    from_addr: str = ""
    from_name: str = ""
    to_addrs: list[str] = field(default_factory=list)
    cc_addrs: list[str] = field(default_factory=list)
    subject: str = ""
    date: datetime | None = None
    text: str = ""          # the new part of the message, quoted history removed
    full_text: str = ""     # everything, for the timeline
    is_auto: bool = False
    auto_reason: str = ""
    is_bounce: bool = False
    bounced_message_ids: list[str] = field(default_factory=list)
    bounced_recipients: list[str] = field(default_factory=list)

    @property
    def replied_to_ids(self) -> list[str]:
        """Every id this message says it answers, nearest first."""
        ids = [self.in_reply_to] if self.in_reply_to else []
        return ids + [r for r in reversed(self.references) if r not in ids]


def _addresses(message, header: str) -> list[str]:
    return [addr.strip().lower() for _name, addr in getaddresses(message.get_all(header, []))
            if addr and "@" in addr]


def _body_text(message) -> str:
    """The plain-text body, else the HTML body reduced to text. Never raises."""
    try:
        part = message.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return content.replace("\r\n", "\n").strip()


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    import html as _html

    text = _html.unescape(text)
    return "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()


def strip_quoted(text: str) -> str:
    """The reply without the history it quotes. Keeps everything when no marker is found."""
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if index and any(pattern.match(line) for pattern in _QUOTE_MARKERS):
            lines = lines[:index]
            break
    kept = [line for line in lines if not line.lstrip().startswith(">")]
    return "\n".join(kept).strip()


def _auto_reason(message, subject: str) -> str:
    auto_submitted = (message.get("Auto-Submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return f"Auto-Submitted: {auto_submitted}"
    for header in ("X-Autoreply", "X-Autorespond", "X-Auto-Response-Suppress"):
        if message.get(header) and header != "X-Auto-Response-Suppress":
            return header
    precedence = (message.get("Precedence") or "").strip().lower()
    if precedence in _AUTO_PRECEDENCE:
        return f"Precedence: {precedence}"
    if _OOO_SUBJECT.match(subject or ""):
        return "out-of-office subject"
    return ""


def _bounce(message, from_addr: str) -> tuple[bool, list[str], list[str]]:
    content_type = message.get_content_type()
    report = (content_type == "multipart/report"
              and (message.get_param("report-type") or "").lower() == "delivery-status")
    if not (report or _MAILER.match(from_addr or "")):
        return False, [], []
    ids: list[str] = []
    recipients: list[str] = []
    for part in message.walk():
        kind = part.get_content_type()
        if kind == "message/rfc822":
            inner = part.get_payload(0) if part.is_multipart() else None
            if inner is not None and inner.get("Message-ID"):
                ids.append(inner["Message-ID"].strip())
        elif kind == "text/rfc822-headers":
            found = re.search(r"(?im)^Message-ID:\s*(<[^>]+>)", _rendered(part))
            if found:
                ids.append(found.group(1))
        elif kind == "message/delivery-status":
            # Its payload is a list of header blocks, not text: read it as rendered.
            for recipient in re.findall(r"(?im)^(?:Final|Original)-Recipient:\s*[^;]+;\s*(\S+)",
                                        _rendered(part)):
                recipients.append(recipient.strip().strip("<>").lower())
    if not ids:
        # Some mailers inline the original as text: take every Message-ID they quote.
        ids = [m for m in _MESSAGE_ID.findall(_as_text(message))
               if m != (message.get("Message-ID") or "").strip()]
    return True, list(dict.fromkeys(ids)), list(dict.fromkeys(recipients))


def _rendered(part) -> str:
    try:
        return part.as_string()
    except Exception:
        return _as_text(part)


def _as_text(part) -> str:
    try:
        if part.is_multipart():
            return "\n".join(_as_text(p) for p in part.get_payload())
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = part.get_payload()
            return payload if isinstance(payload, str) else str(payload)
        return payload.decode("utf-8", errors="replace")
    except Exception:
        return ""


def parse(raw: bytes) -> Parsed:
    message: EmailMessage = message_from_bytes(raw or b"", policy=_modern)
    from_list = getaddresses(message.get_all("From", []))
    from_name, from_addr = (from_list[0] if from_list else ("", ""))
    subject = str(message.get("Subject") or "").strip()
    try:
        date = parsedate_to_datetime(message["Date"]) if message.get("Date") else None
    except (TypeError, ValueError):
        date = None
    full = _body_text(message)
    is_bounce, bounced, recipients = _bounce(message, (from_addr or "").lower())
    reason = "" if is_bounce else _auto_reason(message, subject)
    return Parsed(
        message_id=(message.get("Message-ID") or "").strip(),
        in_reply_to=(_MESSAGE_ID.findall(message.get("In-Reply-To") or "") or [""])[0],
        references=_MESSAGE_ID.findall(message.get("References") or ""),
        from_addr=(from_addr or "").strip().lower(), from_name=(from_name or "").strip(),
        to_addrs=_addresses(message, "To"), cc_addrs=_addresses(message, "Cc"),
        subject=subject, date=date, text=strip_quoted(full), full_text=full,
        is_auto=bool(reason), auto_reason=reason, is_bounce=is_bounce,
        bounced_message_ids=bounced, bounced_recipients=recipients,
    )
```

- [ ] **Step 4: Run** — expected `3 passed`. **Step 5: Commit** — `git commit -am "feat(engagement): parse replies; bounces and auto-replies decided by headers"`

---

### Task 2: The rules the model is not trusted with

- [ ] **Step 1: Write the failing tests** — the five classification tests.
- [ ] **Step 2: Implement** `nexus/engagement/replies/classify.py`:

```python
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
```

In `nexus/billing/catalog.py`, beside `ai.contact_rank`:

```python
    _cap("ai.reply_classify", "ai", "AI reply classification", sub_category="outreach",
         default_mode="metered", depends_on=["module.campaigns"],
         description="Reading one reply to an engagement campaign (spec §10)."),
```

In `nexus/billing/rates.py`, beside `ai.contact_rank`:

```python
    # One short structured completion over the reply and its thread.
    _r("ai.reply_classify", 1, 0.0008, "groq structured reading"),
```

In `nexus/agents/llm.py`, `StubLLMProvider._render` gains an offline `reply_classify` reading, placed before `intake_understanding`:

```python
        if purpose == "reply_classify":
            # Deterministic keyword reading for offline runs. It is deliberately over-eager about
            # "later" when a refusal carries a timeframe, the way a real model can be, so the
            # code rule that turns that into `unclear` (D8) is exercised offline too.
            import json as _json
            import re as _re

            text = str(v.get("text") or "").lower()
            timeframe = _re.search(
                r"\b(?:in|after|next|until)\s+(?:q[1-4]|\w+\s+(?:weeks?|months?)|"
                r"january|february|march|april|may|june|july|august|september|october|"
                r"november|december|quarter|year|month)\b[^.,;]*", text)
            phrase = timeframe.group(0).strip() if timeframe else ""
            if _re.search(r"unsubscribe|remove me|stop emailing", text):
                reading = ("unsubscribe", 0.95)
            elif phrase and _re.search(r"not now|try me|reach out|get back|circle back|"
                                       r"not interested|no thanks", text):
                reading = ("later", 0.9)
            elif _re.search(r"not interested|no thanks|not a fit", text):
                reading = ("declined", 0.92)
            elif _re.search(r"talk to|reach out to|speak to|no longer", text):
                reading = ("referral", 0.85)
            elif _re.search(r"interested|let'?s talk|sounds good|happy to chat|book", text):
                reading = ("interested", 0.9)
            elif "?" in text:
                reading = ("question", 0.85)
            else:
                reading = ("unclear", 0.4)
            return _json.dumps({"category": reading[0], "confidence": reading[1],
                                "date_phrase": phrase if reading[0] == "later" else "",
                                "reasoning": "offline keyword reading"})
```

- [ ] **Step 3: Run** — the classification tests pass; `pytest tests/test_billing_metering_coverage.py tests/test_billing_catalog.py tests/test_billing_rates.py -n0 -q` passes. **Commit** — `git commit -am "feat(engagement): classify replies; D8, the confidence bar and dates in code"`

---

### Task 3: Matching, actions and alerts

`nexus/engagement/replies/matching.py`:

```python
"""Which conversation an inbound email belongs to — or none, in which case it is not stored (§6).

The first rule that matches wins:

1. **Known provider thread** → that conversation.
2. **Reply headers** (`In-Reply-To` / `References`) name one of our `Message-ID`s → that
   conversation. Catches clients that break provider threading, and replies to forwards.
3. **Same person, different thread** — the sender is a contact with an enrollment → their most
   recent conversation; effects apply to every live enrollment for that person.
4. **Colleague** — the sender's domain is an account with a live enrollment → `referral` by default,
   and the colleagues' sequences pause (D3).
5. **No match** → discarded without storing a subject or a body. The SDR's mailbox is theirs; only
   mail that belongs to our conversations or known contacts is kept (privacy filter).
"""
from __future__ import annotations

from dataclasses import dataclass, field

LIVE = ("active", "awaiting_review", "paused", "snoozed")
#: Webmail domains are never a company: a Gmail address is not a colleague of every Gmail address.
FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "gmx.de",
    "yandex.com", "zoho.com", "mail.com",
})


@dataclass(slots=True)
class Match:
    rule: str                       # thread | headers | person | colleague | none
    thread: object | None = None
    contact: object | None = None
    account: object | None = None
    answered: object | None = None  # our outbound message this replies to, when known
    enrollments: list = field(default_factory=list)       # this person's live enrollments
    colleague_enrollments: list = field(default_factory=list)  # others at the same company

    @property
    def stored(self) -> bool:
        return self.rule != "none"


def domain_of(address: str) -> str:
    return (address or "").rsplit("@", 1)[-1].strip().lower() if "@" in (address or "") else ""


async def match(ts, *, mailbox, parsed, provider_thread_id: str) -> Match:
    from nexus.models.account import Account, Contact
    from nexus.models.engagement import EngagementMessage, EngagementThread

    thread = await ts.first(EngagementThread,
                            EngagementThread.mailbox_connection_id == mailbox.id,
                            EngagementThread.provider_thread_id == provider_thread_id) \
        if provider_thread_id else None
    rule = "thread" if thread is not None else ""
    answered = None
    for rfc_id in parsed.replied_to_ids:
        answered = await ts.first(EngagementMessage, EngagementMessage.rfc_message_id == rfc_id,
                                  EngagementMessage.direction == "out")
        if answered is not None:
            break
    if thread is None and answered is not None and answered.thread_id:
        thread = await ts.get(EngagementThread, answered.thread_id)
        rule = "headers"

    contact = None
    if thread is not None and thread.contact_id:
        contact = await ts.get(Contact, thread.contact_id)
    if contact is None and parsed.from_addr:
        contact = await _contact_by_email(ts, parsed.from_addr)
    if thread is None and contact is not None and await _has_enrollment(ts, contact.id):
        thread = await _latest_thread(ts, contact.id)
        rule = "person"

    if rule:
        account = await ts.get(Account, contact.account_id) if contact is not None else None
        if answered is None and thread is not None:
            answered = await _latest_outbound(ts, thread.id)
        return Match(rule=rule, thread=thread, contact=contact, account=account,
                     answered=answered,
                     enrollments=await _live_enrollments(ts, contact_id=getattr(contact, "id", None)),
                     colleague_enrollments=await _colleagues(ts, contact, account))

    domain = domain_of(parsed.from_addr)
    if domain and domain not in FREE_MAIL:
        account = await ts.first(Account, Account.domain == domain)
        if account is not None:
            colleagues = await _live_enrollments(ts, account_id=account.id)
            if colleagues:
                return Match(rule="colleague", contact=contact, account=account,
                             thread=await _latest_thread_for_account(ts, account.id),
                             colleague_enrollments=colleagues)
    return Match(rule="none")


async def _contact_by_email(ts, address: str):
    from sqlalchemy import func

    from nexus.models.account import Contact

    return await ts.first(Contact, func.lower(Contact.email) == address.lower())


async def _has_enrollment(ts, contact_id: str) -> bool:
    from nexus.models.engagement import EngagementEnrollment

    return await ts.first(EngagementEnrollment,
                          EngagementEnrollment.contact_id == contact_id) is not None


async def _latest_thread(ts, contact_id: str):
    from nexus.models.engagement import EngagementThread

    rows = await ts.session.scalars(
        ts.select(EngagementThread, EngagementThread.contact_id == contact_id)
        .order_by(EngagementThread.last_message_at.desc()).limit(1))
    return rows.first()


async def _latest_thread_for_account(ts, account_id: str):
    from nexus.models.engagement import EngagementThread

    rows = await ts.session.scalars(
        ts.select(EngagementThread, EngagementThread.account_id == account_id)
        .order_by(EngagementThread.last_message_at.desc()).limit(1))
    return rows.first()


async def _latest_outbound(ts, thread_id: str):
    from nexus.models.engagement import EngagementMessage

    rows = await ts.session.scalars(
        ts.select(EngagementMessage, EngagementMessage.thread_id == thread_id,
                  EngagementMessage.direction == "out", EngagementMessage.status == "sent")
        .order_by(EngagementMessage.sent_at.desc()).limit(1))
    return rows.first()


async def _live_enrollments(ts, *, contact_id: str | None = None, account_id: str | None = None):
    from nexus.models.engagement import EngagementEnrollment

    where = [EngagementEnrollment.status.in_(LIVE)]
    if contact_id:
        where.append(EngagementEnrollment.contact_id == contact_id)
    elif account_id:
        where.append(EngagementEnrollment.account_id == account_id)
    else:
        return []
    return await ts.list(EngagementEnrollment, *where)


async def _colleagues(ts, contact, account) -> list:
    """Live enrollments of OTHER people at the same company — the ones a reply pauses (D3)."""
    if account is None:
        return []
    return [e for e in await _live_enrollments(ts, account_id=account.id)
            if contact is None or e.contact_id != contact.id]
```

`nexus/engagement/replies/alerts.py`:

```python
"""Reply alerts through the existing alert routing (spec §11, D12).

Immediate to the mailbox owner — the person whose name is on the email — for what needs a human now:
interested, question, referral, needs-decision, a campaign out of credits, a mailbox that needs
reconnecting. Everything else (scheduled, out of office, bounced, unsubscribed, declined) is an
in-app `info` alert that the reply desk and the daily digest pick up. Categories are the ones in
`alerts.rules._ENGAGEMENT_RULES`, so a user can subscribe to them like any other.

Never raises: an alert that cannot be delivered must not undo the reply that caused it.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.engagement.replies")

IMMEDIATE = {"reply_interested", "reply_question", "reply_referral", "reply_needs_decision",
             "campaign_out_of_credits", "mailbox_needs_reauth"}


async def notify(ts, *, category: str, title: str, body: str = "", owner_user_id: str | None,
                 account_id: str | None = None, meta: dict | None = None):
    from nexus.alerts.fanout import channels_for, fan_out
    from nexus.alerts.rules import engagement_rule
    from nexus.alerts.service import get_alert_service
    from nexus.core.db import utcnow

    severity, action = engagement_rule(category)
    try:
        alert = await get_alert_service().create(
            ts, title=title, body="\n\n".join(x for x in (body, action) if x),
            severity=severity, account_id=account_id, source="engagement",
            meta={"category": category, "owner_user_id": owner_user_id, **(meta or {})})
        if category in IMMEDIATE:
            channels = await channels_for(ts, category=category, severity=severity,
                                          owner_user_id=owner_user_id, now=utcnow())
            if channels:
                from nexus.alerts.connections import resolve_alert_channels

                await fan_out(alert, channels, await resolve_alert_channels(ts))
                await ts.flush()
        return alert
    except Exception:
        logger.warning("engagement alert %s could not be raised", category, exc_info=True)
        return None
```

`nexus/engagement/replies/actions.py`:

```python
"""What each reply category does (spec §6 table, D3, D6, D7, D8, D22).

Nothing here ever sends to the person who wrote (D22): the only sends a reply can cause are the
re-engagement a "later" schedules for its date and the steps an out-of-office resumes. Responses to
human replies are drafted in phase 10 and sent by the SDR.

| category | this person's sequences | colleagues' | suppression | alert |
|---|---|---|---|---|
| interested / question / referral | stopped `replied` | paused `colleague_replied` | — | immediate |
| later (confident, dated) | snoozed until the date | — | — | digest |
| out_of_office | paused until the business day after they are back | — | — | digest |
| declined | stopped `declined` | paused | `declined` | digest |
| unsubscribe | stopped `unsubscribed` | — | `unsubscribed`, permanent | digest |
| other_auto | nothing | — | — | — |
| unclear | paused `needs_decision` | — | — | immediate |
"""
from __future__ import annotations

from datetime import UTC, time

RESUME_AT = time(9, 0)
HUMAN = ("interested", "question", "referral")


async def apply(ts, *, verdict, match, inbound, mailbox, classification) -> str:
    from nexus.engagement.replies.alerts import notify
    from nexus.engagement.sequences.service import set_status
    from nexus.engagement.suppression.service import suppress
    from nexus.engagement.timekeeping import at_local, zone_or_none

    category = verdict.category
    owner = mailbox.owner_user_id
    who = getattr(match.contact, "full_name", "") or inbound.from_addr
    company = getattr(match.account, "name", "")
    about = f"{who}{f' at {company}' if company else ''}"
    account_id = getattr(match.account, "id", None)
    meta = {"message_id": inbound.id, "classification_id": classification.id,
            "contact_id": getattr(match.contact, "id", None)}

    async def pause_colleagues():
        for enrollment in match.colleague_enrollments:
            if enrollment.status in ("active", "snoozed", "awaiting_review"):
                await set_status(ts, enrollment, "paused", "colleague_replied")

    if category in HUMAN:
        for enrollment in match.enrollments:
            await set_status(ts, enrollment, "stopped", "replied")
        await pause_colleagues()
        await notify(ts, category=f"reply_{category}", owner_user_id=owner, account_id=account_id,
                     title=f"{about} replied: {category}", body=_snippet(inbound.body_text),
                     meta=meta)
        return f"stopped:replied; notified:reply_{category}"

    if category == "later":
        for enrollment in match.enrollments:
            zone = zone_or_none(enrollment.contact_timezone) or UTC
            enrollment.snoozed_until = at_local(verdict.resolved_date, RESUME_AT, zone)
            await set_status(ts, enrollment, "snoozed", "later")
        await notify(ts, category="reply_scheduled", owner_user_id=owner, account_id=account_id,
                     title=f"{about} asked to reconnect on {verdict.resolved_date:%d %b %Y}",
                     body=_snippet(inbound.body_text), meta=meta)
        return f"snoozed:{verdict.resolved_date.isoformat()}"

    if category == "out_of_office":
        for enrollment in match.enrollments:
            if enrollment.status not in ("active", "awaiting_review"):
                continue
            zone = zone_or_none(enrollment.contact_timezone) or UTC
            enrollment.snoozed_until = at_local(verdict.resume_on, RESUME_AT, zone)
            await set_status(ts, enrollment, "paused", "out_of_office")
        await notify(ts, category="reply_out_of_office", owner_user_id=owner,
                     account_id=account_id,
                     title=f"{about} is away until {verdict.resolved_date:%d %b}", meta=meta)
        return f"paused:out_of_office until {verdict.resume_on.isoformat()}"

    if category in ("declined", "unsubscribe"):
        reason = "declined" if category == "declined" else "unsubscribed"
        for enrollment in match.enrollments:
            await set_status(ts, enrollment, "stopped", reason)
        address = getattr(match.contact, "email", "") or inbound.from_addr
        await suppress(ts, email=address, reason=reason,
                       contact_id=getattr(match.contact, "id", None), source_message_id=inbound.id)
        if category == "declined":
            await pause_colleagues()
        await notify(ts, category=f"reply_{reason}", owner_user_id=owner, account_id=account_id,
                     title=f"{about} {reason}", meta=meta)
        return f"stopped:{reason}; suppressed"

    if category == "other_auto":
        return "ignored"

    for enrollment in match.enrollments:
        if enrollment.status not in ("stopped", "completed"):
            await set_status(ts, enrollment, "paused", "needs_decision")
    await notify(ts, category="reply_needs_decision", owner_user_id=owner, account_id=account_id,
                 title=f"{about} replied — needs your decision",
                 body=_snippet(inbound.body_text), meta=meta)
    return "paused:needs_decision; notified"


async def apply_colleague(ts, *, match, inbound, mailbox, classification) -> str:
    """Someone at the company wrote who is not in the sequence: a referral by default (§6 rule 4)."""
    from nexus.engagement.replies.alerts import notify
    from nexus.engagement.sequences.service import set_status

    for enrollment in match.colleague_enrollments:
        if enrollment.status in ("active", "snoozed", "awaiting_review"):
            await set_status(ts, enrollment, "paused", "colleague_replied")
    await notify(ts, category="reply_referral", owner_user_id=mailbox.owner_user_id,
                 account_id=getattr(match.account, "id", None),
                 title=f"Someone at {getattr(match.account, 'name', 'an account')} replied",
                 body=_snippet(inbound.body_text),
                 meta={"message_id": inbound.id, "classification_id": classification.id})
    return "paused:colleague_replied; notified:reply_referral"


def _snippet(text: str, limit: int = 280) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"
```

In `nexus/alerts/rules.py`, the engagement categories join the subscribable set:

```python
# The engagement engine's categories (spec §11): what a reply or a campaign needs from its owner.
# Immediate ones reach the mailbox owner's channels; the rest land in the reply desk and digest.
_ENGAGEMENT_RULES: dict[str, tuple[str, str]] = {
    # category -> (severity, suggested action)
    "reply_interested": ("critical", "Answer today: an interested buyer goes cold in hours."),
    "reply_question": ("warning", "Answer the question, then propose one next step."),
    "reply_referral": ("warning", "Thank them and reach the person they named."),
    "reply_needs_decision": ("warning", "Read it and decide: re-engage, block, close."),
    "reply_scheduled": ("info", "A re-engagement is scheduled for the date they asked."),
    "reply_out_of_office": ("info", "The sequence resumes the day after they are back."),
    "reply_bounced": ("info", "The address bounced and is now on do-not-contact."),
    "reply_unsubscribed": ("info", "They unsubscribed; nothing more will be sent."),
    "reply_declined": ("info", "They said no; they are on do-not-contact until lifted."),
    "campaign_out_of_credits": ("critical", "Top up credits to resume the campaign."),
    "mailbox_needs_reauth": ("critical", "Reconnect the mailbox from My mailboxes."),
}


def engagement_rule(category: str) -> tuple[str, str]:
    return _ENGAGEMENT_RULES.get(category, ("info", ""))


# Categories a user can subscribe to. Derived from the rules so the two cannot drift.
ALERT_CATEGORIES: tuple[str, ...] = tuple(sorted(
    {c for c, _s, _a in _RULES.values()} | set(_ENGAGEMENT_RULES)))
```

`tests/test_alert_setup_ui.py` requires every category to have a label and a blurb, so `frontend/src/components/alerts/vocabulary.ts` gains the eleven entries in `CATEGORY_LABEL`:

```ts
  reply_interested: "Interested reply",
  reply_question: "Reply with a question",
  reply_referral: "Reply pointing to someone else",
  reply_needs_decision: "Reply that needs your decision",
  reply_scheduled: "Re-engagement scheduled",
  reply_out_of_office: "Out of office",
  reply_bounced: "Email bounced",
  reply_unsubscribed: "Unsubscribed",
  reply_declined: "Declined",
  campaign_out_of_credits: "Campaign out of credits",
  mailbox_needs_reauth: "Mailbox needs reconnecting",
```

and in `CATEGORY_BLURB`:

```ts
  reply_interested: "Someone in a sequence wants to talk or learn more. Answer the same day.",
  reply_question: "Someone in a sequence asked something. Their sequence has stopped.",
  reply_referral: "Someone pointed you to a colleague, or a colleague of theirs replied.",
  reply_needs_decision: "A reply the AI could not read with confidence. Nothing sends until you decide.",
  reply_scheduled: "Someone asked to reconnect later; one email goes on the date they gave.",
  reply_out_of_office: "An auto-reply paused a sequence until they are back.",
  reply_bounced: "An email bounced; the address is now on do-not-contact.",
  reply_unsubscribed: "Someone unsubscribed; they will never be emailed again.",
  reply_declined: "Someone said no; they are on do-not-contact until a manager lifts it.",
  campaign_out_of_credits: "A campaign paused because the workspace ran out of credits.",
  mailbox_needs_reauth: "A sending mailbox lost its connection and needs reconnecting.",
```

- [ ] Run `pytest tests/test_alert_setup_ui.py tests/test_signal_alerts.py -n0 -q`; commit — `git commit -am "feat(engagement): match replies, act on them, and alert through the existing routing"`

---

### Task 4: Ingestion and the worker

`nexus/engagement/replies/ingest.py`:

```python
"""Pull what arrived in a mailbox and turn each message into a stored, classified, acted-on reply (§6).

Notifications (Gmail Pub/Sub, Graph subscriptions) only say "something changed"; this module does
the reading, from the stored cursor, and the heartbeat runs it every few minutes as the fallback for
a notification that never came. Running it twice is harmless: a message is unique per mailbox by its
provider id, so the second copy is skipped before anything is written.

Per message, in order:

1. Skip our own sends.
2. A bounce → the message it reports is marked `bounced`, the address goes on do-not-contact
   (`bounced`), that enrollment stops. The bounce itself is not stored: it is about our email.
3. Match it (matching.py). **No match → nothing is stored** (privacy filter).
4. Store it in its conversation — moving the enrollment's current thread there if the person wrote
   in a different one (D16) — and record `reply.received`.
5. Read it (classify.py), apply the rules, act (actions.py), record `reply.classified`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

logger = logging.getLogger("nexus.engagement.replies")

#: After a cursor the provider has forgotten, how far back to look.
RESYNC_WINDOW = timedelta(days=3)


async def sync_mailbox(ts, mailbox, *, now: datetime | None = None) -> dict:
    from nexus.core.db import utcnow
    from nexus.engagement.mailboxes.provider import AuthExpired, CursorExpired, ProviderLimit
    from nexus.engagement.mailboxes.registry import open_provider

    moment = now or utcnow()
    if mailbox.status != "connected":
        return {"skipped": mailbox.status}
    try:
        provider = await open_provider(ts, mailbox)
        try:
            batch = await provider.fetch_changes(mailbox.sync_cursor)
        except CursorExpired:
            since = (mailbox.last_synced_at or moment) - RESYNC_WINDOW
            batch = await provider.resync(since)
    except AuthExpired:
        mailbox.status = "needs_reauth"
        await ts.flush()
        from nexus.engagement.replies.alerts import notify

        await notify(ts, category="mailbox_needs_reauth", owner_user_id=mailbox.owner_user_id,
                     title=f"Reconnect {mailbox.email}",
                     body="Replies cannot be read until the mailbox is reconnected.")
        return {"error": "needs_reauth"}
    except ProviderLimit as exc:
        return {"error": f"provider limit until {exc.retry_at}"}

    outcomes: dict[str, int] = {}
    for provider_message_id in batch.message_ids:
        try:
            async with ts.session.begin_nested():
                outcome = await ingest_message(ts, mailbox, provider, provider_message_id,
                                               now=moment)
        except Exception:
            logger.warning("could not ingest message %s for mailbox %s", provider_message_id,
                           mailbox.id, exc_info=True)
            outcome = "error"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    mailbox.sync_cursor = batch.next_cursor or mailbox.sync_cursor
    mailbox.last_synced_at = moment
    await ts.flush()
    return outcomes


async def ingest_message(ts, mailbox, provider, provider_message_id: str, *,
                         now: datetime) -> str:
    from nexus.engagement.replies import matching
    from nexus.engagement.replies.parse import parse
    from nexus.models.engagement import EngagementMessage

    if await ts.first(EngagementMessage,
                      EngagementMessage.mailbox_connection_id == mailbox.id,
                      EngagementMessage.provider_message_id == provider_message_id) is not None:
        return "duplicate"
    inbound = await provider.get_message(provider_message_id)
    if inbound.outgoing:
        return "own_send"
    parsed = parse(inbound.raw)
    if parsed.from_addr and parsed.from_addr == (mailbox.email or "").lower():
        return "own_send"
    if parsed.is_bounce:
        return await _bounce(ts, mailbox, parsed)

    found = await matching.match(ts, mailbox=mailbox, parsed=parsed,
                                 provider_thread_id=inbound.provider_thread_id)
    if not found.stored:
        return "discarded"

    thread = await _thread(ts, mailbox, found, inbound, parsed, now)
    row = EngagementMessage(
        mailbox_connection_id=mailbox.id, thread_id=thread.id, contact_id=getattr(
            found.contact, "id", None),
        enrollment_id=getattr(found.enrollments[0], "id", None) if found.enrollments else None,
        direction="in", kind="reply", status="received",
        provider_message_id=inbound.provider_message_id, rfc_message_id=parsed.message_id or None,
        in_reply_to=parsed.in_reply_to or None, references_header=" ".join(parsed.references),
        from_addr=parsed.from_addr, to_addrs=parsed.to_addrs, cc_addrs=parsed.cc_addrs,
        subject=parsed.subject, body_text=parsed.full_text,
        inbound_kind="auto_reply" if parsed.is_auto else "human",
        received_at=inbound.received_at or now)
    ts.add(row)
    await ts.flush()
    for enrollment in found.enrollments:
        # A reply in another thread moves the conversation there (D16).
        enrollment.current_thread_id = thread.id
    await _record_received(ts, row, found, mailbox, parsed)
    return await _classify_and_act(ts, row, found, mailbox, parsed)


async def _thread(ts, mailbox, found, inbound, parsed, now):
    from nexus.engagement.sending.service import thread_for

    if found.thread is not None and (not inbound.provider_thread_id or
                                     found.thread.provider_thread_id == inbound.provider_thread_id):
        found.thread.last_message_at = now
        await ts.flush()
        return found.thread

    class _Seed:  # thread_for reads these off the message it is given
        mailbox_connection_id = mailbox.id
        contact_id = getattr(found.contact, "id", None)
        enrollment_id = getattr(found.enrollments[0], "id", None) if found.enrollments else None
        subject = parsed.subject

    thread = await thread_for(ts, _Seed, inbound.provider_thread_id or parsed.message_id, now)
    if found.account is not None and not thread.account_id:
        thread.account_id = found.account.id
    await ts.flush()
    return thread


async def _classify_and_act(ts, row, found, mailbox, parsed) -> str:
    from nexus.engagement.drafting.context import conversation_block
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.replies import actions, classify
    from nexus.engagement.settings import effective_confidence, read_settings
    from nexus.engagement.timekeeping import zone_or_none
    from nexus.models.engagement import EngagementMessage, ReplyClassification
    from nexus.models.identity import Tenant

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    settings = read_settings(getattr(tenant, "email_settings", None))
    threshold = effective_confidence(settings, mailbox.reply_confidence)
    zone_name = (found.enrollments[0].contact_timezone if found.enrollments
                 else mailbox.timezone) or "UTC"
    history = await ts.session.scalars(
        ts.select(EngagementMessage, EngagementMessage.thread_id == row.thread_id,
                  EngagementMessage.id != row.id,
                  EngagementMessage.status.in_(("sent", "received")))
        .order_by(EngagementMessage.created_at.asc()))
    context_text = conversation_block(list(history.all()))

    if found.rule == "colleague":
        reading = classify.Reading("referral", 1.0, "", "a colleague of the contact replied",
                                   "deterministic")
    else:
        reading = await classify.read(ts, parsed=parsed, context_text=context_text,
                                      user_id=mailbox.owner_user_id)
    verdict = classify.finalize(reading, text=parsed.text, threshold=threshold,
                                received_at=row.received_at, zone=zone_or_none(zone_name) or UTC,
                                ooo_default_days=settings.ooo_default_days)
    classification = ReplyClassification(
        message_id=row.id, mailbox_connection_id=mailbox.id,
        enrollment_id=row.enrollment_id, contact_id=row.contact_id,
        account_id=getattr(found.account, "id", None), category=verdict.category,
        confidence=verdict.confidence, date_phrase=verdict.date_phrase or None,
        resolved_date=verdict.resolved_date, reasoning=verdict.reasoning,
        label_source=verdict.label_source, assigned_user_id=mailbox.owner_user_id,
        status="open" if verdict.category in ("interested", "question", "referral", "unclear")
        else "done")
    ts.add(classification)
    await ts.flush()
    if found.rule == "colleague":
        action = await actions.apply_colleague(ts, match=found, inbound=row, mailbox=mailbox,
                                               classification=classification)
    else:
        action = await actions.apply(ts, verdict=verdict, match=found, inbound=row,
                                     mailbox=mailbox, classification=classification)
    classification.action_taken = action[:40]
    await ts.flush()
    await emit(ts, "reply.classified", refs=_refs(row, found, mailbox, classification),
               payload={"category": verdict.category, "confidence": verdict.confidence,
                        "date_phrase": verdict.date_phrase,
                        "resolved_date": verdict.resolved_date.isoformat()
                        if verdict.resolved_date else "",
                        "label_source": verdict.label_source, "body": parsed.text,
                        "action": action})
    return f"classified:{verdict.category}"


async def _record_received(ts, row, found, mailbox, parsed) -> None:
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.timekeeping import to_local, zone_or_none

    zone_name = (found.enrollments[0].contact_timezone if found.enrollments
                 else mailbox.timezone) or "UTC"
    local = to_local(row.received_at, zone_or_none(zone_name) or UTC)
    latency = None
    if found.answered is not None and found.answered.sent_at:
        sent_at = found.answered.sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=UTC)
        latency = int((row.received_at - sent_at).total_seconds())
    await emit(ts, "reply.received", refs=_refs(row, found, mailbox, None),
               payload={"inbound_kind": row.inbound_kind, "local_hour": local.hour,
                        "local_weekday": local.weekday(), "response_latency_s": latency,
                        "body": parsed.text, "match_rule": found.rule})


def _refs(row, found, mailbox, classification) -> dict:
    return {"message_id": row.id, "thread_id": row.thread_id, "mailbox_id": mailbox.id,
            "contact_id": row.contact_id, "account_id": getattr(found.account, "id", None),
            "enrollment_id": row.enrollment_id,
            "answered_message_id": getattr(found.answered, "id", None),
            "classification_id": getattr(classification, "id", None)}


async def _bounce(ts, mailbox, parsed) -> str:
    from nexus.engagement.ledger.emit import emit
    from nexus.engagement.replies.alerts import notify
    from nexus.engagement.sequences.service import set_status
    from nexus.engagement.suppression.service import suppress
    from nexus.models.account import Contact
    from nexus.models.engagement import EngagementEnrollment, EngagementMessage

    ours = []
    for rfc_id in parsed.bounced_message_ids:
        message = await ts.first(EngagementMessage, EngagementMessage.rfc_message_id == rfc_id,
                                 EngagementMessage.direction == "out")
        if message is not None:
            ours.append(message)
    if not ours:
        return "discarded"          # a bounce about somebody else's email is not ours to keep
    for message in ours:
        message.status = "bounced"
        contact = await ts.get(Contact, message.contact_id) if message.contact_id else None
        address = (getattr(contact, "email", "") or (message.to_addrs or [""])[0])
        if address:
            await suppress(ts, email=address, reason="bounced", contact_id=message.contact_id,
                           source_message_id=message.id)
        if message.enrollment_id:
            enrollment = await ts.get(EngagementEnrollment, message.enrollment_id)
            if enrollment is not None and enrollment.status not in ("stopped", "completed"):
                await set_status(ts, enrollment, "stopped", "bounced")
        await emit(ts, "message.bounced",
                   refs={"message_id": message.id, "contact_id": message.contact_id,
                         "mailbox_id": mailbox.id, "enrollment_id": message.enrollment_id},
                   payload={"recipients": parsed.bounced_recipients})
        await notify(ts, category="reply_bounced", owner_user_id=mailbox.owner_user_id,
                     title=f"{address} bounced", body="The address is now on do-not-contact.")
    await ts.flush()
    return "bounced"
```

In `nexus/workers/tasks.py`, add `handle_sync_mailbox`, `handle_sync_mailboxes`, `enqueue_sync_mailbox`, `enqueue_sync_mailboxes` above `enqueue_advance_engagement`, and register `"sync_mailbox"` and `"sync_mailboxes"` in `HANDLERS`:

```python
async def handle_sync_mailbox(payload: dict) -> dict:
    """Read what arrived in one mailbox (a notification said something changed)."""
    from nexus.engagement import config
    from nexus.engagement.replies.ingest import sync_mailbox
    from nexus.models.engagement import MailboxConnection

    if not config.campaigns_enabled():
        return {"skipped": "engagement campaigns are switched off"}
    tenant_id, mailbox_id = payload.get("tenant_id"), payload.get("mailbox_id")
    if not tenant_id or not mailbox_id:
        return {"error": "tenant_id and mailbox_id are required"}
    async with tenant_session(tenant_id) as ts:
        mailbox = await ts.get(MailboxConnection, mailbox_id)
        if mailbox is None:
            return {"error": "mailbox_not_found"}
        return await sync_mailbox(ts, mailbox)


async def handle_sync_mailboxes(payload: dict) -> dict:
    """The fallback poll (spec §6): every connected mailbox not read in the last few minutes,
    and notification subscriptions renewed a day before they lapse."""
    from datetime import timedelta

    from sqlalchemy import or_, select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.engagement import config
    from nexus.engagement.replies.ingest import sync_mailbox
    from nexus.engagement.replies.notifications import renew, renewal_due
    from nexus.models.engagement import MailboxConnection

    if not config.campaigns_enabled():
        return {"skipped": "engagement campaigns are switched off"}
    now = utcnow()
    stale = now - timedelta(minutes=5)
    async with get_platform_sessionmaker()() as session:
        rows = (await session.execute(
            select(MailboxConnection.tenant_id, MailboxConnection.id)
            .where(MailboxConnection.status == "connected")
            .where(or_(MailboxConnection.last_synced_at.is_(None),
                       MailboxConnection.last_synced_at <= stale))
            .limit(200))).all()
    synced = renewed = 0
    for tenant_id, mailbox_id in rows:
        try:
            async with tenant_session(tenant_id) as ts:
                mailbox = await ts.get(MailboxConnection, mailbox_id)
                if mailbox is None:
                    continue
                if renewal_due(mailbox, now) and await renew(ts, mailbox, now=now) == "renewed":
                    renewed += 1
                await sync_mailbox(ts, mailbox, now=now)
                synced += 1
        except Exception:
            logger.warning("mailbox %s could not be synced", mailbox_id, exc_info=True)
    return {"synced": synced, "renewed": renewed}


async def enqueue_sync_mailbox(tenant_id: str, mailbox_id: str, *,
                              queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="sync_mailbox",
                            payload={"tenant_id": tenant_id, "mailbox_id": mailbox_id}))


async def enqueue_sync_mailboxes(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="sync_mailboxes", payload={}))
```

In `nexus/workers/scheduler.py`, import `enqueue_sync_mailboxes` and enqueue it beside `advance_engagement` (same switch, `count += 2`).

In `nexus/engagement/sequences/advance.py`, `due_enrollments` also selects `status == "paused" AND status_reason == "out_of_office" AND snoozed_until <= now`, and `process` resumes such an enrollment before deciding what is due:

```python
    if (enrollment.status == "paused" and enrollment.status_reason == "out_of_office"
            and enrollment.snoozed_until and enrollment.snoozed_until <= now):
        # Back from out of office: resume the step it was on (§8). No extra email is added.
        enrollment.snoozed_until = None
        enrollment.next_action_at = now
        await set_status(ts, enrollment, "active")
```

- [ ] Run the ingestion tests; commit — `git commit -am "feat(engagement): ingest replies from the cursor; OOO pauses resume on the date"`

---

### Task 5: Notifications and webhooks

`nexus/engagement/replies/notifications.py`:

```python
"""Keeping "something changed" notifications alive, and trusting the ones that arrive (spec §6, §12).

**Subscriptions expire, so they are renewed.** A Gmail `users.watch` lasts seven days; a Graph
subscription on messages at most a few days. `renew_due` re-subscribes anything expiring within a
day. A lapsed one costs latency, not replies: the heartbeat polls every mailbox as the fallback.

**Trust, per provider — neither webhook has a shared secret in its URL:**

* **Gmail** pushes through Pub/Sub with a Google-signed OIDC token. `verify_push_token` checks it
  against Google's published keys, this deployment's audience and the configured push service
  account. With no service account configured the webhook refuses everything rather than accept an
  unauthenticated push.
* **Graph** echoes a validation token once, at subscription time, and then posts notifications that
  carry the `clientState` we chose: an HMAC of the subscription id under `secret_key`, so a forged
  notification cannot name a subscription and pass.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta

logger = logging.getLogger("nexus.engagement.replies")

GOOGLE_CERTS = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
RENEW_WITHIN = timedelta(hours=24)
_CERTS_TTL = 3600.0
_certs_cache: tuple[float, dict] | None = None


class PushRejected(PermissionError):
    """The notification could not be proven to come from the provider."""


def client_state(subscription_id: str) -> str:
    from nexus.core.config import get_settings

    key = get_settings().secret_key.encode()
    return hmac.new(key, f"engagement:graph:{subscription_id}".encode(),
                    hashlib.sha256).hexdigest()[:64]


def client_state_seed() -> str:
    """The clientState for a subscription whose id we do not know yet (it is assigned on create):
    keyed on the deployment, and replaced by the id-keyed value on the first renewal."""
    return client_state("new")


def valid_client_state(subscription_id: str, value: str) -> bool:
    return any(hmac.compare_digest(value or "", candidate)
               for candidate in (client_state(subscription_id), client_state_seed()))


def verify_push_token(token: str, *, jwks: dict, audience: str, service_account: str,
                      now: float | None = None) -> dict:
    """The claims of a valid Google push token, or `PushRejected`. Pure given the key set."""
    from jose import JWTError, jwt

    if not service_account:
        raise PushRejected("no push service account is configured")
    if not token:
        raise PushRejected("no bearer token")
    try:
        claims = jwt.decode(token, jwks, algorithms=["RS256"], audience=audience,
                            options={"verify_at_hash": False})
    except JWTError as exc:
        raise PushRejected(f"invalid token: {exc}") from exc
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise PushRejected("the token was not issued by Google")
    if (claims.get("email") or "").lower() != service_account.lower():
        raise PushRejected("the token is for another service account")
    if claims.get("email_verified") is not True:
        raise PushRejected("the service account email is not verified")
    if now is not None and float(claims.get("exp", 0)) < now:
        raise PushRejected("the token has expired")
    return claims


async def google_jwks() -> dict:
    """Google's current signing keys, cached for an hour (they rotate, slowly)."""
    import httpx

    global _certs_cache
    if _certs_cache is not None and time.monotonic() - _certs_cache[0] < _CERTS_TTL:
        return _certs_cache[1]
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(GOOGLE_CERTS)
        resp.raise_for_status()
        keys = resp.json()
    _certs_cache = (time.monotonic(), keys)
    return keys


async def renew(ts, mailbox, *, now: datetime) -> str:
    """(Re)subscribe one mailbox. Returns what happened; never raises."""
    from nexus.engagement import config
    from nexus.engagement.mailboxes.registry import open_provider

    try:
        provider = await open_provider(ts, mailbox)
        if mailbox.provider == "google":
            topic = config.gmail_pubsub_topic()
            if not topic:
                return "no_topic"
            subscription_id, expires = await provider.watch(topic=topic)
        else:
            url = config.graph_notification_url()
            if not url:
                return "no_public_url"
            existing = mailbox.notification_subscription_id or ""
            subscription_id, expires = await provider.watch(
                notification_url=url, subscription_id=existing,
                client_state=client_state(existing) if existing else client_state_seed())
        mailbox.notification_subscription_id = subscription_id or None
        mailbox.notifications_expire_at = expires
        await ts.flush()
        return "renewed"
    except Exception as exc:
        logger.warning("could not renew notifications for mailbox %s", mailbox.id, exc_info=True)
        return f"failed:{type(exc).__name__}"


def renewal_due(mailbox, now: datetime) -> bool:
    expires = mailbox.notifications_expire_at
    if expires is None:
        return True
    if expires.tzinfo is None:
        from datetime import UTC

        expires = expires.replace(tzinfo=UTC)
    return expires - now < RENEW_WITHIN
```

`nexus/api/routers/engagement_webhooks.py`:

```python
"""Gmail and Graph "something changed" webhooks (spec §6, §12).

Both are public by necessity and do the same small thing: prove the notification is genuine, find
the mailbox it is about, and enqueue a sync of that mailbox. They never read mail themselves — the
worker does, from the stored cursor — so a replayed or duplicated notification costs one extra sync
that finds nothing new.

Both answer 2xx for anything they choose to ignore (an unknown mailbox, the engine switched off):
Pub/Sub and Graph retry a non-2xx for days, and retrying cannot make an ignored notification
relevant. A notification that fails verification is 401/403, which is what those services expect.
"""
from __future__ import annotations

import base64
import json
import logging

from fastapi import APIRouter, HTTPException, Request, Response, status

logger = logging.getLogger("nexus.engagement.replies")

router = APIRouter(prefix="/engagement/webhooks", tags=["engagement-webhooks"])


async def _enqueue_sync(tenant_id: str, mailbox_id: str) -> None:
    from nexus.workers.tasks import enqueue_sync_mailbox

    await enqueue_sync_mailbox(tenant_id, mailbox_id)


async def _mailbox_where(*where):
    """Cross-tenant lookup by address or subscription id — the notification names no tenant."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.engagement import MailboxConnection

    async with get_platform_sessionmaker()() as session:
        row = (await session.execute(
            select(MailboxConnection.tenant_id, MailboxConnection.id)
            .where(MailboxConnection.status == "connected", *where).limit(1))).first()
    return row


@router.post("/gmail", status_code=204, response_model=None)
async def gmail_push(request: Request) -> Response:
    from nexus.engagement import config
    from nexus.engagement.replies.notifications import (
        PushRejected,
        google_jwks,
        verify_push_token,
    )
    from nexus.models.engagement import MailboxConnection

    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    try:
        verify_push_token(token, jwks=await google_jwks(), audience=config.gmail_push_audience(),
                          service_account=config.gmail_push_service_account())
    except PushRejected as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "push not verified") from exc
    if not config.campaigns_enabled():
        return Response(status_code=204)
    try:
        envelope = await request.json()
        data = json.loads(base64.b64decode(envelope["message"]["data"]).decode("utf-8"))
        address = str(data.get("emailAddress") or "").strip().lower()
    except Exception:
        return Response(status_code=204)     # malformed: acknowledge, never retry forever
    found = await _mailbox_where(MailboxConnection.email == address) if address else None
    if found is not None:
        await _enqueue_sync(found[0], found[1])
    return Response(status_code=204)


@router.post("/graph", response_model=None)
async def graph_notification(request: Request) -> Response:
    from nexus.engagement import config
    from nexus.engagement.replies.notifications import valid_client_state
    from nexus.models.engagement import MailboxConnection

    token = request.query_params.get("validationToken")
    if token is not None:
        # The subscription handshake: echo the token as plain text within ten seconds.
        return Response(content=token, media_type="text/plain", status_code=200)
    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=202)
    queued: set[tuple[str, str]] = set()
    for item in payload.get("value", []) or []:
        subscription_id = str(item.get("subscriptionId") or "")
        if not valid_client_state(subscription_id, str(item.get("clientState") or "")):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "clientState does not verify")
        if not config.campaigns_enabled():
            continue
        found = await _mailbox_where(
            MailboxConnection.notification_subscription_id == subscription_id)
        if found is not None and tuple(found) not in queued:
            queued.add(tuple(found))
            await _enqueue_sync(found[0], found[1])
    return Response(status_code=202)
```

In `nexus/engagement/mailboxes/gmail.py`, above `resync`:

```python
    async def watch(self, *, topic: str, **_ignored) -> tuple[str, datetime]:
        """``users.watch`` on the inbox: Gmail publishes each change to ``topic`` for seven days.
        Returns ``("", expires_at)`` — a Gmail watch has no id; re-watching replaces it."""
        data = await self._post("/watch", {"topicName": topic, "labelIds": ["INBOX"],
                                           "labelFilterBehavior": "include"})
        expires_ms = int(data.get("expiration") or 0)
        return "", datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc)
```

In `nexus/engagement/mailboxes/graph.py`, import `NotFound` from the provider module, add above `resync`:

```python
    async def watch(self, *, notification_url: str, client_state: str,
                    subscription_id: str = "", **_ignored) -> tuple[str, datetime]:
        """A ``created`` subscription on the Inbox, renewed in place when it still exists.

        Message subscriptions last under three days; the heartbeat renews a day ahead. A renewal
        the service has forgotten (404) creates a new subscription instead."""
        expires = datetime.now(timezone.utc) + timedelta(minutes=4000)
        stamp = expires.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        if subscription_id:
            try:
                resp = await transport.request(
                    "PATCH", f"{GRAPH}/subscriptions/{subscription_id}", token=self._token,
                    json={"expirationDateTime": stamp})
                return subscription_id, _graph_time(resp.json().get("expirationDateTime")) \
                    or expires
            except NotFound:
                pass
        resp = await transport.request("POST", f"{GRAPH}/subscriptions", token=self._token, json={
            "changeType": "created", "notificationUrl": notification_url,
            "resource": "/me/mailFolders('Inbox')/messages", "expirationDateTime": stamp,
            "clientState": client_state})
        data = resp.json()
        return str(data.get("id") or ""), _graph_time(data.get("expirationDateTime")) or expires
```

and at the end of the module:

```python
def _graph_time(value) -> datetime | None:
    """Graph's ISO timestamps carry seven fractional digits, which ``fromisoformat``
    refuses before Python 3.11."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(c for c in tail if c.isdigit())[:6]
        zone = tail[len("".join(c for c in tail if c.isdigit())):]
        text = f"{head}.{digits}{zone}"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
```

Register `engagement_webhooks` in `nexus/api/routers/__init__.py` beside `engagement_templates`.

- [ ] Run the webhook tests; commit — `git commit -am "feat(engagement): Gmail push (OIDC) and Graph (clientState) webhooks, renewals"`

**Live check for the owner (after Google Cloud and Azure setup):** connect the test mailboxes, send a campaign email to the other test mailbox, reply to it, and confirm the reply desk shows it within a minute (push) or five (poll).

---

### Task 7: The whole test file

```python
"""Reply ingestion: parsing, matching, the privacy filter, classification rules, actions, bounces,
out-of-office, and the two webhooks (spec §6, §11, D3, D6, D7, D8, D22, D23).

Messages are real RFC 5322 bytes built with the standard library and served by `Mailbox`, a provider
double installed through the registry seam that keeps a Sent folder (for sends) and an inbox (for
what arrives). What Gmail and Graph actually deliver is covered by `tests_live/engagement/`.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

import pytest

from nexus.core.config import get_settings
from tests.conftest import tenant_session
from tests.test_engagement_sending import SentFolder
from tests.test_engagement_sequences import _enrollment, _launched, _run

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


class Mailbox(SentFolder):
    """Sends land in a Sent folder; arrivals are served from an inbox in order."""

    def __init__(self):
        super().__init__()
        self.inbox: list = []

    def arrive(self, raw: bytes, *, thread_id: str = "thread-1", when: datetime = NOW) -> str:
        from nexus.engagement.mailboxes.provider import InboundMessage

        message_id = f"in-{len(self.inbox) + 1}"
        self.inbox.append(InboundMessage(provider_message_id=message_id,
                                         provider_thread_id=thread_id, raw=raw,
                                         received_at=when))
        return message_id

    async def fetch_changes(self, cursor):
        from nexus.engagement.mailboxes.provider import ChangeBatch

        start = int(cursor or 0)
        return ChangeBatch(message_ids=[m.provider_message_id for m in self.inbox[start:]],
                           next_cursor=str(len(self.inbox)))

    async def get_message(self, provider_message_id):
        return next(m for m in self.inbox if m.provider_message_id == provider_message_id)


@pytest.fixture
def mailbox_double():
    from nexus.engagement.mailboxes import registry

    box = Mailbox()
    registry.set_provider_factory(lambda _connection: box)
    yield box
    registry.set_provider_factory(None)


def _mail(*, sender: str, body: str, subject: str = "Re: Quick question", in_reply_to: str = "",
          headers: dict | None = None, when: datetime = NOW) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "sam@seq.com"
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain="acme.io")
    message["Date"] = format_datetime(when)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(body)
    return message.as_bytes()


def _bounce(original: bytes, recipient: str) -> bytes:
    """A delivery status notification as Gmail sends one: a multipart/report carrying the status
    block and our original message."""
    crlf = "\r\n"
    head = crlf.join([
        "From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
        "To: sam@seq.com",
        "Subject: Delivery Status Notification (Failure)",
        "Message-ID: <bounce-1@googlemail.com>",
        "MIME-Version: 1.0",
        'Content-Type: multipart/report; report-type=delivery-status; boundary="B"',
        "",
        "--B",
        "Content-Type: text/plain; charset=utf-8",
        "",
        "Your message wasn't delivered.",
        "",
        "--B",
        "Content-Type: message/delivery-status",
        "",
        "Reporting-MTA: dns; googlemail.com",
        "",
        f"Final-Recipient: rfc822; {recipient}",
        "Action: failed",
        "Status: 5.1.1",
        "",
        "--B",
        "Content-Type: message/rfc822",
        "",
        "",
    ]).encode()
    return head + original + f"{crlf}--B--{crlf}".encode()


# ---- parsing ------------------------------------------------------------------------------------

def test_the_new_text_is_read_without_the_history_it_quotes():
    from nexus.engagement.replies.parse import parse

    raw = _mail(sender="Jane Buyer <Jane0@Acme.io>", in_reply_to="<m1@seq.com>", body=(
        "Sounds interesting — can you send pricing?\n\n"
        "On Mon, 21 Sep 2026 at 10:00, Sam Rep <sam@seq.com> wrote:\n> Hi Jane,\n> Worth a chat?"))
    parsed = parse(raw)
    assert parsed.from_addr == "jane0@acme.io" and parsed.from_name == "Jane Buyer"
    assert parsed.in_reply_to == "<m1@seq.com>" and parsed.replied_to_ids == ["<m1@seq.com>"]
    assert parsed.text == "Sounds interesting — can you send pricing?"
    assert "Worth a chat?" in parsed.full_text
    assert not parsed.is_auto and not parsed.is_bounce


def test_auto_replies_are_recognised_by_their_headers_not_by_a_model():
    from nexus.engagement.replies.parse import parse

    assert parse(_mail(sender="jane0@acme.io", body="Away",
                       headers={"Auto-Submitted": "auto-replied"})).is_auto
    assert parse(_mail(sender="jane0@acme.io", body="Away",
                       headers={"Precedence": "bulk"})).is_auto
    assert parse(_mail(sender="jane0@acme.io", body="Away",
                       subject="Automatic reply: Quick question")).is_auto
    assert not parse(_mail(sender="jane0@acme.io", body="Hi",
                           headers={"Auto-Submitted": "no"})).is_auto


def test_a_bounce_names_the_exact_message_that_failed():
    from nexus.engagement.replies.parse import parse

    original = _mail(sender="sam@seq.com", body="Hi Jane")
    ours = parse(original).message_id
    parsed = parse(_bounce(original, "jane0@acme.io"))
    assert parsed.is_bounce
    assert ours in parsed.bounced_message_ids
    assert parsed.bounced_recipients == ["jane0@acme.io"]


# ---- the rules the model is not trusted with --------------------------------------------------------

def _finalize(category, *, confidence=0.9, phrase="", text="", threshold=0.8):
    from nexus.engagement.replies.classify import Reading, finalize

    return finalize(Reading(category, confidence, phrase), text=text, threshold=threshold,
                    received_at=NOW, zone=UTC)


def test_a_refusal_with_a_timeframe_is_never_scheduled():
    verdict = _finalize("later", phrase="next June",
                        text="Not interested right now. Maybe next June.")
    assert verdict.category == "unclear" and "D8" in verdict.reasoning


def test_later_needs_confidence_and_a_date_that_resolves_one_way():
    confident = _finalize("later", phrase="next June", text="Try me next June.")
    assert confident.category == "later" and confident.resolved_date == date(2027, 6, 1)
    assert _finalize("later", confidence=0.6, phrase="next June",
                     text="Try me next June.").category == "unclear"
    assert _finalize("later", phrase="sometime", text="Sometime.").category == "unclear"


def test_the_actions_that_run_unattended_need_the_owners_confidence_bar():
    assert _finalize("declined", confidence=0.95).category == "declined"
    assert _finalize("declined", confidence=0.7).category == "unclear"
    assert _finalize("unsubscribe", confidence=0.7, threshold=0.6).category == "unsubscribe"
    # A human-facing category is never downgraded: a person reads it anyway.
    assert _finalize("interested", confidence=0.3).category == "interested"


def test_out_of_office_resumes_the_business_day_after_they_are_back():
    from nexus.engagement.replies.classify import Reading, finalize, return_phrase

    assert return_phrase("I am out of the office until Friday 2 October.") \
        .startswith("Friday 2 October")
    back = finalize(Reading("out_of_office", 1.0, "Friday 2 October"), text="",
                    threshold=0.8, received_at=NOW, zone=UTC)
    assert back.resolved_date == date(2026, 10, 2) and back.resume_on == date(2026, 10, 5)
    unknown = finalize(Reading("out_of_office", 1.0, ""), text="Away for a while",
                       threshold=0.8, received_at=NOW, zone=UTC, ooo_default_days=7)
    assert unknown.resolved_date == date(2026, 9, 29) and unknown.resume_on == date(2026, 9, 30)


def test_an_unusable_model_answer_is_unclear_not_a_guess():
    from nexus.engagement.replies.classify import parse_reading

    assert parse_reading("no json here").category == "unclear"
    reading = parse_reading('Sure! {"category": "question", "confidence": 0.8, "date_phrase": ""}')
    assert (reading.category, reading.confidence) == ("question", 0.8)


# ---- webhooks -------------------------------------------------------------------------------------

def _rsa_jwks():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwk

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    public = jwk.construct(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo), "RS256")
    public_jwk = {**public.to_dict(), "kid": "k1", "use": "sig"}
    return pem, {"keys": [public_jwk]}


def _token(pem, **claims):
    from jose import jwt

    body = {"iss": "https://accounts.google.com", "aud": "https://app.example.com/api/engagement/"
            "webhooks/gmail", "email": "push@proj.iam.gserviceaccount.com",
            # Signed "now" by the real clock: jose checks expiry against it.
            "email_verified": True, "exp": int(time.time()) + 3600, "iat": int(time.time())}
    body.update(claims)
    return jwt.encode(body, pem, algorithm="RS256", headers={"kid": "k1"})


def test_a_gmail_push_is_trusted_only_with_googles_signature_for_our_audience():
    from nexus.engagement.replies.notifications import PushRejected, verify_push_token

    pem, jwks = _rsa_jwks()
    audience = "https://app.example.com/api/engagement/webhooks/gmail"
    account = "push@proj.iam.gserviceaccount.com"
    claims = verify_push_token(_token(pem), jwks=jwks, audience=audience,
                               service_account=account)
    assert claims["email"] == account
    for bad in (_token(pem, aud="https://evil.example.com/hook"),
                _token(pem, email="someone@else.iam.gserviceaccount.com"),
                _token(pem, iss="https://evil.example.com"),
                _token(pem, email_verified=False), "not-a-token"):
        with pytest.raises(PushRejected):
            verify_push_token(bad, jwks=jwks, audience=audience, service_account=account)
    other_pem, _ = _rsa_jwks()
    with pytest.raises(PushRejected):
        verify_push_token(_token(other_pem), jwks=jwks, audience=audience, service_account=account)
    with pytest.raises(PushRejected):
        verify_push_token(_token(pem), jwks=jwks, audience=audience, service_account="")


async def test_graph_is_answered_during_the_handshake_and_refused_with_a_forged_client_state(
    client,
):
    from nexus.engagement.replies.notifications import client_state

    echo = await client.post("/api/engagement/webhooks/graph?validationToken=abc%20123")
    assert echo.status_code == 200 and echo.text == "abc 123"
    forged = await client.post("/api/engagement/webhooks/graph", json={"value": [
        {"subscriptionId": "sub-1", "clientState": "guess"}]})
    assert forged.status_code == 403
    genuine = await client.post("/api/engagement/webhooks/graph", json={"value": [
        {"subscriptionId": "sub-1", "clientState": client_state("sub-1")}]})
    assert genuine.status_code == 202


async def test_a_gmail_push_without_a_configured_service_account_is_refused(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_google_push_service_account", "")
    data = base64.b64encode(json.dumps({"emailAddress": "sam@seq.com"}).encode()).decode()
    response = await client.post("/api/engagement/webhooks/gmail",
                                 headers={"Authorization": "Bearer x"},
                                 json={"message": {"data": data}})
    assert response.status_code == 401


# ---- ingestion end to end ----------------------------------------------------------------------------

async def _sent_first(slug, monkeypatch, box):
    """A launched one-contact campaign whose first email has gone out through `box`."""
    tid, _campaign = await _launched(slug, monkeypatch)
    enrollment = await _enrollment(tid)
    await _run(tid, enrollment.id, enrollment.next_action_at + timedelta(seconds=1))
    from email import message_from_bytes

    first = message_from_bytes(box.delivered[0])
    return tid, await _enrollment(tid), first["Message-ID"]


async def _sync(tid, mailbox_id, when=NOW):
    from nexus.engagement.replies.ingest import sync_mailbox
    from nexus.models.engagement import MailboxConnection

    async with tenant_session(tid) as ts:
        return await sync_mailbox(ts, await ts.get(MailboxConnection, mailbox_id), now=when)


async def _classification(tid):
    from nexus.models.engagement import ReplyClassification

    async with tenant_session(tid) as ts:
        return await ts.first(ReplyClassification)


async def test_an_interested_reply_stops_the_sequence_and_alerts_the_owner(
    mailbox_double, monkeypatch,
):
    from nexus.models.alerts import Alert
    from nexus.models.engagement import EngagementMessage

    tid, enrollment, our_id = await _sent_first("replyint", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="Jane0 Buyer <jane0@acme.io>", in_reply_to=our_id,
                                body="Sounds interesting, let's talk next week."))
    result = await _sync(tid, enrollment.mailbox_connection_id)
    assert result == {"classified:interested": 1}
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("stopped", "replied")
    classification = await _classification(tid)
    assert classification.category == "interested" and classification.status == "open"
    async with tenant_session(tid) as ts:
        stored = await ts.first(EngagementMessage, EngagementMessage.direction == "in")
        alerts = await ts.list(Alert)
    assert stored.inbound_kind == "human" and stored.thread_id == enrollment.current_thread_id
    assert any(a.meta.get("category") == "reply_interested" for a in alerts)
    # The same notification again is harmless: the message is already stored.
    assert await _sync(tid, enrollment.mailbox_connection_id) == {}


async def test_a_dated_later_snoozes_until_that_day_in_the_contacts_morning(
    mailbox_double, monkeypatch,
):
    tid, enrollment, our_id = await _sent_first("replylater", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Not now — try me in June please."))
    await _sync(tid, enrollment.mailbox_connection_id)
    enrollment = await _enrollment(tid)
    assert enrollment.status == "snoozed"
    local = enrollment.snoozed_until.astimezone(__import__("zoneinfo").ZoneInfo(
        enrollment.contact_timezone))
    assert (local.year, local.month, local.hour) == (2027, 6, 9)


async def test_a_refusal_with_a_date_goes_to_the_sdr_and_nothing_sends(mailbox_double,
                                                                        monkeypatch):
    tid, enrollment, our_id = await _sent_first("replymixed", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Not interested. Maybe reach out next year."))
    await _sync(tid, enrollment.mailbox_connection_id)
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("paused", "needs_decision")
    assert (await _classification(tid)).category == "unclear"


async def test_unsubscribe_is_permanent_and_blocks_every_future_send(mailbox_double, monkeypatch):
    from nexus.engagement.suppression.service import active_block

    tid, enrollment, our_id = await _sent_first("replyunsub", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                body="Please remove me from your list."))
    await _sync(tid, enrollment.mailbox_connection_id)
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("stopped", "unsubscribed")
    async with tenant_session(tid) as ts:
        block = await active_block(ts, "jane0@acme.io")
    assert block is not None and block.reason == "unsubscribed"


async def test_out_of_office_pauses_then_resumes_the_same_step_when_they_are_back(
    mailbox_double, monkeypatch,
):
    tid, enrollment, our_id = await _sent_first("replyooo", monkeypatch, mailbox_double)
    step_before = enrollment.current_step_index
    mailbox_double.arrive(_mail(sender="jane0@acme.io", in_reply_to=our_id,
                                subject="Automatic reply: Quick question",
                                headers={"Auto-Submitted": "auto-replied"},
                                body="I am out of the office until Friday 2 October."))
    await _sync(tid, enrollment.mailbox_connection_id)
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("paused", "out_of_office")
    assert enrollment.snoozed_until.date() == date(2026, 10, 5)
    # Before they are back: nothing. After: the same step is sent, with no extra email.
    assert await _run(tid, enrollment.id, enrollment.snoozed_until - timedelta(hours=1)) \
        == "campaign_not_active" or (await _enrollment(tid)).status == "paused"
    outcome = await _run(tid, enrollment.id, enrollment.snoozed_until + timedelta(minutes=1))
    assert outcome == "sent"
    enrollment = await _enrollment(tid)
    assert enrollment.current_step_index == step_before + 1


async def test_a_bounce_marks_our_message_stops_the_sequence_and_blocks_the_address(
    mailbox_double, monkeypatch,
):
    from nexus.engagement.suppression.service import active_block
    from nexus.models.engagement import EngagementMessage

    tid, enrollment, _our_id = await _sent_first("replybounce", monkeypatch, mailbox_double)
    mailbox_double.arrive(_bounce(mailbox_double.delivered[0], "jane0@acme.io"))
    assert await _sync(tid, enrollment.mailbox_connection_id) == {"bounced": 1}
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("stopped", "bounced")
    async with tenant_session(tid) as ts:
        ours = await ts.first(EngagementMessage, EngagementMessage.direction == "out")
        assert ours.status == "bounced"
        assert (await active_block(ts, "jane0@acme.io")).reason == "bounced"
        # The bounce itself is not stored: it is about our email, not a conversation.
        assert await ts.first(EngagementMessage, EngagementMessage.direction == "in") is None


async def test_mail_from_a_stranger_is_never_stored(mailbox_double, monkeypatch):
    from nexus.models.engagement import EngagementMessage

    tid, enrollment, _our_id = await _sent_first("replyprivacy", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="friend@example.org", subject="Dinner?",
                                body="Are we still on for Friday?"), thread_id="personal-9")
    assert await _sync(tid, enrollment.mailbox_connection_id) == {"discarded": 1}
    async with tenant_session(tid) as ts:
        assert await ts.first(EngagementMessage, EngagementMessage.direction == "in") is None


async def test_a_colleague_writing_pauses_everyone_at_the_company_as_a_referral(
    mailbox_double, monkeypatch,
):
    tid, enrollment, _our_id = await _sent_first("replycolleague", monkeypatch, mailbox_double)
    mailbox_double.arrive(_mail(sender="Pat Other <pat@acme.io>", subject="Re: Quick question",
                                body="Jane forwarded this — I own this area."),
                          thread_id="thread-99")
    assert await _sync(tid, enrollment.mailbox_connection_id) == {"classified:referral": 1}
    enrollment = await _enrollment(tid)
    assert (enrollment.status, enrollment.status_reason) == ("paused", "colleague_replied")


def test_every_reply_category_has_a_readable_label_and_a_rule():
    from nexus.alerts.rules import ALERT_CATEGORIES, engagement_rule

    for category in ("reply_interested", "reply_needs_decision", "reply_bounced",
                     "mailbox_needs_reauth"):
        assert category in ALERT_CATEGORIES
        assert engagement_rule(category)[1]
```

RUN_RESULT:

```
20 passed in 64.53s     # tests/test_engagement_replies.py
177 passed in 331.68s   # replies, sequences, sending, alerts, billing catalog, scheduler
All checks passed!      # ruff check nexus tests tests_live
tsc --noEmit            # clean
```

---

## What phase 10 depends on

- Every human reply leaves a `ReplyClassification` with `status="open"`, `assigned_user_id` = the mailbox owner and the category; `unclear` ones are the SDR's decisions.
- Colleagues paused `colleague_replied` never resume on their own; the reply desk resumes or stops each.
- `reply.received` and `reply.classified` carry `answered_message_id`, which labels the outreach example in the training ledger (phase 06).
