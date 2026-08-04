# nexus/billing/lifecycle.py
"""Subscription lifecycle: proration, trial expiry, pause and resume.

Three gaps, all of which cost or lose money quietly.

**Proration.** A mid-cycle plan change charged the new plan's full base fee and credited nothing for
the days already paid on the old one. Upgrading on the 28th of a 30-day month billed a full month
for two days of service. Nobody notices until a customer reads their invoice, and then it is a
refund conversation and a trust problem rather than a bug report.

**Trial expiry.** A trial had no automatic transition. It sat in ``trialing`` forever, which is a
live subscription contributing zero revenue — the platform gave the product away to anyone who
signed up and never converted them.

**Pause.** ``suspended`` was already in ``SUBSCRIPTION_STATUSES`` and nothing ever set it or acted
on it. A customer asking to pause had to be cancelled, losing their history and their plan terms.

Everything here is deterministic and day-weighted. Money stays in integer cents and is **never**
computed in floating point: a fraction of a cent per proration compounds into an invoice that does
not reconcile, and "off by one cent" is indistinguishable from fraud to an auditor.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("nexus.billing.lifecycle")


@dataclass(slots=True)
class Proration:
    """The two lines a mid-cycle plan change produces.

    Both are reported even when one is zero, because an invoice that shows only the charge looks
    like the customer was billed twice for the same month.
    """

    credit_cents: int = 0      # unused days on the plan being left
    charge_cents: int = 0      # remaining days on the plan being joined
    days_remaining: int = 0
    days_in_period: int = 0

    @property
    def net_cents(self) -> int:
        """Positive means the customer owes; negative means they are owed."""
        return self.charge_cents - self.credit_cents


def _days(period_start: datetime, period_end: datetime) -> int:
    return max(1, (period_end.date() - period_start.date()).days)


def prorate(
    *,
    old_monthly_cents: int,
    new_monthly_cents: int,
    period_start: datetime,
    period_end: datetime,
    at: datetime,
) -> Proration:
    """Day-weighted proration for a plan change part-way through a period.

    Rounding is **toward the customer**: the credit rounds up and the charge rounds down. Any
    rounding rule loses a fraction of a cent somewhere, and choosing to lose it in the customer's
    favour makes the error a rounding policy rather than an overcharge. Across a month of changes
    it is pennies; the alternative is an invoice a customer can dispute and be right about.

    A change at or after the period end prorates nothing — there are no remaining days to weight.
    """
    total_days = _days(period_start, period_end)
    remaining = max(0, (period_end.date() - at.date()).days)
    remaining = min(remaining, total_days)

    if remaining <= 0:
        return Proration(days_remaining=0, days_in_period=total_days)

    # Credit rounds UP (ceiling division), charge rounds DOWN — both in the customer's favour.
    credit = -((-int(old_monthly_cents) * remaining) // total_days) if old_monthly_cents else 0
    charge = (int(new_monthly_cents) * remaining) // total_days if new_monthly_cents else 0
    return Proration(
        credit_cents=credit,
        charge_cents=charge,
        days_remaining=remaining,
        days_in_period=total_days,
    )


def trial_expiry(started_at: datetime, trial_days: int) -> datetime | None:
    """When a trial ends, or None when the plan has no trial."""
    if not trial_days or trial_days <= 0:
        return None
    return started_at + timedelta(days=int(trial_days))


def trial_verdict(
    *, status: str, started_at: datetime, trial_days: int, now: datetime,
    has_payment_method: bool,
) -> str:
    """What an expired trial should become: ``trialing`` (unchanged), ``active`` or ``canceled``.

    A trial that expires **with** a payment method converts; without one it is cancelled rather than
    left live. Leaving it live is the failure that gives the product away indefinitely, and flipping
    it to ``active`` without a way to charge would manufacture receivables that can never be
    collected and pollute MRR with revenue that does not exist.
    """
    if status != "trialing":
        return status
    ends = trial_expiry(started_at, trial_days)
    if ends is None or now < ends:
        return "trialing"
    return "active" if has_payment_method else "canceled"


# Statuses from which a pause is meaningful. Pausing a cancelled subscription is a no-op, and
# pausing one that is past due would hide a debt behind a status the dunning sweep ignores.
PAUSABLE = ("active", "trialing")


def can_pause(status: str) -> tuple[bool, str]:
    """Whether this subscription may be paused, and why not."""
    if status in PAUSABLE:
        return True, ""
    if status == "past_due":
        # Refusing on purpose: pause is not a way to make an unpaid balance stop being chased.
        return False, "settle the outstanding balance before pausing"
    if status == "suspended":
        return False, "already paused"
    return False, f"cannot pause a {status} subscription"


def can_resume(status: str) -> tuple[bool, str]:
    if status == "suspended":
        return True, ""
    return False, f"cannot resume a {status} subscription"


def paused_extension(paused_at: datetime, resumed_at: datetime) -> timedelta:
    """How much to push the period end out by, so a customer is not billed for paused days.

    Without this, pausing for two weeks silently shortens the paid month — the customer pays for
    thirty days and receives sixteen, which is the same overcharge proration exists to prevent.
    """
    return max(timedelta(0), resumed_at - paused_at)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def run_trial_sweep(ts, *, now: datetime | None = None) -> dict:
    """Transition this tenant's expired trials. Returns what it did.

    Only ``trialing`` rows with an explicit ``trial_end`` are considered. An open-ended trial is a
    deliberate commercial arrangement — a design partner, a pilot — and sweeping it would cancel a
    customer somebody promised something to.

    "Has a payment method" is read as **a live provider subscription**: Checkout only produces one
    after a card is accepted, so its presence is evidence we can actually charge. Inferring it from
    a customer id would convert trials we cannot bill.
    """
    from nexus.models.billing import BillingSubscription

    now = now or utc_now()
    result = {"examined": 0, "converted": 0, "cancelled": 0}

    for sub in await ts.list(BillingSubscription, BillingSubscription.status == "trialing"):
        ends = sub.trial_end
        if ends is None:
            continue
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=timezone.utc)
        result["examined"] += 1
        if now < ends:
            continue

        if sub.psp_subscription_id:
            sub.status = "active"
            result["converted"] += 1
        else:
            sub.status = "canceled"
            sub.cancel_at_period_end = False
            result["cancelled"] += 1
        sub.meta = {
            **(sub.meta or {}),
            "trial_resolved_at": now.isoformat(),
            "trial_outcome": sub.status,
        }
        logger.info("trial for tenant %s resolved to %s", ts.tenant_id, sub.status)

    if result["converted"] or result["cancelled"]:
        await ts.flush()
    return result

