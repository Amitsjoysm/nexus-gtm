# nexus/ingestion/tiering.py
"""How often an account is worth re-crawling.

Every account was refreshed on the same 6h cycle. Measured, that is what makes the pipeline
unaffordable: 500 tenants x 1000 accounts on a 6h cycle is **23.15 accounts/sec**, against a
measured drain rate of **0.036 accounts/sec** on one serial worker — a 640x gap. Most of that
demand is spent re-crawling accounts where nothing has happened for months.

Tiering is the only lever that attacks the demand side rather than the supply side. At a realistic
hot ratio it brings the target down to roughly 3-5/sec, which the throughput work can actually
reach.

**The bias is deliberately toward hot.** Every rule here is a reason to keep crawling; the cold
tier is only what is left when none of them fire. Being wrong in the hot direction costs a crawl;
being wrong in the cold direction means a rep learns about a funding round three days late, which
is the failure this whole product exists to prevent. That is the same asymmetry as
`_NEGATION_CUES` in the signal classifier: missing a real event costs more than an extra look.

Deterministic, no LLM, no scoring — the same rule as the Relevance Engine. An account's refresh
cadence must be explainable to the rep who asks why their account looks stale.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from nexus.core.config import get_settings
from nexus.core.db import utcnow

logger = logging.getLogger("nexus.ingestion.tiering")

HOT = "hot"
COLD = "cold"


async def _has_recent_signal(ts, account_id: str, since: datetime) -> bool:
    from nexus.models.signal import SignalEvent

    return await ts.first(
        SignalEvent,
        SignalEvent.account_id == account_id,
        SignalEvent.occurred_at >= since,
    ) is not None


async def _in_active_cadence(ts, account_id: str) -> bool:
    from nexus.models.cadence import CadenceEnrollment, ENROLL_ACTIVE

    return await ts.first(
        CadenceEnrollment,
        CadenceEnrollment.account_id == account_id,
        CadenceEnrollment.status == ENROLL_ACTIVE,
    ) is not None


async def _on_a_list(ts, account_id: str) -> bool:
    from nexus.models.workflow import ListItem

    return await ts.first(ListItem, ListItem.account_id == account_id) is not None


async def classify(ts, account, *, new_signals: list | None = None) -> str:
    """`hot` or `cold` for this account, right now.

    Ordered cheapest-first, and short-circuits. The common case for an active account — the crawl
    just found something — costs **zero queries**, because a signal in hand is the strongest
    possible evidence that this account is worth watching.

    Never raises: a classification failure returns `hot`, which is the pre-tiering behaviour. A
    bookkeeping problem must not be able to quietly stop crawling an account.
    """
    try:
        if new_signals:
            return HOT  # something is happening right now

        settings = get_settings()
        since = utcnow() - timedelta(days=settings.account_hot_signal_window_days)
        if await _has_recent_signal(ts, account.id, since):
            return HOT
        # A rep is actively working this account. Whatever the signal history says, an account
        # someone is emailing today must not go on a three-day crawl cycle.
        if await _in_active_cadence(ts, account.id):
            return HOT
        # Somebody deliberately put it on a list. That is an explicit statement of interest and is
        # the cheapest possible signal of intent to watch.
        if await _on_a_list(ts, account.id):
            return HOT
        return COLD
    except Exception:
        logger.warning("refresh tiering failed for %s; treating as hot", account.id, exc_info=True)
        return HOT


def interval_for(tier: str) -> int:
    settings = get_settings()
    return (
        settings.account_refresh_interval_s
        if tier == HOT
        else settings.account_refresh_interval_cold_s
    )


def next_refresh_from(tier: str, *, now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(seconds=interval_for(tier))


async def schedule_next_refresh(ts, account, *, new_signals: list | None = None) -> str:
    """Classify and stamp `account.next_refresh_at`. Returns the tier, for the caller's result dict.

    Called at the END of the pipeline, when the crawl's outcome is known. The claim has already
    stamped a conservative hot-interval default, so an account whose processing dies part-way still
    comes back on the old 6h cycle rather than stalling — the tier can only ever push it further
    out from a schedule that already exists.
    """
    tier = await classify(ts, account, new_signals=new_signals)
    account.next_refresh_at = next_refresh_from(tier)
    return tier
