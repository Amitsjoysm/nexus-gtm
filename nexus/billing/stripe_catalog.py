# nexus/billing/stripe_catalog.py
"""Stripe ids that remember which Stripe account they belong to.

Checkout cached each plan's product and price ids in ``billing_plans.meta`` and each workspace's
customer id in ``billing_subscriptions.psp_customer_id``, and reused them without recording WHICH
account they were made in. Reported 2026-10-09: production moved from a test key to a live key and
staging to a new test account, and checkout sent the old account's ids to the new one, which
answered "No such price".

The rule, in one place: an id is reused only inside the account it was made in. A cache written
before accounts were recorded is checked once (a 404 means another account). Ids from another
account are kept under ``previous_stripe`` / ``previous_psp_customers`` so an operator can still
trace them; nothing is deleted.

Callers flush. Every function here writes only to the ORM objects it is handed.
"""
from __future__ import annotations

import logging

from nexus.billing.payments import PaymentError

logger = logging.getLogger("nexus.billing.stripe_catalog")

_PRICE_KEYS = ("product_id", "price_id", "price_account", "price_amount_cents",
               "price_currency", "price_interval", "price_active")
#: Recorded for an id that turned out not to exist in the current account: its real account is
#: unknown, only that it is not this one.
_UNKNOWN = "?"
_HISTORY = 5


def _terms(plan) -> dict:
    return {"price_amount_cents": int(plan.base_price_cents), "price_currency": plan.currency,
            "price_interval": plan.interval}


def sellable_at_provider(plan) -> bool:
    """Plans that exist at Stripe: standard tiers with a price. Free, trial, internal and
    unlimited plans never charge anyone, and custom plans keep their own path."""
    return plan.plan_class == "standard" and int(plan.base_price_cents or 0) > 0


async def current_account(provider) -> str:
    # Read through getattr: a provider shaped like the seam without subclassing it (several test
    # doubles are) predates accounts, and its ids are treated exactly as they were before.
    reader = getattr(provider, "account_id", None)
    if reader is None:
        return str(getattr(provider, "name", "") or "")
    return str(await reader() or "")


async def _exists(provider, kind: str, object_id: str) -> bool:
    check = getattr(provider, "object_exists", None)
    return True if check is None else bool(await check(kind, object_id))


def _retire(meta: dict, account: str) -> dict:
    previous = list(meta.get("previous_stripe") or [])
    previous.append({"product_id": meta.get("product_id") or "",
                     "price_id": meta.get("price_id") or "", "account": account})
    out = {k: v for k, v in meta.items() if k not in _PRICE_KEYS}
    out["previous_stripe"] = previous[-_HISTORY:]
    return out


async def plan_price_id(plan, provider, *, account: str = "") -> str:
    """The price for ``plan`` in the CURRENT account, creating what is missing.

    Within one account a plan keeps one product; a new price is made only when the amount,
    currency or interval changed, and the superseded price is archived. Raises ``PaymentError``
    when the provider refuses; ``plan.meta`` is then left as it was.
    """
    account = account or await current_account(provider)
    meta = dict(plan.meta or {})
    price_id = str(meta.get("price_id") or "")
    recorded = str(meta.get("price_account") or "")
    if price_id and not recorded:
        # Cached before ids recorded their account: ask once.
        recorded = account if await _exists(provider, "prices", price_id) else _UNKNOWN
    if price_id and recorded != account:
        meta = _retire(meta, recorded)
        price_id = ""

    terms = _terms(plan)
    if price_id and all(meta.get(k) == v for k, v in terms.items()):
        if meta.get("price_active") is False:
            # Held earlier, on sale again: the same price, reactivated.
            await provider.set_price_active(price_id=price_id, active=True)
        meta.update({"price_account": account, "price_active": True})
        plan.meta = meta
        return price_id

    kwargs = {"plan_id": plan.id, "name": plan.name, "amount_cents": int(plan.base_price_cents),
              "currency": plan.currency, "interval": plan.interval}
    product_id = str(meta.get("product_id") or "") if price_id else ""
    if product_id:
        kwargs["product_id"] = product_id
    refs = await provider.ensure_plan_price(**kwargs)
    new_price = str(refs.get("price_id") or "")
    if price_id and price_id != new_price:
        # Superseded inside this account. Existing subscriptions keep billing on it; archiving only
        # stops it being used for anything new.
        await _set_active_quietly(provider, price_id, False)
    meta.update({"product_id": str(refs.get("product_id") or product_id), "price_id": new_price,
                 "price_account": account, "price_active": True, **terms})
    plan.meta = meta
    return new_price


