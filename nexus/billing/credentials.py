# nexus/billing/credentials.py
"""Managing the payment provider's credentials without a redeploy.

``providers/catalog.py`` deliberately excluded Stripe from the generic key pool, and the reason it
gave is the design brief for this module: **money fails silently**. A dead search key returns no
results and somebody notices within a day. A wrong Stripe key means checkout sessions stop being
created and invoices stop being raised — which is indistinguishable from a quiet month, right up
until a customer asks why they were never charged.

So this is not key CRUD with a Stripe label on it. Three rules the generic pool does not have:

* **Verification is mandatory before activation.** ``activate`` refuses a credential that has not
  made a successful live call. There is no equivalent of "add it and see" for money.
* **The account is read back and stored.** An operator pasting a key cannot otherwise tell test
  from live, or one business's account from another's. "Wrong Stripe account" is the failure that
  charges the wrong company's customers.
* **Exactly one is active.** No rotation pool — you cannot ride out a bad Stripe key by trying the
  next one, and two accounts both collecting money with no rule about which is worse than an
  outage, because it is an outage you cannot see.

Everything runs on the platform sessionmaker: the table has no ``tenant_id``.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from nexus.core.crypto import seal_text, unseal_text
from nexus.core.db import get_platform_sessionmaker
from nexus.models.payment_credential import PaymentCredential

logger = logging.getLogger("nexus.billing.credentials")


class CredentialError(RuntimeError):
    """The credential cannot be used or the transition is not allowed."""


def _hint(secret: str) -> str:
    return (secret or "")[-4:]


async def list_credentials() -> list[PaymentCredential]:
    async with get_platform_sessionmaker()() as s:
        return list((await s.scalars(
            select(PaymentCredential).order_by(
                PaymentCredential.active.desc(), PaymentCredential.created_at
            )
        )).all())


async def add_credential(
    *, provider: str = "stripe", label: str, secret_key: str, publishable_key: str = "",
    webhook_secret: str = "", user_id: str = "",
) -> PaymentCredential:
    """Store a credential set. It starts inactive and unverified — always."""
    secret_key = (secret_key or "").strip()
    if not secret_key:
        raise CredentialError("a secret key is required")
    async with get_platform_sessionmaker()() as s:
        row = PaymentCredential(
            provider=provider or "stripe",
            label=(label or "").strip(),
            secret_key_encrypted=seal_text(secret_key),
            # Sealed even when blank, so the column never mixes ciphertext with empty plaintext and
            # a reader cannot mistake "not set" for "failed to decrypt".
            webhook_secret_encrypted=seal_text(webhook_secret.strip()) if webhook_secret else "",
            publishable_key=(publishable_key or "").strip(),
            key_hint=_hint(secret_key),
            status="registered",
            active=False,
            created_by_user_id=user_id or None,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def secret_for(row: PaymentCredential) -> str:
    """Unseal the secret key. Raises rather than returning "" — an empty string reads as
    "not configured", and the operator's next move for those two states is opposite."""
    if not row.secret_key_encrypted:
        raise CredentialError("this credential has no stored secret key")
    return unseal_text(row.secret_key_encrypted)


async def verify_credential(credential_id: str) -> dict:
    """Make a real call and record what came back.

    Reads ``/v1/account``, which is the cheapest call that proves the key works AND says which
    account it belongs to. A key that merely authenticates is not enough here: authenticating
    against the wrong business is the expensive mistake, and it looks identical to success.
    """
    async with get_platform_sessionmaker()() as s:
        row = await s.get(PaymentCredential, credential_id)
        if row is None:
            raise CredentialError("no such credential")
        secret = await secret_for(row)

        from nexus.billing.payments import StripePaymentProvider

        try:
            account = await StripePaymentProvider(secret)._get("/account")
        except Exception as exc:
            row.status = "failed"
            row.last_error = str(exc)[:500]
            # A failed verification also deactivates. Leaving a now-broken credential active
            # because it passed last month is how a silent billing outage lasts a month.
            row.active = False
            await s.commit()
            return {"ok": False, "status": "failed", "detail": row.last_error}

        row.account_id = str(account.get("id") or "")[:120]
        row.account_name = str(
            (account.get("settings") or {}).get("dashboard", {}).get("display_name")
            or account.get("business_profile", {}).get("name")
            or account.get("email")
            or ""
        )[:200]
        row.livemode = bool(account.get("charges_enabled")) and not str(secret).startswith(
            "sk_test_"
        )
        row.status = "verified"
        row.last_error = ""
        await s.commit()
        return {
            "ok": True, "status": "verified", "account_id": row.account_id,
            "account_name": row.account_name, "livemode": row.livemode,
        }


