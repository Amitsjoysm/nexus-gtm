# nexus/billing/stripe_carryover.py
"""Carry live Stripe subscriptions into the new account when the Stripe account changes.

The rule (product owner, 2026-10-09): switching the Stripe account changes only where the money
goes. Plans are created in the new account (`stripe_catalog.sync_plans`), customers follow on
first use (`customer_id_for`), and this module moves the subscriptions, so nobody has to start a
paying workspace again by hand.

Stripe cannot move a subscription or a card between accounts through its API, so each live
subscription is RECREATED in the new account and the old one is told to end:

1. The old one is read with the old account's key, from the stored credentials or the
   environment. Without that key nothing is carried: a new subscription beside an old one that
   keeps renewing bills the customer twice, and only the old key can stop the old one.
2. A subscription the old account already ends (cancelled, or cancelling at period end) is left:
   the customer chose to leave.
3. The customer is found or created in the new account. A customer Stripe's own migration copied
   keeps its id and its card, and is adopted.
4. The new subscription is on the same plan and first bills where the old paid period ends, so
   nothing is charged twice. It charges the card when the new account holds one for the customer
   and otherwise emails an invoice, which asks for money and never takes it: a workspace that only
   ever paid with a test card is never charged a real one.
5. The old one is set to end at period end, naming the new one. If Stripe refuses, the new one is
   cancelled at once, so there is no moment with two subscriptions renewing.
6. Our row points at the new subscription. The old id is kept under `previous_psp_subscriptions`,
   and the webhook ignores events about it.

Credits: the plan does not change, so nothing is granted. The period that was paid for in the old
account keeps its credits, and the webhook keeps our period start for the carried first period
(see `webhooks._apply_subscription_event`), so the period roll is exactly where it was.
"""
from __future__ import annotations

import logging

from nexus.billing.payments import CARRIED_TO_KEY, PaymentError
from nexus.core.db import utcnow
from nexus.models.billing import BillingPlan, BillingSubscription

logger = logging.getLogger("nexus.billing.stripe_carryover")

#: Statuses worth carrying: a subscription that is still being billed.
LIVE_STATUSES = ("active", "trialing", "past_due")
DAYS_TO_PAY = 14
_HISTORY = 5
NO_OLD_KEY = (
    "The previous Stripe account's key is not stored here (or cannot read subscriptions), so its "
    "subscription cannot be ended from the app, and carrying it over would bill the customer "
    "twice. Add that key on the Payments tab without making it live and press Move subscriptions "
    "here, or cancel it in that account's Stripe dashboard, set it canceled with Edit terms on "
    "the Customers tab, and start a new Stripe subscription."
)


async def previous_account_providers(current_key: str) -> list:
    """A Stripe provider for every other key this deployment holds: stored credentials (active or
    not) and the environment's. The only place an old account's key is read from."""
    from nexus.billing.credentials import list_credentials, secret_for
    from nexus.billing.payments import StripePaymentProvider
    from nexus.core.config import get_settings

    keys: list[str] = []
    try:
        for row in await list_credentials():
            if row.provider != "stripe":
                continue
            try:
                keys.append(await secret_for(row))
            except Exception:  # an unsealable row is a key we do not have
                logger.warning("could not unseal payment credential %s", row.id)
    except Exception:
        logger.warning("could not list payment credentials", exc_info=True)
    keys.append(get_settings().stripe_secret_key or "")
    seen = {current_key}
    out = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            out.append(StripePaymentProvider(key))
    return out


async def _find_old(subscription_id: str, olds: list) -> tuple:
    for old in olds:
        data = await old.get_subscription(subscription_id=subscription_id)
        if data.get("id"):
            return old, data
    return None, {}


async def _lives_here(sub, provider, account: str) -> bool:
    from nexus.billing.stripe_catalog import _exists

    recorded = str((sub.meta or {}).get("psp_account") or "")
    if recorded:
        return recorded == account
    here = await _exists(provider, "subscriptions", sub.psp_subscription_id)
    if here:
        sub.meta = {**(sub.meta or {}), "psp_account": account}
    return here


def _epoch_of(value) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    from datetime import timezone

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


