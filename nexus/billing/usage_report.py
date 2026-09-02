"""Where a workspace's credits went.

`/billing/usage` reported per-capability ACTION COUNTS and nothing about credits — `enrich.account:
used 40` is really 120 credits at 3 per action, and nothing on the screen said so. A customer
watching a balance fall had no way to find out what was spending it, which is the one question a
credit-funded plan guarantees they will ask.

Built from the CREDIT LEDGER rather than the usage stream, and that choice is load-bearing. The
ledger is where the money actually moved: it holds the exact amount deducted, keyed by capability
and idempotency key. Recomputing spend by multiplying usage events against today's rate cards would
produce a different number the moment a price changed, and a report that disagrees with the balance
is worse than no report — it invites a support ticket rather than answering one.

Three views, because they answer three different questions:

* **by capability** — "what is eating my balance?", sorted by spend
* **by day** — "why did it drop on Tuesday?"
* **by user** — "who is spending it?", with the unattributable remainder shown as its own line
"""
from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import select

from nexus.core.tenancy import TenantSession
from nexus.models.billing import BillingCreditLedger, BillingUsageEvent

logger = logging.getLogger("nexus.billing.usage_report")

# Days of history the timeline covers. A billing period is a month, and a chart longer than that
# spans a period boundary where the balance was reset — two different stories on one axis.
TIMELINE_DAYS = 31


async def credit_usage_report(ts: TenantSession, *, period_key: str | None = None) -> dict:
    """Credit spend for the current period, broken down three ways. Never raises.

    A failure returns an empty report rather than an error: this is a read-only panel, and a
    customer who cannot see their usage should not also be unable to load their billing page.
    """
    try:
        return await _build(ts, period_key)
    except Exception:
        logger.warning("credit usage report failed for %s", ts.tenant_id, exc_info=True)
        return {
            "period": period_key or "",
            "granted": 0.0, "spent": 0.0, "balance": 0.0,
            "by_capability": [], "by_day": [], "by_user": [],
            "unattributed_credits": 0.0,
        }


