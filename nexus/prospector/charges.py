"""Spending credits for a Prospector delivery: ask first, charge once, remember what was bought.

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
