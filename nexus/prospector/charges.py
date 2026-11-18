"""Spending credits for a Prospector delivery: ask first, charge once, remember what was bought.

* ``price`` and ``affordability`` are what the confirm dialog shows: the meter's own arithmetic and
  the billing engine's own yes or no, so the dialog cannot refuse what the delivery would allow.
* ``preflight_all`` asks the billing engine whether the WHOLE quote is affordable before anything is
  bought or written (the ``preflight`` rule: a 402 before work, never after).
* ``meter_all`` charges each capability once for its quantity, keyed on the request, so a retried
  request (same ``request_id``) re-derives the same charge and pays once.
* ``record_purchases`` writes the ledger (D6), so a later delivery of the same field is free even
  under a new request id, and even after the record was deleted from the workspace.

Both writes happen inside the caller's savepoint: a refused charge takes the records with it.
"""
from __future__ import annotations

from nexus.core.db import utcnow
from nexus.models.prospector import ProspectorPurchase
from nexus.prospector.pricing import Line

#: The capabilities a delivery can charge, named here so the metering coverage test sees them.
#: Pinned equal to `pricing.CAPABILITY` by `test_the_charged_set_is_the_pricing_set`.
CHARGED = ("prospector.email", "prospector.phone", "prospector.company")


async def rate_cards(ts) -> dict[str, float]:
    """Credits per unit for each charged capability, as Control plane has them now."""
    from sqlalchemy import select

    from nexus.models.billing import BillingRateCard

    rows = (await ts.session.scalars(select(BillingRateCard).where(
        BillingRateCard.capability_id.in_(CHARGED), BillingRateCard.active.is_(True)))).all()
    return {r.capability_id: float(r.credits_per_unit or 0) for r in rows}


async def price(ts, charges: dict[str, int]) -> dict[str, float]:
    """Credits each capability's quantity costs, computed exactly as the meter will charge it.

    The quote used to multiply the flat ``credits_per_unit``; the meter charges the MARGINAL cost
    on a tiered card (``entitlements._usage_amount``), so a volume ladder would have shown one
    number and charged another. Falls back to the flat card if resolution fails, never raises.
    """
    from nexus.billing.entitlements import _usage_amount, resolve_entitlement

    flat = await rate_cards(ts)
    out: dict[str, float] = {}
    for capability, quantity in sorted(charges.items()):
        if quantity <= 0:
            continue
        try:
            ent = await resolve_entitlement(ts, capability)
            amount = await _usage_amount(ts, ent, float(quantity))
        except Exception:
            amount = None
        out[capability] = float(amount) if amount is not None \
            else quantity * flat.get(capability, 0.0)
    return out


async def uses_credits(ts, charges: dict[str, int]) -> bool:
    """Whether this delivery would take credits at all.

    No for a plan that exists not to gate (``legacy-unlimited``, internal, partner), a workspace
    with no subscription, and billing switched off: the meter records nothing to burn for them.
    Measured on a copy of the local database, 2026-11-18: a superadmin's own workspace is
    ``legacy-unlimited`` with a ledger of 0, and the dialog read "Not enough credits, balance 0"
    and disabled Save, List, Export and Reveal for a delivery that costs it nothing.
    """
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.core.config import get_settings

    if get_settings().billing_enforcement == "off":
        return False
    for capability, quantity in charges.items():
        if quantity <= 0:
            continue
        try:
            ent = await resolve_entitlement(ts, capability)
        except Exception:
            return True
        if ent.plan_id is not None and ent.mode not in ("shadow", "unlimited"):
            return True
    return False


_REASONS = {
    "disabled": "Your plan does not include saving prospects. An admin can change the plan in "
                "Billing.",
    "dependency": "Your plan does not include Prospector. An admin can change the plan in "
                  "Billing.",
    "quota_exhausted": "Your plan's limit for this is used up for the period.",
    "throttled": "Too many requests in a short time. Try again in a minute.",
}


async def affordability(ts, charges: dict[str, int], *, total: float,
                        balance: float | None) -> tuple[bool, str | None]:
    """Would the delivery go through? The billing engine's own answer, with the reason.

    The confirm dialog used to compare the balance with the total itself, which refused
    deliveries the engine allows: a plan not funded by credits, a workspace with no subscription,
    and every workspace while enforcement is in shadow mode, where nothing is refused for money.
    It now asks ``preflight`` exactly as the delivery does.
    """
    from nexus.billing.entitlements import preflight

    for capability, quantity in sorted(charges.items()):
        if quantity <= 0:
            continue
        result = await preflight(ts, capability, quantity=quantity)
        if result.allowed:
            continue
        reason = result.reason or "quota_exhausted"
        if reason == "credits_exhausted":
            have = "" if balance is None else f" and the balance is {balance:g}"
            return False, (f"Not enough credits: this needs {total:g}{have}. Choose fewer rows, "
                           "or add credits in Billing.")
        if reason == "feature_switch":
            ent = result.entitlement
            message = getattr(ent, "switch_message", "") if ent is not None else ""
            return False, message or "Prospector is switched off right now."
        return False, _REASONS.get(reason, "This cannot be delivered on your plan.")
    return True, None


async def preflight_all(ts, charges: dict[str, int]) -> None:
    from nexus.billing.entitlements import preflight

    for capability, quantity in sorted(charges.items()):
        if quantity > 0:
            (await preflight(ts, capability, quantity=quantity)).raise_if_blocked()


async def meter_all(ts, charges: dict[str, int], *, request_id: str, user_id: str) -> None:
    from nexus.billing.meter import metered

    for capability, quantity in sorted(charges.items()):
        if quantity <= 0:
            continue
        async with metered(ts, capability, quantity=quantity, user_id=user_id,
                           idempotency_key=f"prospector:{request_id}:{capability}",
                           attrs={"request_id": request_id}):
            pass


async def record_purchases(ts, lines: list[Line], *, request_id: str,
                           links: dict[str, tuple[str | None, str | None]]) -> None:
    """One ledger row per (record, field) charged. ``links``: identity -> (contact_id, account_id)."""
    now = utcnow()
    for line in lines:
        contact_id, account_id = links.get(line.identity, (None, None))
        for field_name in line.fields:
            ts.add(ProspectorPurchase(
                tenant_id=ts.tenant_id, fingerprint=line.fingerprint, field=field_name,
                contact_id=contact_id, account_id=account_id, request_id=request_id[:64],
                first_charged_at=now, last_delivered_at=now))
    await ts.flush()
