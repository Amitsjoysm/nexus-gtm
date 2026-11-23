"""What a set of charges would cost and whether this workspace may spend it, asked BEFORE anything
is bought (MCP previews, owner decision 2026-11-20: "ask permission and give credit info at each
step"). The one copy: Prospector's confirm dialog (`prospector/charges.py`) delegates here.

The price is the meter's own arithmetic (`entitlements._usage_amount`, marginal on a tiered card) and
the yes or no is the billing engine's own (`preflight`), so a preview can never refuse what the action
would allow or allow what it would refuse. Read-only: nothing is recorded, burned or locked.
"""
from __future__ import annotations

_REASONS = {
    "credits_exhausted": "Not enough credits for this. Add credits in Billing, or do less.",
    "disabled": "Your plan does not include this. An admin can change the plan in Billing.",
    "dependency": "Your plan does not include this feature. An admin can change the plan in "
                  "Billing.",
    "quota_exhausted": "Your plan's limit for this is used up for the period.",
    "throttled": "Too many requests in a short time. Try again in a minute.",
}


async def _flat(ts, capabilities: list[str]) -> dict[str, float]:
    from sqlalchemy import select

    from nexus.models.billing import BillingRateCard

    rows = (await ts.session.scalars(select(BillingRateCard).where(
        BillingRateCard.capability_id.in_(capabilities), BillingRateCard.active.is_(True)))).all()
    return {r.capability_id: float(r.credits_per_unit or 0) for r in rows}


async def price(ts, charges: dict[str, int]) -> dict[str, float]:
    """Credits each capability's quantity costs, as the meter will charge it: the MARGINAL cost on a
    tiered card (`entitlements._usage_amount`), falling back to the flat card if resolution fails.
    Unrounded, because Prospector's confirm dialog reads it as it always has. Never raises."""
    from nexus.billing.entitlements import _usage_amount, resolve_entitlement

    wanted = {c: q for c, q in charges.items() if q > 0}
    flat = await _flat(ts, sorted(wanted))
    out: dict[str, float] = {}
    for capability, quantity in sorted(wanted.items()):
        try:
            ent = await resolve_entitlement(ts, capability)
            amount = await _usage_amount(ts, ent, float(quantity))
        except Exception:
            amount = None
        out[capability] = float(amount) if amount is not None \
            else quantity * flat.get(capability, 0.0)
    return out


async def first_refusal(ts, charges: dict[str, int]) -> tuple[str, object] | None:
    """The first charge `preflight` would refuse, as (reason code, entitlement), or None."""
    from nexus.billing.entitlements import preflight

    for capability, quantity in sorted(charges.items()):
        if quantity <= 0:
            continue
        result = await preflight(ts, capability, quantity=quantity)
        if not result.allowed:
            return result.reason or "quota_exhausted", result.entitlement
    return None


async def uses_credits(ts, charges: dict[str, int]) -> bool:
    """Whether these charges would take credits at all: not on an unlimited or internal plan, with
    no subscription, in shadow mode, or with billing off (the meter burns nothing there)."""
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


async def quote(ts, charges: dict[str, int]) -> dict:
    """``{"credits", "lines", "uses_credits", "balance", "covered", "reason"}``."""
    from nexus.billing.credits import balance

    lines = await price(ts, charges)
    spends = await uses_credits(ts, charges)
    refusal = await first_refusal(ts, charges)
    reason = None
    if refusal is not None:
        code, ent = refusal
        if code == "feature_switch":
            reason = (getattr(ent, "switch_message", "") if ent is not None else "") \
                or "This is switched off right now."
        else:
            reason = _REASONS.get(code, "This cannot be done on your plan.")
    return {"credits": round(sum(lines.values()), 2) if spends else 0.0,
            "lines": {c: round(v, 2) for c, v in lines.items()},
            "uses_credits": spends, "balance": await balance(ts) if spends else None,
            "covered": refusal is None, "reason": reason}
