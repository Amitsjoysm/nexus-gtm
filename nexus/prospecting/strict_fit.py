"""Strict fit: every discovered account must clear the fit threshold (2026-10-15).

LinkedIn and stored candidates are delivered without the fit threshold, as ICP matches by
construction (`deliver.py`). A workspace whose plan includes ``discovery.strict_fit`` may ask that
they clear it too. Measured on the local deploy, only 15-27% of what discovery delivered scored 70
or more (median 54-55), so this trades most of the daily volume for precision, which is why it is a
premium choice and off by default.

Decided with the product owner: always plan-gated, whatever the enforcement mode, except ``off``
(billing's full kill switch). Plan classes that include everything (`legacy-unlimited`, internal,
partner) include this too, as the engine resolves them.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.prospecting.strict_fit")

CAPABILITY = "discovery.strict_fit"
#: Shown when a workspace asks for it and its plan does not include it.
NOT_INCLUDED = ("Strict fit is not included in this workspace's plan. It comes with Enterprise "
                "plans; ask us to add it.")


async def available(ts) -> bool:
    """Whether this workspace's plan includes strict fit. Never raises."""
    from nexus.core.config import get_settings

    if get_settings().billing_enforcement == "off":
        return True
    try:
        from nexus.billing.entitlements import resolve_entitlement

        ent = await resolve_entitlement(ts, CAPABILITY)
        # `enterprise` is "talk to us", not "you have it" (as GET /billing/entitlements reads it).
        return ent.mode not in ("disabled", "enterprise")
    except Exception:
        logger.warning("strict fit entitlement check failed; not applying it", exc_info=True)
        return False


async def in_effect(ts, tenant=None) -> bool:
    """Whether discovery applies strict fit for this workspace right now: asked for AND included."""
    if tenant is None:
        from nexus.models.identity import Tenant

        tenant = await ts.session.get(Tenant, ts.tenant_id)
    if tenant is None or not getattr(tenant, "icp_strict_fit", False):
        return False
    return await available(ts)
