# nexus/billing/payments.py
"""Payment-provider seam: interface + offline default + real adapter + registry.

Mirrors every other provider seam in this repo (LLM, search, CRM, SEP, telephony, network
connectors): application code asks for ``get_payment_provider()`` and never names Stripe.

The default is ``noop``, which records intent and returns synthetic ids. That is not a stub for
its own sake — it lets the whole subscription lifecycle run end to end offline, which is what
docs/billing/16-Testing-Strategy.md §2 requires of the money rails.

The Stripe adapter is **inert until configured**. With no key it raises a clear
``PaymentNotConfigured`` rather than silently pretending to charge, the same rule the network
connectors follow: a provider that isn't set up must fail loudly, never fake success.

Deliberately NOT here: webhook handling and dunning. A webhook endpoint that has never received
a signed event is not something to claim as working, and both need live keys to verify
(docs/billing/17-Production-Checklist.md §Money rails).
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("nexus.billing.payments")


class PaymentError(RuntimeError):
    """Base class for payment-provider failures."""


class PaymentNotConfigured(PaymentError):
    """The selected provider has no credentials. Never raised by the noop provider."""


@dataclass
class PaymentResult:
    """Outcome of a money movement. ``provider`` names who actually handled it, so a support
    question ("did this really hit Stripe?") is answerable from the record alone."""

    ok: bool
    provider: str
    reference: str = ""
    amount_cents: int = 0
    currency: str = "USD"
    detail: dict = field(default_factory=dict)


class PaymentProvider(abc.ABC):
    """What the billing engine needs from a payment processor."""

    name = "base"

    @abc.abstractmethod
    async def ensure_customer(self, *, tenant_id: str, email: str, name: str = "") -> str:
        """Return a stable provider-side customer id for this tenant."""

    @abc.abstractmethod
    async def attach_payment_method(
        self, *, customer_id: str, payment_method_id: str, set_default: bool = True
    ) -> bool:
        """Attach a card to the customer and (by default) make it the one we bill.

        Without this an off-session charge has nothing to draw on, so it is part of the
        interface rather than a Stripe-only detail.
        """

    @abc.abstractmethod
    async def charge(
        self, *, customer_id: str, amount_cents: int, currency: str, idempotency_key: str,
        description: str = "",
    ) -> PaymentResult:
        """Collect ``amount_cents``. Must be idempotent on ``idempotency_key``."""

    @abc.abstractmethod
    async def refund(
        self, *, reference: str, amount_cents: int, idempotency_key: str
    ) -> PaymentResult:
        """Return money already collected."""

    @abc.abstractmethod
    async def ensure_plan_price(
        self, *, plan_id: str, name: str, amount_cents: int, currency: str = "USD",
        interval: str = "month",
    ) -> dict:
        """Publish a plan as a product + recurring price at the provider.

        Needed so a bespoke, per-customer price negotiated in Admin exists as a real object at
        the PSP, rather than only inside our own tables. Returns ``{"product_id", "price_id"}``.
        """


class NoopPaymentProvider(PaymentProvider):
    """Offline default. Records intent, moves no money, never fails.

    Every call is retained on the instance so tests can assert what *would* have been charged
    without a network or a key.
    """

    name = "noop"

    def __init__(self) -> None:
        self.customers: dict[str, str] = {}
        self.payment_methods: dict[str, str] = {}
        self.prices: dict[str, dict[str, Any]] = {}
        self.charges: list[dict[str, Any]] = []
        self.refunds: list[dict[str, Any]] = []
        self._seen: dict[str, PaymentResult] = {}

    async def ensure_customer(self, *, tenant_id: str, email: str, name: str = "") -> str:
        cid = self.customers.setdefault(tenant_id, f"noop_cus_{tenant_id[:12]}")
        return cid

    async def attach_payment_method(
        self, *, customer_id: str, payment_method_id: str, set_default: bool = True
    ) -> bool:
        self.payment_methods[customer_id] = payment_method_id
        return True

    async def charge(
        self, *, customer_id: str, amount_cents: int, currency: str, idempotency_key: str,
        description: str = "",
    ) -> PaymentResult:
        if idempotency_key in self._seen:      # replay returns the original, never a second charge
            return self._seen[idempotency_key]
        record = {
            "customer_id": customer_id, "amount_cents": amount_cents, "currency": currency,
            "idempotency_key": idempotency_key, "description": description,
        }
        self.charges.append(record)
        result = PaymentResult(
            ok=True, provider=self.name, reference=f"noop_ch_{len(self.charges)}",
            amount_cents=amount_cents, currency=currency, detail=record,
        )
        self._seen[idempotency_key] = result
        return result

    async def ensure_plan_price(
        self, *, plan_id: str, name: str, amount_cents: int, currency: str = "USD",
        interval: str = "month",
    ) -> dict:
        out = {"product_id": f"noop_prod_{plan_id}", "price_id": f"noop_price_{plan_id}"}
        self.prices[plan_id] = {**out, "amount_cents": amount_cents, "name": name}
        return out

    async def refund(
        self, *, reference: str, amount_cents: int, idempotency_key: str
    ) -> PaymentResult:
        if idempotency_key in self._seen:
            return self._seen[idempotency_key]
        record = {"reference": reference, "amount_cents": amount_cents}
        self.refunds.append(record)
        result = PaymentResult(
            ok=True, provider=self.name, reference=f"noop_re_{len(self.refunds)}",
            amount_cents=amount_cents, detail=record,
        )
        self._seen[idempotency_key] = result
        return result


class StripePaymentProvider(PaymentProvider):
    """Stripe over the REST API, using the vendored HTTP client (no new dependency).

    Inert without ``NEXUS_STRIPE_SECRET_KEY``: every method raises ``PaymentNotConfigured``.
    Stripe's own ``Idempotency-Key`` header carries our key through, so a retry on our side is a
    retry on theirs and cannot double-charge.
    """

    name = "stripe"
    BASE = "https://api.stripe.com/v1"

    def __init__(self, secret_key: str, *, timeout_s: float = 20.0) -> None:
        self.secret_key = secret_key or ""
        self.timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return bool(self.secret_key)

    def _require(self) -> None:
        if not self.configured:
            raise PaymentNotConfigured(
                "Stripe is selected but NEXUS_STRIPE_SECRET_KEY is not set. "
                "Set it, or use NEXUS_PAYMENT_PROVIDER=noop."
            )

    async def _post(self, path: str, form: dict[str, Any], idempotency_key: str = "") -> dict:
        import httpx

        headers = {"Authorization": f"Bearer {self.secret_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.post(f"{self.BASE}{path}", data=form, headers=headers)
        if resp.status_code >= 400:
            raise PaymentError(f"stripe {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def _get(self, path: str) -> dict:
        import httpx

        headers = {"Authorization": f"Bearer {self.secret_key}"}
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            resp = await client.get(f"{self.BASE}{path}", headers=headers)
        if resp.status_code >= 400:
            raise PaymentError(f"stripe {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def _default_payment_method(self, customer_id: str) -> str:
        """The card we bill. An off-session PaymentIntent does not read invoice_settings on its
        own, so it has to be passed explicitly."""
        cust = await self._get(f"/customers/{customer_id}")
        pm = (cust.get("invoice_settings") or {}).get("default_payment_method")
        if pm:
            return str(pm)
        listed = await self._get(f"/payment_methods?customer={customer_id}&type=card&limit=1")
        data = listed.get("data") or []
        return str(data[0]["id"]) if data else ""

    async def ensure_customer(self, *, tenant_id: str, email: str, name: str = "") -> str:
        self._require()
        data = await self._post(
            "/customers",
            {"email": email, "name": name or email, "metadata[tenant_id]": tenant_id},
            idempotency_key=f"cus:{tenant_id}",
        )
        return str(data.get("id", ""))

    async def attach_payment_method(
        self, *, customer_id: str, payment_method_id: str, set_default: bool = True
    ) -> bool:
        self._require()
        attached = await self._post(
            f"/payment_methods/{payment_method_id}/attach", {"customer": customer_id}
        )
        # Attaching a test token (`pm_card_visa`) mints a concrete PaymentMethod with a NEW id.
        # Setting the default to the string we were handed would reference something that is
        # not attached to this customer, and Stripe rejects it.
        real_id = str(attached.get("id") or payment_method_id)
        if set_default:
            await self._post(
                f"/customers/{customer_id}",
                {"invoice_settings[default_payment_method]": real_id},
            )
        return True

    async def charge(
        self, *, customer_id: str, amount_cents: int, currency: str, idempotency_key: str,
        description: str = "",
    ) -> PaymentResult:
        self._require()
        form: dict[str, Any] = {
            "amount": int(amount_cents), "currency": currency.lower(),
            "customer": customer_id, "confirm": "true",
            "off_session": "true", "description": description,
        }
        pm = await self._default_payment_method(customer_id)
        if pm:
            form["payment_method"] = pm
        data = await self._post(
            "/payment_intents", form, idempotency_key=idempotency_key
        )
        return PaymentResult(
            ok=data.get("status") in ("succeeded", "processing"),
            provider=self.name, reference=str(data.get("id", "")),
            amount_cents=int(amount_cents), currency=currency, detail=data,
        )

    async def ensure_plan_price(
        self, *, plan_id: str, name: str, amount_cents: int, currency: str = "USD",
        interval: str = "month",
    ) -> dict:
        self._require()
        # Keyed on the plan id, so re-publishing the same custom plan reuses the product
        # instead of littering the account with duplicates.
        product = await self._post(
            "/products",
            {"name": name, "metadata[plan_id]": plan_id},
            idempotency_key=f"product:{plan_id}",
        )
        product_id = str(product.get("id", ""))
        price = await self._post(
            "/prices",
            {
                "product": product_id, "unit_amount": int(amount_cents),
                "currency": currency.lower(), "recurring[interval]": interval,
                "metadata[plan_id]": plan_id,
            },
            # Amount is in the key: changing the price must mint a NEW Stripe price object,
            # because Stripe prices are immutable once created.
            idempotency_key=f"price:{plan_id}:{amount_cents}:{currency}:{interval}",
        )
        return {"product_id": product_id, "price_id": str(price.get("id", ""))}

    async def refund(
        self, *, reference: str, amount_cents: int, idempotency_key: str
    ) -> PaymentResult:
        self._require()
        data = await self._post(
            "/refunds",
            {"payment_intent": reference, "amount": int(amount_cents)},
            idempotency_key=idempotency_key,
        )
        return PaymentResult(
            ok=data.get("status") in ("succeeded", "pending"),
            provider=self.name, reference=str(data.get("id", "")),
            amount_cents=int(amount_cents), detail=data,
        )


_provider: PaymentProvider | None = None


def build_payment_provider_from_settings() -> PaymentProvider:
    from nexus.core.config import get_settings

    s = get_settings()
    if s.payment_provider == "stripe":
        return StripePaymentProvider(s.stripe_secret_key)
    return NoopPaymentProvider()


def get_payment_provider() -> PaymentProvider:
    global _provider
    if _provider is None:
        _provider = build_payment_provider_from_settings()
    return _provider


def set_payment_provider(provider: PaymentProvider | None) -> None:
    """Test/runtime override. Passing None restores selection from settings."""
    global _provider
    _provider = provider
