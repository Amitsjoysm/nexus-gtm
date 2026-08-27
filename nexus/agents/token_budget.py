# nexus/agents/token_budget.py
"""Keep a completion inside the provider's per-request and per-minute token limits.

**Measured against the live Groq account on 2026-08-26**, because the failure was being read as a
context-window problem and it is not:

    17,000 chars -> 413  "Request too large ... on tokens per minute (TPM):
                          Limit 8000, Requested 8588, please reduce your message size"

So the binding constraint is **TPM, not context window**, and it produces two different statuses
from one limit:

* **413** when a SINGLE request exceeds the whole minute's budget. No amount of waiting fixes it —
  the request must get smaller.
* **429** when the rolling minute is exhausted by traffic. Waiting does fix it, and the response
  carries a ``Retry-After``.

Two more measurements shaped the design, and both contradict the obvious reading:

* **The five keys are five different organizations**, each with its own 8,000 TPM — verified by
  reading the org id out of four separate 429s. So rotation genuinely helps, and the total budget is
  about 40,000 TPM rather than 8,000.
* **One large prompt very nearly fills a whole org's minute.** Four concurrent ~7.5k-token requests
  on four different keys all 429'd, with ``Retry-After`` of 28-33 seconds. That is why rotating
  without waiting fails: it burns every key in seconds and then raises, and the caller falls through
  to the stub — whose output is emailed to real prospects.

The fix is therefore three things, and the first is the one that matters:

1. **Never send a request larger than the per-request ceiling.** This alone eliminates 413 and drops
   the cost of every call, so the minute budget stretches much further.
2. **Honour ``Retry-After``.** Rotate to another org first — they are separate budgets — and only
   then wait.
3. **Bound how many completions run at once**, because the limit is shared across every endpoint and
   `/enrich`, `/lookalikes` and `/source-contacts` fire together.
"""
from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger("nexus.agents.token_budget")

# English prose runs about four characters per token. Deliberately conservative: under-estimating
# tokens is what sends an over-sized request, and the whole point here is not to do that.
CHARS_PER_TOKEN = 3.5

# Groq `on_demand` measured at 8,000 TPM per organization. Leave headroom for the completion itself
# (`max_tokens`) plus the tokenizer disagreeing with the estimate above.
DEFAULT_TPM = 8000
HEADROOM = 0.75

# What ONE request may spend, as a fraction of the minute.
#
# Not `TPM * headroom`: that sizes a single request to fill the whole minute, which passes in
# isolation and then starves everything else. Measured on this account — a 6,000-token request
# succeeded on its own and left so little of the 8,000 TPM that the next two calls 429'd, waited
# out their retries and fell through to the stub.
#
# 30% means roughly three completions per minute per organization, which is what "consistent
# performance" actually requires. Raise it on a larger tier through the Control plane rather than
# here; the ceiling belongs to the account, not to the code.
REQUEST_SHARE_OF_TPM = 0.30
DEFAULT_MAX_PROMPT_TOKENS = int(DEFAULT_TPM * REQUEST_SHARE_OF_TPM)

# How many completions may be in flight across the whole process. The limit is per organization and
# shared by every endpoint, so unbounded concurrency exhausts it however small each request is.
DEFAULT_CONCURRENCY = 3

_TPM_RE = re.compile(r"Limit\s+(\d+)", re.I)


def estimate_tokens(text: str) -> int:
    """Conservative token estimate. Over-estimating costs a slightly shorter prompt; under-
    estimating costs a 413 and a fallback to the stub."""
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def parse_limit(detail: str) -> int | None:
    """The provider's own stated limit, out of a 413/429 body.

    Preferred over our constant wherever it appears: the account's tier can change under us, which
    is exactly how this bug arrived — a model swap moved the deployment onto a tier whose ceiling
    nobody had encoded anywhere.
    """
    m = _TPM_RE.search(detail or "")
    return int(m.group(1)) if m else None


