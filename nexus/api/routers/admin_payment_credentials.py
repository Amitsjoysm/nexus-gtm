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

from nexus.api.deps import Principal, get_principal, require_platform_permission
from nexus.billing.audit import record_admin_action
from nexus.billing.permissions import PRICING_WRITE
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
