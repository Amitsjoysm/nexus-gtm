# nexus/billing/plan_changes.py
"""Which way a plan change goes, and which plan a Stripe price bills (owner decisions 2026-10-22).

Hybrid plan changes for a workspace paying through Stripe:

* an UPGRADE stays in the app (`POST /billing/switch-plan`): charged in full now, applied only if
  paid, the new plan's credits at once;
* a DOWNGRADE goes to Stripe's billing portal (`POST /billing/downgrade`) and takes effect at the
  end of the period already paid for. Stripe holds it on a subscription schedule, and the plan here
  moves when the billed price does. A move to Free is the portal's cancel flow;
* an upgrade a customer makes inside the portal moves the plan at once and earns PRORATED credits
  (the new plan's extra credits times the part of the period left), granted only when Stripe's
  proration invoice is paid.

The portal changes the PRICE and leaves the subscription's ``metadata.plan_id`` as it was, so the
plan a subscription is on is read from the price it bills (`plan_for_price`), falling back to the
metadata only for a price NEXUS did not make.
"""
from __future__ import annotations

from datetime import datetime


def is_downgrade(current, target) -> bool:
    """A downgrade: no charge at all (Free), the same interval at a lower price, or yearly to
    monthly billing. Everything else, monthly to yearly included, is an upgrade.

    The same two conditions the downgrade portal configuration hands to Stripe
    (``decreasing_item_amount`` and ``shortening_interval``), so what the app calls a downgrade is
    exactly what Stripe holds until the period ends."""
    if current is None:
        return False
    price = int(target.base_price_cents or 0)
    if price <= 0:
        return True
    if current.interval == "year" and target.interval == "month":
        return True
    return target.interval == current.interval and price < int(current.base_price_cents or 0)


def _id(value) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or "")
    return str(value or "")


def subscription_price_id(obj: dict) -> str:
    """The price a Stripe subscription bills: its only item's price."""
    items = obj.get("items")
    rows = items.get("data") if isinstance(items, dict) else None
    first = rows[0] if rows and isinstance(rows[0], dict) else {}
    return _id(first.get("price"))


def invoice_price_id(obj: dict) -> str:
    """The price on an invoice's first line, in either shape Stripe renders it: ``price`` up to
    2025-03-31 (the version NEXUS pins), ``pricing.price_details.price`` from then on (an endpoint
    made by hand in the Dashboard takes the account's default version)."""
    lines = obj.get("lines")
    rows = lines.get("data") if isinstance(lines, dict) else None
    first = rows[0] if rows and isinstance(rows[0], dict) else {}
    price = _id(first.get("price"))
    if price:
        return price
    pricing = first.get("pricing")
    details = pricing.get("price_details") if isinstance(pricing, dict) else None
    return _id(details.get("price")) if isinstance(details, dict) else ""


async def plans_by_price(session) -> dict[str, str]:
    """Every Stripe price NEXUS made, current or superseded, mapped to its plan.

    A price superseded by a reprice or an account switch keeps billing the subscriptions already on
    it, so ``previous_stripe`` counts as much as ``price_id``; a current price wins a tie."""
    from sqlalchemy import select

    from nexus.models.billing import BillingPlan

    current: dict[str, str] = {}
    previous: dict[str, str] = {}
    for plan in (await session.scalars(select(BillingPlan))).all():
        meta = plan.meta or {}
        if meta.get("price_id"):
            current[str(meta["price_id"])] = plan.id
        for old in meta.get("previous_stripe") or []:
            if isinstance(old, dict) and old.get("price_id"):
                previous.setdefault(str(old["price_id"]), plan.id)
    return {**previous, **current}


async def plan_for_price(session, price_id: str):
    """The plan ``price_id`` bills, or None when NEXUS did not make that price."""
    from nexus.models.billing import BillingPlan

    if not price_id:
        return None
    plan_id = (await plans_by_price(session)).get(price_id)
    return await session.get(BillingPlan, plan_id) if plan_id else None


#: Schedule statuses that can still change a subscription.
_LIVE_SCHEDULE = ("active", "not_started")


def next_scheduled_phase(schedule: dict, now: int) -> dict | None:
    """The phase of a subscription schedule that has yet to start, or None.

    The portal holds a downgrade as a schedule whose second phase starts at period end. Once that
    phase is running the schedule stays attached until it ends, with nothing left to change.

    Read from the schedule's own ``current_phase`` when it has one: Stripe's clock, not ours. Under
    a test clock the two are a year apart, and comparing with ours picked the running phase as
    "next" (seen 2026-10-22). ``now`` is the fallback for a schedule that names none."""
    if not isinstance(schedule, dict) or str(schedule.get("status") or "") not in _LIVE_SCHEDULE:
        return None
    current = schedule.get("current_phase")
    if isinstance(current, dict) and current.get("end_date"):
        try:
            now = int(current["end_date"]) - 1
        except (TypeError, ValueError):
            pass
    for phase in schedule.get("phases") or []:
        try:
            starts = int((phase or {}).get("start_date") or 0)
        except (TypeError, ValueError):
            continue
        if starts > now:
            return phase
    return None


def phase_price_id(phase: dict) -> str:
    items = (phase or {}).get("items") or []
    return _id(items[0].get("price")) if items and isinstance(items[0], dict) else ""


def remaining_fraction(start: datetime | None, end: datetime | None, at: datetime) -> float:
    """How much of the period [start, end) is left at ``at``, between 0 and 1."""
    from datetime import timezone

    if start is None or end is None:
        return 0.0
    # SQLite hands timestamps back without a zone; they were written in UTC.
    start, end, at = (v if v.tzinfo else v.replace(tzinfo=timezone.utc) for v in (start, end, at))
    total = (end - start).total_seconds()
    if total <= 0:
        return 0.0
    return min(1.0, max(0.0, (end - at).total_seconds() / total))


def prorated_upgrade_credits(old, new, fraction: float) -> int:
    """The new plan's extra credits for the part of the period left, rounded down."""
    extra = int(new.included_credits or 0) - int((old.included_credits if old else 0) or 0)
    return max(0, int(extra * fraction))
