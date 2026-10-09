# nexus/billing/stripe_subscriptions.py
"""Stripe subscriptions a superadmin starts or moves from the console (decided 2026-10-09).

Two ways to start one: an emailed Stripe invoice (the subscription starts now, its credits arrive
when the invoice is paid) or a Checkout link (nothing changes until the customer pays, then the
existing webhook applies it). A plan change for a Stripe-billed workspace moves the Stripe price
now and invoices the prorated difference.

Every Stripe write happens before our row changes, so a refusal leaves NEXUS untouched.
"""
from __future__ import annotations

from nexus.billing.payments import resolve_payment_provider
from nexus.core.db import utcnow
from nexus.models.billing import BillingPlan

#: Plan classes that can be billed through a Stripe subscription. Everything else charges nobody
#: (free, trial, internal, unlimited) or is invoiced from the console (enterprise).
BILLABLE_CLASSES = ("standard", "custom")
LIVE_STATUSES = ("active", "trialing", "past_due")
NO_CREDENTIAL = ("No Stripe credential is in force. Add and activate one on the Payments tab "
                 "first.")


class StripeSubscriptionError(Exception):
    """A request the current state forbids. ``status_code`` is the HTTP answer."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def _app_url(path: str) -> str:
    from nexus.core.config import get_settings

    base = (get_settings().app_base_url or "").rstrip("/")
    return f"{base}{path}" if base else path


async def _newest_subscription(ts):
    from nexus.billing.subscriptions import _active, _any_subscription

    return await _active(ts) or await _any_subscription(ts)


async def owner_email(ts) -> str:
    """The workspace owner's email: who a Stripe invoice is sent to by default."""
    from sqlalchemy import select

    from nexus.models.identity import Membership, User

    row = (await ts.session.execute(
        select(User.email).join(Membership, Membership.user_id == User.id)
        .where(Membership.tenant_id == ts.tenant_id, Membership.role == "owner")
        .order_by(Membership.created_at)
    )).first()
    return str(row[0] or "") if row else ""


def _check_billable(plan, sub) -> None:
    if plan.plan_class not in BILLABLE_CLASSES or int(plan.base_price_cents or 0) <= 0:
        raise StripeSubscriptionError(f"'{plan.name}' has no price to bill through Stripe.")
    if plan.plan_class == "custom" and (sub is None or sub.plan_id != plan.id):
        raise StripeSubscriptionError(
            "A custom plan can only be billed for the workspace it was made for.")


async def stripe_subscription_of(ts):
    """The workspace's live subscription when it is billed through Stripe, else None."""
    from nexus.billing.subscriptions import _active

    sub = await _active(ts)
    return sub if sub is not None and (sub.psp_subscription_id or "").strip() else None


