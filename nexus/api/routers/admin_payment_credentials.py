# nexus/api/routers/admin_payment_credentials.py
"""Manage the payment provider's account from the Control plane.

Gated on ``PRICING_WRITE``, which the ``superadmin`` and ``billing`` presets carry — the same
permission that sets prices, because a payment credential decides *which business* the money lands
in and that is a commercial decision, not an infrastructure one.

**The secret key is in no response model, ever.** ``key_hint`` (last four) plus the account name
read back from the provider is all the UI gets. The account name is the point: an operator pasting
a key cannot otherwise tell a test account from a live one, or one business from another, and
"verified against the wrong Stripe account" looks exactly like success until a customer is charged
by the wrong company.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.audit import record_admin_action
from nexus.billing.permissions import PRICING_WRITE, STRIPE_MANAGE
from nexus.core.db import get_platform_sessionmaker

router = APIRouter(prefix="/admin/payment-credentials", tags=["admin-payments"])


class CredentialOut(BaseModel):
    id: str
    provider: str
    label: str
    key_hint: str
    publishable_key: str
    account_id: str
    account_name: str
    livemode: bool
    status: str
    last_error: str
    active: bool
    # The Stripe payment method configuration Checkout sends; "" is the account's Default Config.
    payment_method_configuration: str = ""
    # Set only on activation by a superadmin: what syncing the plans to this account did, and
    # which subscriptions were carried over from the previous account.
    stripe_sync: list[dict] | None = None
    stripe_carry: list[dict] | None = None


class CredentialIn(BaseModel):
    # `extra="forbid"`, so a request body cannot smuggle `status` or `active` and skip the
    # verification the ladder exists to enforce. Same discipline as `nexus/sources/service.py`.
    model_config = {"extra": "forbid"}

    provider: str = "stripe"
    label: str = ""
    secret_key: str
    publishable_key: str = ""
    webhook_secret: str = ""


def _out(row) -> CredentialOut:
    return CredentialOut(
        id=row.id, provider=row.provider, label=row.label, key_hint=row.key_hint,
        publishable_key=row.publishable_key, account_id=row.account_id,
        account_name=row.account_name, livemode=row.livemode, status=row.status,
        last_error=row.last_error, active=row.active,
        payment_method_configuration=row.payment_method_configuration or "",
    )


async def _audit(principal: Principal, action: str, target: str, after: dict) -> None:
    async with get_platform_sessionmaker()() as session:
        await record_admin_action(
            session, actor=principal.user_id, action=action, target=target, after=after,
        )
        await session.commit()


@router.get("", response_model=list[CredentialOut])
async def list_payment_credentials(
    _: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> list[CredentialOut]:
    from nexus.billing.credentials import list_credentials

    return [_out(r) for r in await list_credentials()]


@router.post("", response_model=CredentialOut, status_code=status.HTTP_201_CREATED)
async def add_payment_credential(
    body: CredentialIn,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> CredentialOut:
    """Store a credential set. It starts inactive and unverified, always.

    There is deliberately no "add and use immediately" path. A wrong payment key does not error —
    it stops billing, which reads as a quiet month.
    """
    from nexus.billing.credentials import CredentialError, add_credential

    if body.provider != "stripe":
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "only 'stripe' is supported today")
    try:
        row = await add_credential(
            provider=body.provider, label=body.label, secret_key=body.secret_key,
            publishable_key=body.publishable_key, webhook_secret=body.webhook_secret,
            user_id=principal.user_id,
        )
    except CredentialError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await _audit(principal, "payment_credential.create", row.id,
                 {"label": row.label, "key_hint": row.key_hint, "provider": row.provider})
    return _out(row)


@router.post("/{credential_id}/verify", response_model=dict)
async def verify_payment_credential(
    credential_id: str,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    """Make a real call and report which account answered.

    Reads the provider's own account object rather than just checking that the key authenticates.
    A key that authenticates against the wrong business is the expensive mistake, and it is
    indistinguishable from success without asking who it belongs to.
    """
    from nexus.billing.credentials import CredentialError, verify_credential

    try:
        result = await verify_credential(credential_id)
    except CredentialError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    await _audit(principal, "payment_credential.verify", credential_id, result)
    return result


@router.post("/{credential_id}/activate", response_model=CredentialOut)
async def activate_payment_credential(
    credential_id: str,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> CredentialOut:
    """Bill with this account from now on. Refused unless it has been verified."""
    from nexus.billing.credentials import CredentialError, activate_credential

    try:
        row = await activate_credential(credential_id)
    except CredentialError as exc:
        # 409, not 400: the request is well-formed and the credential exists — it is the STATE
        # that forbids this, and the fix is to verify, not to resend.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await _audit(principal, "payment_credential.activate", credential_id,
                 {"account_id": row.account_id, "account_name": row.account_name,
                  "livemode": row.livemode})
    from nexus.api.deps import platform_permissions
    from nexus.billing.permissions import STRIPE_MANAGE

    out = _out(row)
    # A new key may belong to a new Stripe account, where none of the plans exist yet. A
    # superadmin's activation creates them straight away; anyone else's leaves Stripe alone
    # (creating objects at Stripe is superadmin-only, 2026-10-09), and checkout still creates a
    # plan's price in the new account on its first purchase.
    if STRIPE_MANAGE in await platform_permissions(principal):
        # Plans first: a carried subscription needs its plan's price in the new account.
        out = out.model_copy(update={"stripe_sync": await _sync_plans_after_activation()})
        out = out.model_copy(update={
            "stripe_carry": await _carry_after_activation(principal.user_id)})
    return out


async def _sync_plans_after_activation() -> list[dict] | None:
    """Create the plans on sale in the account just activated. Never fails the activation:
    a refusal comes back as a `failed` row for the operator to read."""
    from nexus.billing import stripe_catalog
    from nexus.billing.payments import PaymentError, resolve_payment_provider
    from nexus.core.db import get_sessionmaker

    provider = await resolve_payment_provider()
    if provider.name != "stripe":
        return None
    try:
        async with get_sessionmaker()() as session:
            report = await stripe_catalog.sync_plans(session, provider)
            await session.commit()
        return report
    except PaymentError as exc:
        return [{"plan_id": "", "outcome": "failed", "detail": str(exc)[:300]}]


async def _carry_after_activation(actor: str) -> list[dict] | None:
    """Carry the previous account's live subscriptions into this one (2026-10-09: switching the
    account changes only where the money goes). Never fails the activation."""
    from nexus.billing.payments import PaymentError, resolve_payment_provider
    from nexus.billing.stripe_carryover import carry_over_all

    provider = await resolve_payment_provider()
    if provider.name != "stripe":
        return None
    try:
        return await carry_over_all(provider, actor=actor)
    except PaymentError as exc:
        return [{"outcome": "failed", "detail": str(exc)[:300]}]


@router.post("/{credential_id}/webhook-endpoint", response_model=dict)
async def register_webhook_endpoint(
    credential_id: str,
    principal: Principal = Depends(require_platform_permission(STRIPE_MANAGE)),
) -> dict:
    """Register this deployment's Stripe webhook in the credential's account and keep its secret.

    Replaces the hand-made step in the Stripe dashboard that every account switch needed: the URL
    comes from the app's public address, the API version is pinned to the one the handler reads,
    the events are exactly the ones it handles, and the signing secret goes straight onto this
    credential, sealed, without anyone copying it. Uses the credential's OWN key, so a new
    account can be set up completely before it is activated. Superadmin only (2026-10-09).
    """
    from nexus.billing.credentials import CredentialError, secret_for, set_webhook_secret
    from nexus.billing.payments import PaymentError, StripePaymentProvider
    from nexus.billing.webhooks import HANDLED_EVENTS
    from nexus.core.config import get_settings
    from nexus.models.payment_credential import PaymentCredential
    from nexus.runtime_config.service import _validate_public_base_url

    base = (get_settings().app_base_url or "").strip().rstrip("/")
    try:
        _validate_public_base_url(base)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Set the app's public address (Configuration, Billing) first: Stripe posts the "
            f"webhook there. {exc}",
        ) from exc
    async with get_platform_sessionmaker()() as session:
        row = await session.get(PaymentCredential, credential_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such credential")
        try:
            key = await secret_for(row)
        except CredentialError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    url = f"{base}/api/billing/webhooks/stripe"
    try:
        created = await StripePaymentProvider(key).create_webhook_endpoint(
            url=url, events=HANDLED_EVENTS)
        await set_webhook_secret(credential_id, created["secret"])
    except PaymentError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Stripe refused: {exc}") from exc
    except CredentialError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Stripe returned no usable signing secret: {exc}") from exc
    result = {"endpoint_id": created["id"], "url": url, "events": sorted(HANDLED_EVENTS),
              "api_version": StripePaymentProvider.API_VERSION,
              "replaced": created["replaced"], "others": created["others"]}
    await _audit(principal, "payment_credential.webhook_endpoint", credential_id, result)
    return result


@router.post("/{credential_id}/checkout-check", response_model=dict)
async def check_checkout(
    credential_id: str,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> dict:
    """Would Stripe Checkout open with a way to pay on this credential's account? Read-only.

    The answer to "There are no valid payment methods available for this session" (reported
    2026-10-12): the account's charges, card capability, payment method settings and, for an
    Indian account, the currencies the plans on sale use (`checkout_readiness`). Uses the
    credential's OWN key, so a new account is checked before it goes live.
    """
    from nexus.api.routers.admin_health import _currencies_on_sale
    from nexus.billing.checkout_readiness import checkout_readiness
    from nexus.billing.credentials import CredentialError, secret_for
    from nexus.billing.payments import PaymentError, StripePaymentProvider
    from nexus.models.payment_credential import PaymentCredential

    async with get_platform_sessionmaker()() as session:
        row = await session.get(PaymentCredential, credential_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such credential")
        try:
            key = await secret_for(row)
        except CredentialError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    try:
        provider = StripePaymentProvider(
            key, payment_method_configuration=row.payment_method_configuration or "")
        ready = await checkout_readiness(provider, currencies=await _currencies_on_sale(),
                                         fresh=True)
    except PaymentError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"Stripe would not show the account: {exc}") from exc
    return ready.as_dict()


class PaymentMethodConfigurationIn(BaseModel):
    model_config = {"extra": "forbid"}

    # "" returns Checkout to the account's Default Config.
    configuration_id: str = ""


@router.put("/{credential_id}/payment-method-configuration", response_model=CredentialOut)
async def set_payment_method_configuration(
    credential_id: str,
    body: PaymentMethodConfigurationIn,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> CredentialOut:
    """Choose which of the account's Stripe payment method configurations Checkout uses (2026-10-13).

    Optional: without one Checkout uses the account's Default Config, as it always has. Checked
    against Stripe with this credential's own key before it is stored, so an id from another
    account, an inactive one, or one with cards off is refused here rather than by a customer's
    Checkout page."""
    from nexus.billing import credentials

    try:
        row = await credentials.set_payment_method_configuration(
            credential_id, body.configuration_id[:80])
    except credentials.CredentialError as exc:
        missing = str(exc) == "no such credential"
        raise HTTPException(status.HTTP_404_NOT_FOUND if missing else status.HTTP_400_BAD_REQUEST,
                            str(exc)) from exc
    await _audit(principal, "payment_credential.payment_method_configuration", credential_id,
                 {"payment_method_configuration": row.payment_method_configuration})
    return _out(row)


@router.post("/{credential_id}/deactivate", response_model=CredentialOut)
async def deactivate_payment_credential(
    credential_id: str,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> CredentialOut:
    """Stop billing with this account. Never refused — during an incident, "stop taking money
    through this account" must not be blocked by a state machine."""
    from nexus.billing.credentials import deactivate_credential

    row = await deactivate_credential(credential_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such credential")
    await _audit(principal, "payment_credential.deactivate", credential_id, {"active": False})
    return _out(row)


@router.delete("/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payment_credential(
    credential_id: str,
    principal: Principal = Depends(require_platform_permission(PRICING_WRITE)),
) -> Response:
    from nexus.billing.credentials import CredentialError, delete_credential

    try:
        ok = await delete_credential(credential_id)
    except CredentialError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such credential")
    await _audit(principal, "payment_credential.delete", credential_id, {})
    # An explicit Response: FastAPI refuses to build a response model for 204, which must carry
    # no body. Matches `admin_sources` and `admin_provider_keys`.
    return Response(status_code=status.HTTP_204_NO_CONTENT)