async def _build(ts: TenantSession, period_key: str | None) -> dict:
    from nexus.billing.rollups import period_key as make_period_key
    from nexus.core.db import utcnow

    period = period_key or make_period_key(utcnow(), "period")

    # A NULL `period_key` is INCLUDED, then attributed by its own `created_at`.
    #
    # `grant_credits` takes `period_key` as an optional argument, and the admin goodwill grant —
    # support's most common action — does not pass it. Filtering strictly on the column therefore
    # dropped those rows from the report while they still raised the balance: the screen read
    # "granted 2,000, spent 293" beside a balance 500 higher than either number could explain,
    # right after support told the customer the credits were there. That is the exact
    # reconciliation failure this report exists to prevent.
    #
    # Attributed by date rather than swept into whatever period is open, or last quarter's
    # adjustment would inflate this month and the report would be wrong in a new direction. The
    # per-capability ACTION loop below already accepted `period_key in (None, period)`; this makes
    # the ledger read agree with it.
    from sqlalchemy import or_

    rows = [
        r
        for r in await ts.session.scalars(
            select(BillingCreditLedger)
            .where(
                BillingCreditLedger.tenant_id == ts.tenant_id,
                or_(
                    BillingCreditLedger.period_key == period,
                    BillingCreditLedger.period_key.is_(None),
                ),
            )
            .order_by(BillingCreditLedger.created_at.asc())
        )
        if r.period_key == period
        or (r.created_at is not None and make_period_key(r.created_at, "period") == period)
    ]

    # `delta` is signed: grants are positive, burns negative. Reported as positive magnitudes,
    # because "spent 120" reads correctly and "spent -120" invites a double negative in the UI.
    granted = sum(float(r.delta) for r in rows if float(r.delta) > 0)
    burns = [r for r in rows if float(r.delta) < 0]
    spent = sum(-float(r.delta) for r in burns)

    # ---- by capability -------------------------------------------------------------------------
    # Action counts come from the usage stream; credits come from the ledger. Two sources on one
    # row on purpose: the customer wants to know both "how many" and "how much", and deriving one
    # from the other would drift the moment a price changed.
    actions: dict[str, float] = defaultdict(float)
    for event in await ts.list(BillingUsageEvent):
        if getattr(event, "period_key", None) in (None, period):
            actions[event.capability_id] += float(event.quantity or 0)

    credits_by_cap: dict[str, float] = defaultdict(float)
    for r in burns:
        credits_by_cap[r.capability_id or "other"] += -float(r.delta)

    names = await _capability_names(ts, set(credits_by_cap))
    by_capability = [
        {
            "capability_id": cid,
            "name": names.get(cid, cid),
            "credits": round(amount, 4),
            "actions": round(actions.get(cid, 0.0), 4),
        }
        # Zero-spend rows are omitted: sixty lines of nothing bury the handful that matter, the
        # same reason the admin customer directory reports only what was actually used.
        for cid, amount in credits_by_cap.items() if amount > 0
    ]
    by_capability.sort(key=lambda r: (-r["credits"], r["capability_id"]))

    # ---- by day --------------------------------------------------------------------------------
    per_day: dict[str, float] = defaultdict(float)
    for r in burns:
        if r.created_at is not None:
            per_day[r.created_at.date().isoformat()] += -float(r.delta)
    by_day = [
        {"date": day, "credits": round(amount, 4)}
        for day, amount in sorted(per_day.items())
    ][-TIMELINE_DAYS:]

    # ---- by user -------------------------------------------------------------------------------
    # The ledger carries no user, so attribution comes from the usage events, matched by
    # capability. ATTRIBUTION IS PARTIAL BY CONSTRUCTION: background work — refresh sweeps, crawls,
    # plays — has nobody to attribute to, so the per-user rows cannot sum to the total. The
    # remainder is reported as its own number rather than dropped, because a screen whose parts do
    # not add up to the balance quietly lies about a figure the customer will check.
    user_share = await _user_share(ts, period)
    by_user: dict[str, float] = defaultdict(float)
    for cid, amount in credits_by_cap.items():
        shares = user_share.get(cid) or {}
        total_units = sum(shares.values())
        if not total_units:
            continue
        for user_id, units in shares.items():
            if user_id:
                by_user[user_id] += amount * (units / total_units)

    attributed = sum(by_user.values())
    by_user_rows = [
        {"user_id": uid, "credits": round(amount, 4)}
        for uid, amount in sorted(by_user.items(), key=lambda kv: -kv[1])
        if amount > 0
    ]

    # The LIVE balance, not `granted - spent`. Those two are period figures, and a balance carried
    # over from an earlier period (or a support grant made outside it) is real money the customer
    # can still spend. Deriving the balance from this period's rows alone would show a negative
    # number to anyone whose credits predate it — and the balance is the one figure on this screen
    # people check against what the product told them elsewhere.
    from nexus.billing.credits import balance as _live_balance

    return {
        "period": period,
        "granted": round(granted, 4),
        "spent": round(spent, 4),
        "balance": round(await _live_balance(ts), 4),
        "by_capability": by_capability,
        "by_day": by_day,
        "by_user": by_user_rows,
        "unattributed_credits": round(max(0.0, spent - attributed), 4),
    }


async def _capability_names(ts: TenantSession, ids: set[str]) -> dict[str, str]:
    """Human labels, so the panel does not show `ai.icp_from_website` to a customer."""
    if not ids:
        return {}
    from nexus.models.billing import BillingCapability

    rows = await ts.session.scalars(
        select(BillingCapability).where(BillingCapability.id.in_(ids))
    )
    return {c.id: c.name or c.id for c in rows}


async def _user_share(ts: TenantSession, period: str) -> dict[str, dict[str | None, float]]:
    """Units per capability per user, used to apportion each capability's credit spend.

    Apportioned rather than summed directly, because the ledger records money and the usage stream
    records who — and only the first is authoritative about the amount. Splitting the ledger figure
    by the usage stream's shape keeps the report reconciling with the balance whatever the rate card
    said at the time.
    """
    share: dict[str, dict[str | None, float]] = defaultdict(lambda: defaultdict(float))
    for event in await ts.list(BillingUsageEvent):
        if getattr(event, "period_key", None) not in (None, period):
            continue
        share[event.capability_id][getattr(event, "user_id", None)] += float(event.quantity or 0)
    return share