async def carry_one(session, sub, provider, *, account: str, olds: list,
                    actor: str = "system") -> dict:
    """Carry ``sub`` into the current account if it is billed in a previous one.

    ``session`` is bound to the subscription's tenant. Writes only to ``sub`` and the audit log;
    the caller commits. Returns ``{"outcome": in_place | carried | skipped | failed, ...}``.
    Raises ``PaymentError`` only for a refusal from the NEW account, before anything changed.
    """
    from nexus.billing.audit import record_admin_action
    from nexus.billing.stripe_catalog import current_account, customer_id_for, plan_price_id
    from nexus.billing.stripe_subscriptions import BILLABLE_CLASSES, owner_email
    from nexus.core.tenancy import TenantSession, apply_rls

    old_id = str(sub.psp_subscription_id or "")
    out = {"tenant_id": sub.tenant_id, "plan_id": sub.plan_id, "from": old_id}
    if await _lives_here(sub, provider, account):
        return {**out, "outcome": "in_place"}
    old_provider, old = await _find_old(old_id, olds)
    if old_provider is None:
        return {**out, "outcome": "failed", "detail": NO_OLD_KEY}

    new_id = str((old.get("metadata") or {}).get(CARRIED_TO_KEY) or "")
    collection = ""
    if not new_id:
        if old.get("status") == "canceled" or old.get("cancel_at_period_end"):
            return {**out, "outcome": "skipped",
                    "detail": "It already ends in the previous Stripe account."}
        plan = await session.get(BillingPlan, sub.plan_id)
        if (plan is None or plan.plan_class not in BILLABLE_CLASSES
                or int(plan.base_price_cents or 0) <= 0):
            return {**out, "outcome": "skipped", "detail": "Its plan has nothing to bill."}
        await apply_rls(session, sub.tenant_id)
        email = await owner_email(TenantSession(session, sub.tenant_id))
        price_id = await plan_price_id(plan, provider, account=account)
        customer_id = await customer_id_for(sub, provider, tenant_id=sub.tenant_id, email=email,
                                            account=account)
        if not customer_id:
            return {**out, "outcome": "failed",
                    "detail": "This workspace has no owner email to bill in the new account."}
        reader = getattr(provider, "default_payment_method", None)
        card = str(await reader(customer_id) or "") if reader else ""
        trial = old.get("status") == "trialing" and bool(old.get("trial_end"))
        starts_at = _epoch_of(old.get("trial_end") if trial else old.get("current_period_end")) \
            or _epoch_of(sub.current_period_end)
        created = await provider.create_carried_subscription(
            customer_id=customer_id, price_id=price_id, tenant_id=sub.tenant_id,
            plan_id=plan.id, starts_at=starts_at, trial=trial, payment_method=card,
            days_until_due=DAYS_TO_PAY, carried_from=old_id, idempotency_key=f"carry:{old_id}",
        )
        new_id = str(created.get("id") or "")
        collection = str(created.get("collection_method") or "")
        try:
            await old_provider.end_carried_subscription(subscription_id=old_id,
                                                        carried_to=new_id)
        except PaymentError as exc:
            # Never leave two renewing. The new one has billed nothing yet, so it goes now.
            undo = ""
            try:
                await provider.cancel_subscription(subscription_id=new_id, at_period_end=False)
            except PaymentError as undo_exc:
                logger.error("carried subscription %s could not be undone: %s", new_id, undo_exc)
                undo = f" Cancel {new_id} in the new account's Stripe dashboard."
            return {**out, "outcome": "failed",
                    "detail": f"The previous account refused to end it: {exc}.{undo}"[:400]}
        sub.psp_customer_id = customer_id
    else:
        # An earlier run created the new one and ended the old one, then stopped before saving.
        found = await provider.get_subscription(subscription_id=new_id)
        if not found.get("id"):
            return {**out, "outcome": "failed",
                    "detail": f"It was carried to {new_id}, which this account cannot find."}
        sub.psp_customer_id = str(found.get("customer") or sub.psp_customer_id or "")
        collection = str(found.get("collection_method") or "")

    old_account = await current_account(old_provider)
    meta = dict(sub.meta or {})
    previous = list(meta.get("previous_psp_subscriptions") or [])
    previous.append({"id": old_id, "account": old_account})
    meta.update({
        "previous_psp_subscriptions": previous[-_HISTORY:], "psp_account": account,
        "psp_customer_account": account, "carried_at": utcnow().isoformat(),
        "carried_by": actor,
    })
    if collection:
        meta["collection"] = collection
    await apply_rls(session, sub.tenant_id)
    sub.psp_subscription_id = new_id
    sub.meta = meta
    await session.flush()
    result = {**out, "outcome": "carried", "to": new_id, "collection": collection,
              "from_account": old_account}
    await record_admin_action(
        session, actor=actor, action="subscription.stripe_carry_over", target=sub.plan_id,
        subject_tenant_id=sub.tenant_id, before={"psp_subscription_id": old_id,
                                                  "account": old_account},
        after={"psp_subscription_id": new_id, "account": account, "collection": collection},
    )
    return result