async def _set_active_quietly(provider, price_id: str, active: bool) -> str:
    """Archive or reactivate, never failing the caller. Returns a warning, or ''."""
    try:
        await provider.set_price_active(price_id=price_id, active=active)
    except PaymentError as exc:
        logger.warning("could not set price %s active=%s: %s", price_id, active, exc)
        return f"Could not {'reactivate' if active else 'archive'} {price_id}: {exc}"[:300]
    return ""


async def _in_this_account(plan, provider) -> bool:
    meta = plan.meta or {}
    return bool(meta.get("price_id")) and meta.get("price_account") == await current_account(
        provider)


async def archive_plan_price(plan, provider) -> str:
    """Stop new purchases of ``plan`` at Stripe. Existing subscriptions keep billing on it.
    Never raises: a hold must not be blocked by Stripe. Returns a warning, or ''."""
    if not await _in_this_account(plan, provider):
        return ""
    warning = await _set_active_quietly(provider, str(plan.meta["price_id"]), False)
    if not warning:
        plan.meta = {**plan.meta, "price_active": False}
    return warning


async def rename_plan_product(plan, provider) -> str:
    """Rename the plan's product in this account. Never raises. Returns a warning, or ''."""
    if not await _in_this_account(plan, provider) or not (plan.meta or {}).get("product_id"):
        return ""
    try:
        await provider.rename_product(product_id=str(plan.meta["product_id"]), name=plan.name)
    except PaymentError as exc:
        return f"Could not rename the Stripe product: {exc}"[:300]
    return ""


async def apply_plan_to_provider(plan, provider, *, renamed: bool = False,
                                 was_status: str | None = None) -> dict:
    """Make Stripe match a plan a superadmin just created, edited, published or held.

    A plan on sale must be buyable, so a price it NEEDS raises ``PaymentError`` and the caller
    refuses the change. Archiving and renaming only warn.
    """
    out: dict = {"price_id": "", "warnings": []}
    if not sellable_at_provider(plan):
        return out
    if plan.status == "active":
        out["price_id"] = await plan_price_id(plan, provider)
    elif was_status == "active":
        warning = await archive_plan_price(plan, provider)
        if warning:
            out["warnings"].append(warning)
    if renamed:
        warning = await rename_plan_product(plan, provider)
        if warning:
            out["warnings"].append(warning)
    return out


async def sync_plans(session, provider, *, account: str = "") -> list[dict]:
    """Make every plan on sale exist in the current account. One row per plan:
    ``created`` / ``reused`` / ``failed`` (with Stripe's words)."""
    from sqlalchemy import select

    from nexus.models.billing import BillingPlan

    account = account or await current_account(provider)
    plans = (await session.scalars(
        select(BillingPlan).where(BillingPlan.status == "active",
                                  BillingPlan.plan_class == "standard",
                                  BillingPlan.base_price_cents > 0)
        .order_by(BillingPlan.sort_order, BillingPlan.id)
    )).all()
    report: list[dict] = []
    for plan in plans:
        before = str((plan.meta or {}).get("price_id") or "")
        try:
            price = await plan_price_id(plan, provider, account=account)
        except PaymentError as exc:
            report.append({"plan_id": plan.id, "outcome": "failed", "detail": str(exc)[:300]})
            continue
        report.append({"plan_id": plan.id, "price_id": price,
                       "outcome": "reused" if price == before else "created"})
    await session.flush()
    return report


async def customer_id_for(sub, provider, *, tenant_id: str, email: str, name: str = "",
                          account: str = "") -> str:
    """The workspace's customer in the CURRENT account, created when missing.

    Returns '' when there is no usable customer and no email to create one with; the caller
    decides what that means. Writes ``sub.psp_customer_id`` and ``sub.meta`` when ``sub`` exists.
    """
    account = account or await current_account(provider)
    meta = dict(sub.meta or {}) if sub is not None else {}
    existing = str(sub.psp_customer_id or "") if sub is not None else ""
    recorded = str(meta.get("psp_customer_account") or "")
    if existing and recorded != account:
        # Not recorded yet (a cache from before accounts were), or recorded in another account.
        # Stripe's own migration between accounts keeps customer ids, so a customer it copied
        # here, cards included, is adopted rather than duplicated.
        if await _exists(provider, "customers", existing):
            recorded = account
        elif not recorded:
            recorded = _UNKNOWN
    if existing and recorded == account:
        if meta.get("psp_customer_account") != account:
            meta["psp_customer_account"] = account
            sub.meta = meta
        return existing
    if not email:
        return ""
    customer_id = await provider.ensure_customer(tenant_id=tenant_id, email=email, name=name)
    if sub is not None:
        if existing:
            previous = list(meta.get("previous_psp_customers") or [])
            previous.append({"id": existing, "account": recorded})
            meta["previous_psp_customers"] = previous[-_HISTORY:]
        meta["psp_customer_account"] = account
        sub.psp_customer_id = customer_id
        sub.meta = meta
    return customer_id
