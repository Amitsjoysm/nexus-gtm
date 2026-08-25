# nexus/models/payment_credential.py
"""The payment provider's credentials, managed from the Control plane.

Deliberately NOT a row in ``provider_keys``. ``providers/catalog.py`` excluded Stripe from that
table for a stated reason worth preserving: *money fails silently*. A dead search key returns no
results and somebody notices within a day; a wrong Stripe key means checkout sessions stop being
created and invoices stop being raised, which looks exactly like a quiet month. So this gets its
own table with a rule the generic key pool does not have — **a credential set cannot be activated
until a live call against it has succeeded** — and no rotation pool, because there is no such thing
as riding out a bad Stripe key by trying the next one.

Three secrets travel together and are useless apart: the secret key signs API calls, the webhook
secret verifies callbacks, and the publishable key is what the browser uses. Storing them as one
row means an operator swapping accounts swaps all three, rather than leaving a webhook secret from
the previous account silently rejecting every event.
"""
from __future__ import annotations

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin

# registered → verified → (enabled). Same ladder discipline as `nexus/sources/service.py`: only
# the service functions advance it, and a request body can never carry one.
CREDENTIAL_STATUSES = ("registered", "verified", "failed")


class PaymentCredential(IdMixin, TimestampMixin, Base):
    """One payment-provider account. Platform-global — no ``tenant_id``, no RLS."""

    __tablename__ = "payment_credentials"

    provider: Mapped[str] = mapped_column(String(32), default="stripe", index=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    # Fernet-sealed. Never in a response model, not even for the superadmin who typed it.
    secret_key_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    webhook_secret_encrypted: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Not sealed: the publishable key is designed to be shipped to a browser. Encrypting it would
    # imply a secrecy it does not have and cannot keep.
    publishable_key: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    # Last four of the secret key — all the UI ever gets.
    key_hint: Mapped[str] = mapped_column(String(8), default="")
    # Which Stripe account this actually is, read back from the provider during verification.
    # An operator pasting a key cannot otherwise tell test from live, or one account from another,
    # and "wrong account" is the failure that produces a real charge against the wrong business.
    account_id: Mapped[str] = mapped_column(String(120), default="")
    account_name: Mapped[str] = mapped_column(String(200), default="")
    livemode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="registered", nullable=False)
    last_error: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    # Exactly one credential can be active. A pool would mean two accounts collecting money with
    # no rule about which — and the wrong one is a charge against the wrong business.
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
