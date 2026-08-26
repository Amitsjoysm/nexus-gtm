# nexus/billing/capability_authoring.py
"""Create a billable capability without a deploy.

``CAPABILITY_SEED`` in ``catalog.py`` is a Python list, and ``sync_catalog()`` inserts only what it
names. There was no other write path to ``billing_capabilities`` — ``PUT /rates/{id}`` and
``PUT /plans/{id}/entitlements/{cap}`` both 404 when the row is missing, so they can price and
entitle only what the seed already created. The table was always writable; the API was the missing
half, and that is why a new billable action meant a code change and a release.

Two properties of the seed make this safe to add rather than a race with the next deploy:

* ``sync_catalog`` **never deletes**. It upserts what the seed names and leaves everything else.
* ``_MANAGED_FIELDS`` re-asserts ``category``/``unit``/``depends_on`` from code, but only for ids
  the seed knows. An admin-created capability is invisible to it.

**Pricing is offered in the same call, and its absence is warned about.** A capability with no rate
card is metered and then rated at nothing: usage events accumulate, quotas count down, and no
revenue line ever appears. It looks handled. That shipped once — ``ai.scoring``, 4,090 runs, free.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.models.billing import BillingCapability, BillingCostRate, BillingRateCard

# ``category.name``. Every existing id uses it, the Admin UI groups on the prefix, and it is what
# appears in URLs, entitlement rows, usage events and invoice line items.
_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

VALID_METER_KINDS = ("counter", "gauge")
VALID_MODES = ("enabled", "metered", "shadow", "disabled", "enterprise")


class CapabilityError(ValueError):
    """The capability cannot be created as asked."""


class DuplicateCapability(CapabilityError):
    """That id already exists."""


async def create_capability(
    session: AsyncSession, *, capability_id: str, name: str, category: str,
    unit: str = "action", description: str = "", sub_category: str = "",
    meter_kind: str = "counter", default_mode: str = "metered",
    depends_on: list[str] | None = None,
    credits_per_unit: float | None = None, unit_cost_usd: float | None = None,
) -> dict:
    """Create the capability, and its rate card when a price is given."""
    capability_id = (capability_id or "").strip().lower()
    if not _ID_RE.match(capability_id):
        raise CapabilityError(
            f"'{capability_id}' is not a valid id - use lowercase category.name, "
            f"for example 'enrich.company_news'"
        )
    if meter_kind not in VALID_METER_KINDS:
        raise CapabilityError(f"meter_kind must be one of {VALID_METER_KINDS}")
    if default_mode not in VALID_MODES:
        raise CapabilityError(f"default_mode must be one of {VALID_MODES}")

    if await session.get(BillingCapability, capability_id) is not None:
        raise DuplicateCapability(f"'{capability_id}' already exists")

    deps = [d for d in (depends_on or []) if d]
    if deps:
        known = set(
            (
                await session.scalars(
                    select(BillingCapability.id).where(BillingCapability.id.in_(deps))
                )
            ).all()
        )
        missing = [d for d in deps if d not in known]
        if missing:
            # A dependency that does not exist gates this capability behind something that can
            # never resolve, so it can never be granted. Permanently unusable, and silently.
            raise CapabilityError(
                f"depends_on names capabilities that do not exist: {missing}"
            )

    session.add(
        BillingCapability(
            id=capability_id, name=name or capability_id, description=description,
            category=category or capability_id.split(".")[0], sub_category=sub_category,
            unit=unit, meter_kind=meter_kind, default_mode=default_mode,
            depends_on=deps, active=True,
        )
    )
    await session.flush()

    priced = False
    margin = 0.0
    warning = ""
    if credits_per_unit is not None:
        from nexus.billing.rates import validate_rate

        cost = float(unit_cost_usd or 0.0)
        # Raises MarginFloorError below the floor, which the caller maps to 422 exactly as the rate
        # endpoint does. There must be no path - seed, rate endpoint, or this one - that lands an
        # underwater price in the database.
        margin = validate_rate(
            capability_id, credits_per_unit=float(credits_per_unit), unit_cost_usd=cost,
        )
        session.add(
            BillingRateCard(
                capability_id=capability_id, credits_per_unit=float(credits_per_unit),
                active=True,
            )
        )
        session.add(BillingCostRate(capability_id=capability_id, unit_cost_usd=cost))
        await session.flush()
        priced = True
    elif not capability_id.startswith("module."):
        # Allowed but never silent. A module gate legitimately has no unit price; anything else
        # without one is the priced-nothing hole.
        warning = (
            f"'{capability_id}' has no rate card, so anything metered against it is rated at "
            f"nothing - usage will accumulate and no revenue line will ever appear. Add a price "
            f"on the Rate cards tab."
        )

    return {
        "capability_id": capability_id,
        "priced": priced,
        "gross_margin": round(margin, 4),
        "warning": warning,
    }