async def start_subscription(ts, *, plan_id: str, method: str, days_until_due: int = 14,
                             billing_email: str = "", actor: str = "system") -> dict:
    """Start the workspace on ``plan_id`` through Stripe: ``invoice`` or ``checkout_link``."""
    from nexus.billing.stripe_catalog import current_account, customer_id_for, plan_price_id
    from nexus.billing.subscriptions import ensure_subscription, take_plan_terms
    from nexus.billing.webhooks import STRIPE_SUBSCRIPTION_STATUS, _epoch

    plan = await ts.session.get(BillingPlan, plan_id)
    if plan is None:
        raise StripeSubscriptionError(f"Unknown plan '{plan_id}'", 404)
    sub = await _newest_subscription(ts)
    _check_billable(plan, sub)
    if sub is not None and (sub.psp_subscription_id or "") and sub.status in LIVE_STATUSES:
        raise StripeSubscriptionError(
            "This workspace already has a Stripe subscription. Change its plan instead.")
    provider = await resolve_payment_provider()
    if provider.name != "stripe":
        raise StripeSubscriptionError(NO_CREDENTIAL)
    email = billing_email or await owner_email(ts)
    if not email:
        raise StripeSubscriptionError(
            "This workspace has no owner email to bill. Enter a billing email.")

    account = await current_account(provider)
    price_id = await plan_price_id(plan, provider, account=account)
    customer_id = await customer_id_for(sub, provider, tenant_id=ts.tenant_id, email=email,
                                        account=account)
    if method == "checkout_link":
        link = await provider.create_checkout_session(
            tenant_id=ts.tenant_id, plan_id=plan.id, price_id=price_id, customer_id=customer_id,
            success_url=_app_url("/settings/billing?checkout=success"),
            cancel_url=_app_url("/settings/billing?checkout=cancelled"),
        )
        await ts.flush()
        return {"method": "checkout_link", "plan_id": plan.id,
                "url": str(link.get("url") or ""), "expires_at": int(link.get("expires_at") or 0)}

    created = await provider.create_invoiced_subscription(
        customer_id=customer_id, price_id=price_id, tenant_id=ts.tenant_id, plan_id=plan.id,
        days_until_due=days_until_due,
        idempotency_key=f"admin-sub:{account}:{ts.tenant_id}:{plan.id}",
    )
    if sub is None:
        sub = await ensure_subscription(ts, plan_id=plan.id)
    take_plan_terms(sub, plan)
    sub.psp_subscription_id = str(created.get("id") or "")
    sub.psp_customer_id = customer_id
    sub.status = STRIPE_SUBSCRIPTION_STATUS.get(str(created.get("status") or ""), "active")
    sub.cancel_at_period_end = False
    start = _epoch(created.get("current_period_start"))
    end = _epoch(created.get("current_period_end"))
    if start is not None:
        sub.current_period_start = start
    if end is not None:
        sub.current_period_end = end
    sub.meta = {**(sub.meta or {}), "collection": "send_invoice",
                "credits_pending_plan": plan.id, "started_by": actor,
                "started_at": utcnow().isoformat(), "psp_customer_account": account}
    await ts.flush()
    return {"method": "invoice", "plan_id": plan.id, "status": sub.status,
            "psp_subscription_id": sub.psp_subscription_id,
            "invoice_url": str(created.get("hosted_invoice_url") or ""),
            "invoice_note": str(created.get("invoice_note") or ""),
            "days_until_due": days_until_due}


async def change_stripe_plan(ts, sub, plan_id: str, *, actor: str = "system"):
    """Move a Stripe-billed workspace to ``plan_id`` AT STRIPE, now, prorated and invoiced.

    Our row changes only after Stripe agrees. Credits follow the subscription: a card subscription
    gets them now (keyed per plan per period, so the webhook's later grant is a no-op); an invoiced
    one gets them when the proration invoice is paid.
    """
    from nexus.billing.stripe_catalog import plan_price_id
    from nexus.billing.subscriptions import apply_plan_change_credits, take_plan_terms

    plan = await ts.session.get(BillingPlan, plan_id)
    if plan is None:
        raise StripeSubscriptionError(f"Unknown plan '{plan_id}'", 404)
    if plan.plan_class != "standard" or int(plan.base_price_cents or 0) <= 0:
        raise StripeSubscriptionError(
            f"'{plan.name}' cannot be billed through this Stripe subscription. Cancel the Stripe "
            "subscription first, then change the plan.")
    psp = (sub.psp_subscription_id or "").strip()
    provider = await resolve_payment_provider()
    if provider.name != "stripe" and psp.startswith("sub_"):
        raise StripeSubscriptionError(
            f"This subscription is billed through Stripe ({psp}). {NO_CREDENTIAL}")
    price_id = await plan_price_id(plan, provider)
    result = await provider.change_subscription_price(
        subscription_id=psp, price_id=price_id, plan_id=plan.id)
    previous = sub.plan_id
    take_plan_terms(sub, plan)
    invoiced = str(result.get("collection_method")
                   or (sub.meta or {}).get("collection") or "") == "send_invoice"
    sub.meta = {**(sub.meta or {}), "previous_plan_id": previous, "changed_by": actor,
                "changed_at": utcnow().isoformat(), "changed_via": "stripe",
                **({"credits_pending_plan": plan.id} if invoiced else {})}
    await ts.flush()
    if not invoiced:
        await apply_plan_change_credits(ts, plan)
    return sub