async def ensure_in_current_account(session, sub, provider, *, actor: str = "system") -> dict:
    """Carry one workspace's subscription before anything acts on it. Used by plan change and
    cancel, so a subscription the account switch missed is moved instead of failing."""
    from nexus.billing.stripe_catalog import current_account

    account = await current_account(provider)
    olds = await previous_account_providers(getattr(provider, "secret_key", ""))
    try:
        return await carry_one(session, sub, provider, account=account, olds=olds, actor=actor)
    finally:
        await _close(olds)


async def carry_over_all(provider, *, actor: str = "system") -> list[dict]:
    """Carry every live subscription billed in a previous account. One row per subscription that
    was not already here; each runs in its own transaction, so one refusal stops nothing else."""
    from sqlalchemy import select

    from nexus.billing.stripe_catalog import current_account
    from nexus.core.db import get_platform_sessionmaker
    from nexus.core.tenancy import apply_rls

    if getattr(provider, "name", "") != "stripe":
        return []
    maker = get_platform_sessionmaker()
    async with maker() as session:
        ids = list((await session.scalars(
            select(BillingSubscription.id).where(
                BillingSubscription.status.in_(LIVE_STATUSES),
                BillingSubscription.psp_subscription_id.is_not(None),
                BillingSubscription.psp_subscription_id != "",
                BillingSubscription.cancel_at_period_end.is_(False),
            ).order_by(BillingSubscription.created_at)
        )).all())
    if not ids:
        return []           # nothing billed through Stripe: ask Stripe nothing
    account = await current_account(provider)
    olds = await previous_account_providers(getattr(provider, "secret_key", ""))
    report: list[dict] = []
    try:
        for sub_id in ids:
            async with maker() as session:
                sub = await session.get(BillingSubscription, sub_id)
                if sub is None:
                    continue
                await apply_rls(session, sub.tenant_id)
                try:
                    row = await carry_one(session, sub, provider, account=account, olds=olds,
                                          actor=actor)
                    await session.commit()
                except PaymentError as exc:
                    await session.rollback()
                    row = {"tenant_id": sub.tenant_id, "plan_id": sub.plan_id,
                           "from": sub.psp_subscription_id, "outcome": "failed",
                           "detail": f"Stripe refused: {exc}"[:400]}
                if row["outcome"] != "in_place":
                    report.append(row)
    finally:
        await _close(olds)
    return report


async def _close(providers: list) -> None:
    for p in providers:
        closer = getattr(p, "aclose", None)
        if closer is not None:
            try:
                await closer()
            except Exception:
                pass


async def cancel_in_previous_account(subscription_id: str, *, at_period_end: bool,
                                     current_key: str = "") -> bool:
    """Cancel a subscription in whichever previous account holds it. False when no stored key
    can see it, in which case nothing was cancelled anywhere."""
    olds = await previous_account_providers(current_key)
    try:
        old, _data = await _find_old(subscription_id, olds)
        if old is None:
            return False
        await old.cancel_subscription(subscription_id=subscription_id,
                                      at_period_end=at_period_end)
        return True
    finally:
        await _close(olds)
