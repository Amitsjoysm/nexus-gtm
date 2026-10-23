# nexus/billing/checkout_readiness.py
"""Why Stripe Checkout would say "no valid payment methods", read from the account (2026-10-12).

Reported: "Continue to checkout" opened Stripe's page and it said "There are no valid payment
methods available for this session. Please contact the merchant." Our Checkout session names no
payment method types, so Stripe picks from the ACCOUNT's own settings when the page renders and
refuses when none fits the session: a subscription, in the plan's currency, for this buyer. The
session is created without complaint, so nothing on our side saw it, and the first to find out was
a customer trying to pay.

Blocking (Checkout refuses up front with a sentence, nothing charged):
- charges not enabled: Stripe has not finished activating the account (what is still due is
  counted from ``requirements.currently_due``);
- the ``card_payments`` capability not active;
- cards off, or unavailable, in the account's DEFAULT payment method configuration, which is what
  a session with no ``payment_method_types`` uses.

Warnings (Checkout goes ahead):
- an Indian Stripe account selling in a currency other than INR. Stripe lets Indian buyers pay an
  Indian business only in INR, so a buyer in India gets exactly that message; buyers elsewhere can
  pay once international payments are enabled on the account.
- payment method settings the key may not read (a restricted key without Payment Method
  Configurations: Read).

Read-only, cached per key for ``TTL_S`` (account activation changes rarely, and Checkout should not
cost two extra Stripe calls per click), and it raises only when the account itself cannot be read:
the Checkout preflight treats that as "go ahead", because a failing diagnostic must not block sales.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

TTL_S = 600

_cache: dict[str, tuple[float, "Readiness"]] = {}


@dataclass
class Readiness:
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    account_id: str = ""
    country: str = ""
    default_currency: str = ""
    charges_enabled: bool = True
    card_payments: str = ""

    @property
    def ok(self) -> bool:
        return not self.blocking

    def as_dict(self) -> dict:
        return {"ok": self.ok, "blocking": self.blocking, "warnings": self.warnings,
                "account_id": self.account_id, "country": self.country,
                "default_currency": self.default_currency,
                "charges_enabled": self.charges_enabled, "card_payments": self.card_payments}


#: The name Stripe gives a new account until someone sets one.
_PLACEHOLDER_NAME = "new business"


def clear_cache() -> None:
    _cache.clear()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


async def checkout_readiness(provider, *, currencies: set[str] | None = None,
                             fresh: bool = False) -> Readiness:
    """What stands between this Stripe account and a working Checkout page."""
    from nexus.billing.payments import PaymentError

    if getattr(provider, "name", "") != "stripe" or not getattr(provider, "configured", False):
        return Readiness()
    wanted = sorted({(c or "").upper() for c in (currencies or set())} - {""})
    digest = hashlib.sha256(provider.secret_key.encode()).hexdigest()
    pmc = str(getattr(provider, "payment_method_configuration", "") or "")
    key = f"{digest}|{pmc}|{','.join(wanted)}"
    cached = _cache.get(key)
    if cached and not fresh and time.monotonic() - cached[0] < TTL_S:
        return cached[1]

    account = await provider._get("/account")
    r = Readiness(
        account_id=str(account.get("id") or ""),
        country=str(account.get("country") or "").upper(),
        default_currency=str(account.get("default_currency") or "").upper(),
        charges_enabled=bool(account.get("charges_enabled")),
        card_payments=str((account.get("capabilities") or {}).get("card_payments") or ""),
    )
    # Activation gates LIVE money only: Stripe's test mode takes payments before an account is
    # activated, so with a test key these are warnings. Refusing there stopped a fresh account's
    # test checkout for a reason that does not apply to it (2026-10-13).
    from nexus.billing.credentials import stripe_key_mode

    live_only = r.warnings if stripe_key_mode(provider.secret_key) == "test" else r.blocking
    if not r.charges_enabled:
        due = len((account.get("requirements") or {}).get("currently_due") or [])
        live_only.append(
            "Stripe has not enabled charges on this account yet: finish activating it in the "
            "Stripe Dashboard" + (f" ({_plural(due, 'item')} still due)." if due else "."))
    if r.card_payments and r.card_payments != "active":
        live_only.append(f"Card payments are {r.card_payments} on this Stripe account, so "
                         "Checkout has no way to take a payment.")
    # Seen walking the flow in test mode (2026-10-23): with no public business name, Checkout read
    # "you authorize  to charge you" and the portal named the business "New Business", Stripe's
    # placeholder. A customer about to pay a business with no name may stop there.
    public_name = str((account.get("business_profile") or {}).get("name") or "").strip()
    if not public_name or public_name.lower() == _PLACEHOLDER_NAME:
        r.warnings.append(
            "Stripe shows customers no business name: Checkout reads \"you authorize  to charge "
            "you\" and the billing portal says \"New Business\". Set the public business name in "
            "the Stripe Dashboard (Settings > Business > Public details).")

    try:
        if pmc:
            # The configuration chosen on the Payments tab is the one Checkout sends.
            configs = [await provider._get(f"/payment_method_configurations/{pmc}")]
        else:
            configs = ((await provider._get("/payment_method_configurations?limit=20"))
                       .get("data") or [])
    except PaymentError as exc:
        configs = None
        if pmc and getattr(exc, "status", 0) == 404:
            r.blocking.append(
                f"The payment method configuration set on the Payments tab ({pmc}) is not in "
                "this Stripe account, so every Checkout would fail. Clear it, or set one from "
                "this account.")
        else:
            r.warnings.append("Could not read the account's payment method settings with this "
                              f"key (it needs Payment Method Configurations: Read): {exc}")
    if configs:
        default = (configs[0] if pmc else
                   next((c for c in configs if c.get("is_default")), None) or configs[0])
        card = default.get("card") or {}
        value = (card.get("display_preference") or {}).get("value")
        if pmc and default.get("active") is False:
            r.blocking.append(
                f"The payment method configuration set on the Payments tab ({pmc}) is switched "
                "off in Stripe, so Checkout has nothing to offer.")
        if card.get("available") is False or value == "off":
            r.blocking.append(
                "Cards are switched off in Stripe's payment method settings (Settings > Payment "
                "methods), so Checkout has nothing to offer a subscription.")

    other = [c for c in wanted if c != "INR"]
    if r.country == "IN" and other:
        r.warnings.append(
            f"This is an Indian Stripe account and plans are priced in {', '.join(other)}. Stripe "
            "lets buyers in India pay an Indian business only in INR, so a buyer in India sees "
            "\"There are no valid payment methods available\". Buyers outside India can pay once "
            "international payments are enabled on the account; for buyers in India, sell the "
            "plans in INR.")

    _cache[key] = (time.monotonic(), r)
    return r