def fit_messages(messages: list, max_prompt_tokens: int) -> tuple[list, bool]:
    """Trim message content so the whole prompt fits. Returns ``(messages, was_trimmed)``.

    **The system message is never trimmed**, and the LAST user message is trimmed last. The system
    message carries the output contract — the `Subject:` line the parser needs, the ban on inventing
    customer names and metrics — and dropping it produces confidently-wrong copy that goes to a real
    buyer. Losing the middle of a research blob costs detail; losing the contract costs correctness.

    Trimming keeps the head and tail of an over-long body with a marker between them, because the
    beginning states what the thing is and the end usually carries the most recent facts.
    """
    total = sum(estimate_tokens(getattr(m, "content", "")) for m in messages)
    if total <= max_prompt_tokens:
        return messages, False

    # Everything the system messages need, taken off the top before anything else is allocated.
    system_tokens = sum(
        estimate_tokens(getattr(m, "content", ""))
        for m in messages if getattr(m, "role", "") == "system"
    )
    budget = max(256, max_prompt_tokens - system_tokens)

    others = [m for m in messages if getattr(m, "role", "") != "system"]
    if not others:
        return messages, False

    # Spend the budget from the LAST message backwards: the most recent turn is the one being
    # answered, so it keeps the most room.
    per_message: dict[int, int] = {}
    remaining = budget
    for idx in range(len(others) - 1, -1, -1):
        want = estimate_tokens(getattr(others[idx], "content", ""))
        give = min(want, max(128, remaining))
        per_message[idx] = give
        remaining = max(0, remaining - give)

    trimmed_any = False
    out = []
    other_idx = 0
    for m in messages:
        if getattr(m, "role", "") == "system":
            out.append(m)
            continue
        allowed = per_message.get(other_idx, 128)
        other_idx += 1
        content = getattr(m, "content", "") or ""
        if estimate_tokens(content) <= allowed:
            out.append(m)
            continue
        # The marker costs tokens too. Budgeting the body first and adding the marker afterwards
        # overshoots by the marker's own length — which defeats the entire purpose, because the
        # request that comes back 413 is the one that was supposed to have been made to fit.
        marker = (
            f"\n\n[... {len(content):,} characters trimmed to fit the model's token limit ...]\n\n"
        )
        # `allowed - 1` because `estimate_tokens` adds a whole token of safety margin to every
        # string. Sizing to `allowed` and then measuring lands one token over, and a ceiling that
        # is exceeded by one is not a ceiling — the provider counts differently from us, which is
        # the entire reason for the margin in the first place.
        keep = int((allowed - 1) * CHARS_PER_TOKEN) - len(marker)
        if keep < 64:
            # No room for both. The marker is what tells the reader something was removed, so at
            # this size the honest answer is the marker on its own.
            m.content = marker
            out.append(m)
            trimmed_any = True
            continue
        head = keep * 2 // 3
        tail = keep - head
        m.content = content[:head] + marker + (content[-tail:] if tail > 0 else "")
        out.append(m)
        trimmed_any = True

    return out, trimmed_any


class CompletionGate:
    """Bounds how many completions are in flight process-wide.

    Not per-caller: the token budget belongs to the provider account and is shared by every
    endpoint. `/enrich`, `/lookalikes`, `/source-contacts` and the refresh pipeline all fired within
    the same second in the reported failure, and each holding its own limiter would have permitted
    exactly that.
    """

    def __init__(self, limit: int = DEFAULT_CONCURRENCY) -> None:
        self._limit = max(1, limit)
        self._sem = asyncio.Semaphore(self._limit)

    def resize(self, limit: int) -> None:
        """Adopt a new limit. Takes effect for calls that have not yet acquired."""
        limit = max(1, int(limit))
        if limit != self._limit:
            self._limit, self._sem = limit, asyncio.Semaphore(limit)

    def __call__(self):
        return self._sem


# One gate for the process. The worker and each API replica have their own, which is correct: the
# provider limit is per organization, and bounding each process is what keeps their combined
# request rate survivable without any of them coordinating.
GATE = CompletionGate()
