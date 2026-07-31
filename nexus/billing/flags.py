# nexus/billing/flags.py
"""Feature-flag evaluation for entitlements.

``BillingPlanEntitlement.feature_flag`` has existed since the schema was written, is editable
through the admin API, is copied by the custom-plan builder — and was **never read**. Exactly the
dead-config class that ``burst_limit`` and ``depends_on`` were in before they were wired up: a
setting an operator can change, that changes nothing, which is worse than an absent setting because
it invites someone to rely on it.

**Resolution order, narrowest first**, because a flag exists to override:

1. ``tenant`` — an override for one workspace. Beta access, a temporary disable during an incident.
2. ``environment`` — off in prod, on in staging. The normal shape of a staged rollout.
3. ``default`` — the flag's own default when nobody has said otherwise.

**An unknown flag is ON.** A plan entitlement naming a flag that has never been created must not
silently disable a capability the customer is paying for. The same bias runs through the whole
billing engine: unknown capability → allow, no subscription → allow, engine error → allow. A flag is
a switch for turning things *off* deliberately, so the absence of a decision is not a decision to
deny.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.billing.flags")


async def flag_enabled(ts, flag: str | None, *, env: str = "") -> bool:
    """Whether ``flag`` permits the capability for this tenant.

    Never raises: an evaluation failure resolves to enabled, for the reason in the module docstring.
    A flag that fails closed on a database blip would disable a paid feature during an incident.
    """
    name = (flag or "").strip()
    if not name:
        return True                     # no flag on this entitlement — nothing to evaluate
    try:
        from nexus.models.billing import BillingFeatureFlag

        row = await ts.session.get(BillingFeatureFlag, name)
        if row is None:
            # Named but never created. Enabled — see the module docstring.
            return True
        overrides = row.overrides or {}
        tenant_value = overrides.get(f"tenant:{ts.tenant_id}")
        if tenant_value is not None:
            return bool(tenant_value)
        if env:
            env_value = overrides.get(f"env:{env}")
            if env_value is not None:
                return bool(env_value)
        return bool(row.enabled)
    except Exception:
        logger.warning("feature flag %r evaluation failed; treating as enabled", name,
                       exc_info=True)
        return True
