# nexus/billing/tokens.py
"""Record LLM token consumption against ``ai.tokens``.

Priced at 0.01 credits per 1,000 tokens and metered at no call site until now: the hook existed and
nothing used it, which is the same shape of gap as a rate card nobody spends.

**This runs alongside the flat per-action charge, not instead of it.** The flat rate is what the
customer pays and what makes a bill predictable; this is the measurement that lets the flat rate be
*checked* rather than assumed. Measured spread within a single agent is about 4x median-to-max,
which these margins absorb comfortably — but that is a fact about today's prompts, and prompts
change. Without this, the first sign that a flat rate had stopped covering its capability would be
the margin report, months later.

Never raises. It is bookkeeping attached to work the customer is already waiting on, and losing the
measurement is much cheaper than losing the run.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.billing.tokens")

CAPABILITY = "ai.tokens"
UNIT_TOKENS = 1000.0


async def meter_tokens(ts, *, tokens: int, agent: str = "") -> bool:
    """Record ``tokens`` against the tenant.

    Returns whether metering was **attempted** — not whether a row landed. ``record_usage`` one
    layer down is already defensive and swallows its own write failures, so this cannot see them.
    False therefore means "there was nothing to record", which is the distinction a caller can act
    on; a failed write is the usage layer's business and is logged there.
    """
    if ts is None or not tokens or tokens <= 0:
        # Nothing consumed, nothing recorded. A zero-quantity event is noise in the usage stream,
        # and it is the same rule that keeps an unconfigured phone lookup off the bill.
        return False
    try:
        from nexus.billing.meter import metered

        async with metered(
            ts,
            CAPABILITY,
            quantity=tokens / UNIT_TOKENS,
            # The agent, so a margin review can attribute consumption to a prompt rather than to
            # "the LLM" — which is what makes the number actionable.
            attrs={"agent": agent, "tokens": int(tokens)},
        ):
            return True
    except Exception:
        logger.debug("token metering skipped for %s", agent, exc_info=True)
        return False