async def activate_credential(credential_id: str) -> PaymentCredential:
    """Make this the credential the platform bills with. Refuses anything unverified."""
    async with get_platform_sessionmaker()() as s:
        row = await s.get(PaymentCredential, credential_id)
        if row is None:
            raise CredentialError("no such credential")
        if row.status != "verified":
            raise CredentialError(
                "verify this credential against the provider before activating it — "
                "a wrong payment key stops billing silently"
            )
        for other in (await s.scalars(
            select(PaymentCredential).where(PaymentCredential.provider == row.provider)
        )).all():
            other.active = other.id == row.id
        await s.commit()
        await s.refresh(row)
    _invalidate()
    return row


async def deactivate_credential(credential_id: str) -> PaymentCredential | None:
    """Stop billing with this credential. Never refused — during an incident "stop taking money
    with this account" must not be blocked by a state machine."""
    async with get_platform_sessionmaker()() as s:
        row = await s.get(PaymentCredential, credential_id)
        if row is None:
            return None
        row.active = False
        await s.commit()
        await s.refresh(row)
    _invalidate()
    return row


async def delete_credential(credential_id: str) -> bool:
    """Remove a credential. The active one is refused: deleting what the platform is currently
    billing with, in one click, with no confirmation the operator understood, is not a feature."""
    async with get_platform_sessionmaker()() as s:
        row = await s.get(PaymentCredential, credential_id)
        if row is None:
            return False
        if row.active:
            raise CredentialError(
                "deactivate this credential before deleting it — it is the one in use"
            )
        await s.delete(row)
        await s.commit()
    _invalidate()
    return True


async def active_credential(provider: str = "stripe") -> PaymentCredential | None:
    async with get_platform_sessionmaker()() as s:
        return (await s.scalars(
            select(PaymentCredential).where(
                PaymentCredential.provider == provider,
                PaymentCredential.active.is_(True),
            )
        )).first()


async def resolve_stripe_secrets() -> tuple[str, str, str]:
    """``(secret_key, webhook_secret, publishable_key)`` — managed if one is active, else the
    environment.

    The environment fallback is what makes this additive: a deployment that never opens this screen
    behaves exactly as it did before the table existed. Any failure to read the managed credential
    also falls back rather than raising — losing the ability to bill because a lookup hiccuped is
    worse than billing with the previous configuration.
    """
    from nexus.core.config import get_settings

    s = get_settings()
    # `stripe_publishable_key` is read with getattr because Settings does not define it — the
    # publishable key has never been needed server-side (the hosted Checkout page carries its own),
    # and inventing a setting for it here would be a config change this surface does not need.
    env = (s.stripe_secret_key or "", s.stripe_webhook_secret or "",
           getattr(s, "stripe_publishable_key", "") or "")
    try:
        row = await active_credential("stripe")
        if row is None:
            return env
        secret = unseal_text(row.secret_key_encrypted) if row.secret_key_encrypted else ""
        hook = unseal_text(row.webhook_secret_encrypted) if row.webhook_secret_encrypted else ""
        return (secret or env[0], hook or env[1], row.publishable_key or env[2])
    except Exception:
        logger.warning("could not read the managed Stripe credential; using the environment",
                       exc_info=True)
        return env


def _invalidate() -> None:
    """Drop the cached payment provider so the next call builds one with the new key."""
    try:
        from nexus.billing.payments import set_payment_provider

        set_payment_provider(None)
    except Exception:
        logger.debug("could not reset the payment provider", exc_info=True)
