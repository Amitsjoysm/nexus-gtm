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

    @abc.abstractmethod
    async def create_checkout_session(
        self, *, tenant_id: str, plan_id: str, price_id: str, customer_id: str = "",
        success_url: str = "", cancel_url: str = "",
    ) -> dict:
        """Open a hosted subscribe flow. Returns at least ``{"id", "url"}``.

        Self-serve only. The card details never touch our infrastructure, which is the entire
        point of a hosted page: PCI scope stays at the provider. The resulting subscription
        arrives back through a webhook, not through our own write — see
        ``nexus/billing/webhooks.py``; a self-write here would diverge from the provider the
        first time the customer changed anything in the portal.
        """

    @abc.abstractmethod
    async def create_invoice(
        self, *, customer_id: str, lines: list[dict], currency: str = "USD",
        idempotency_key: str = "", description: str = "", auto_charge: bool = True,
    ) -> dict:
        """Publish a rated invoice at the provider and finalize it.

        **Our rating stays the source of truth for what is owed.** This does not ask the provider
        to compute anything — it sends the lines we already rated, so the customer receives a
        hosted invoice with a PDF and a payment link for a total our own tables can still explain.
        Letting the provider price it would put the arithmetic somewhere `reconcile.py` cannot
        check, and this codebase already refuses to let a provider be the only witness to a number.

        ``lines`` are ``{"description", "amount_cents", "quantity"}``. Returns at least
        ``{"id", "status", "hosted_url", "pdf_url", "amount_cents"}``.

        Must be idempotent on ``idempotency_key``: a retry after a timeout must not bill twice,
        which is the same rule ``charge`` follows and for the same reason.
        """

    @abc.abstractmethod
    async def get_subscription(self, *, subscription_id: str) -> dict:
        """Fetch the provider's current view of a subscription.

        Read-only. Used by reconciliation to compare their state against ours; returns ``{}``
        when the subscription is unknown so a deleted remote object reads as "nothing to
        compare" rather than raising mid-sweep.
        """

    @abc.abstractmethod
    async def create_billing_portal_session(
        self, *, customer_id: str, return_url: str = ""
    ) -> dict:
        """Open the provider's hosted self-service portal. Returns at least ``{"id", "url"}``.

        Card changes, cancellations and invoice history live there; every one of those comes
        back to us as a ``customer.subscription.*`` or ``invoice.*`` event.
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
        self.invoices: list[dict[str, Any]] = []
        self._invoices: dict[str, dict[str, Any]] = {}
        self.checkout_sessions: list[dict[str, Any]] = []
        self.portal_sessions: list[dict[str, Any]] = []
        # Stage remote state here to exercise reconciliation offline.
        self.subscriptions: dict[str, dict[str, Any]] = {}
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

    async def create_invoice(
        self, *, customer_id: str, lines: list[dict], currency: str = "USD",
        idempotency_key: str = "", description: str = "", auto_charge: bool = True,
    ) -> dict:
        if idempotency_key and idempotency_key in self._invoices:
            return self._invoices[idempotency_key]
        total = sum(int(ln.get("amount_cents", 0)) for ln in lines)
        out = {
            "id": f"noop_in_{len(self.invoices) + 1}",
            "status": "paid" if auto_charge else "open",
            # Deliberately empty rather than a fake URL. A plausible-looking link that 404s is
            # worse than a visibly absent one — the UI hides the button when there is nothing to
            # open, and a fake would put a dead link in front of a customer.
            "hosted_url": "", "pdf_url": "",
            "amount_cents": total, "currency": currency, "lines": list(lines),
        }
        self.invoices.append(out)
        if idempotency_key:
            self._invoices[idempotency_key] = out
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

    async def create_checkout_session(
        self, *, tenant_id: str, plan_id: str, price_id: str, customer_id: str = "",
        success_url: str = "", cancel_url: str = "",
    ) -> dict:
        session_id = f"noop_cs_{len(self.checkout_sessions) + 1}"
        record = {
            "id": session_id, "tenant_id": tenant_id, "plan_id": plan_id,
            "price_id": price_id, "customer_id": customer_id,
            "success_url": success_url, "cancel_url": cancel_url,
            # Mirrors what Stripe puts on the session, so the offline webhook fixtures in the
            # tests can be built from exactly the shape the real provider returns.
            "metadata": {"tenant_id": tenant_id, "plan_id": plan_id},
            "url": f"https://checkout.local.test/{session_id}",
            "provider": self.name,
        }
        self.checkout_sessions.append(record)
        return record

    async def get_subscription(self, *, subscription_id: str) -> dict:
        # Whatever a test staged in `self.subscriptions`, else unknown. Lets reconciliation be
        # exercised offline with no network and no key.
        return self.subscriptions.get(subscription_id, {})

    async def create_billing_portal_session(
        self, *, customer_id: str, return_url: str = ""
    ) -> dict:
        session_id = f"noop_ps_{len(self.portal_sessions) + 1}"
        record = {
            "id": session_id, "customer_id": customer_id, "return_url": return_url,
            "url": f"https://portal.local.test/{session_id}", "provider": self.name,
        }
        self.portal_sessions.append(record)
        return record


class StripePaymentProvider(PaymentProvider):
    """Stripe over the REST API, using the vendored HTTP client (no new dependency).

    Inert without ``NEXUS_STRIPE_SECRET_KEY``: every method raises ``PaymentNotConfigured``.
    Stripe's own ``Idempotency-Key`` header carries our key through, so a retry on our side is a
    retry on theirs and cannot double-charge.
    """

    name = "stripe"
    BASE = "https://api.stripe.com/v1"

    # Pinned, never the account default. Unpinned, Stripe can change a response shape under a
    # running deployment with no deploy on our side — and every field this module reads
    # (`status`, `current_period_end`, `hosted_invoice_url`) is a shape. Raising this is a
    # deliberate migration with the changelog read, not a dependency bump.
    API_VERSION = "2024-06-20"

    # Stripe rate-limits, and infrastructure returns 502/503/504. Retrying is safe here ONLY
    # because every mutating call already carries an Idempotency-Key, which Stripe honours: the
    # retry re-reads the original result instead of creating a second charge.
    RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        secret_key: str,
        *,
        timeout_s: float = 20.0,
        max_retries: int = 3,
        retry_base_delay_s: float = 0.5,
    ) -> None:
        self.secret_key = secret_key or ""
        self.timeout_s = timeout_s
        self.max_retries = max(1, max_retries)
        self.retry_base_delay_s = retry_base_delay_s
        # Injected by tests (httpx.MockTransport). None means a real connection.
        self._transport = None
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.secret_key)

    def _require(self) -> None:
        if not self.configured:
            raise PaymentNotConfigured(
                "Stripe is selected but NEXUS_STRIPE_SECRET_KEY is not set. "
                "Set it, or use NEXUS_PAYMENT_PROVIDER=noop."
            )

    def _headers(self, idempotency_key: str = "") -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Stripe-Version": self.API_VERSION,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _http(self):
        """One pooled client for the process.

        A fresh AsyncClient per call meant a TCP handshake and a TLS negotiation for every Stripe
        request, on the latency path of checkout and invoice collection.
        """
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_s, transport=self._transport
            )
        return self._client

    async def _request(
        self, method: str, path: str, *, form: dict[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> dict:
        """One Stripe call, retried on transient failures only.

        A 4xx other than 429 is a final answer — `card_declined` retried three times is three
        declines and a slower error — so only RETRY_STATUSES and transport errors come back here.
        The idempotency key is reused unchanged across attempts; minting a new one per attempt
        would make the retry a second, independent charge.
        """
        import asyncio
        import random

        import httpx

        client = self._http()
        headers = self._headers(idempotency_key)
        url = f"{self.BASE}{path}"
        last: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                if method == "POST":
                    resp = await client.post(url, data=form or {}, headers=headers)
                else:
                    resp = await client.get(url, headers=headers)
            except httpx.HTTPError as exc:      # connect/read timeout, reset connection
                last = PaymentError(f"stripe {path} -> transport error: {exc}")
            else:
                if resp.status_code < 400:
                    return resp.json()
                last = PaymentError(
                    f"stripe {path} -> {resp.status_code}: {resp.text[:300]}"
                )
                if resp.status_code not in self.RETRY_STATUSES:
                    raise last
            if attempt < self.max_retries - 1 and self.retry_base_delay_s:
                # Jittered, so a fleet retrying after one Stripe blip does not resynchronise
                # into a second one.
                delay = self.retry_base_delay_s * (2 ** attempt)
                await asyncio.sleep(delay * (0.5 + random.random()))
        raise last if last else PaymentError(f"stripe {path} failed")

    async def _post(self, path: str, form: dict[str, Any], idempotency_key: str = "") -> dict:
        return await self._request(
            "POST", path, form=form, idempotency_key=idempotency_key
        )

    async def _get(self, path: str) -> dict:
        return await self._request("GET", path)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

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

    async def create_checkout_session(
        self, *, tenant_id: str, plan_id: str, price_id: str, customer_id: str = "",
        success_url: str = "", cancel_url: str = "",
    ) -> dict:
        self._require()
        form: dict[str, Any] = {
            "mode": "subscription",
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": tenant_id,
            "metadata[tenant_id]": tenant_id,
            "metadata[plan_id]": plan_id,
            # Stamped on the SUBSCRIPTION too, not just the session. The session metadata is
            # only visible on `checkout.session.completed`; every later
            # `customer.subscription.*` event carries the subscription's own metadata, and
            # without this the tenant would have to be recovered by customer-id lookup on
            # every single one.
            "subscription_data[metadata][tenant_id]": tenant_id,
            "subscription_data[metadata][plan_id]": plan_id,
            # Without this Checkout shows no coupon field at all, so a discount could only be
            # given by moving the customer onto an admin-managed custom plan — the expensive path,
            # for what should be a promo code.
            "allow_promotion_codes": "true",
        }
        if customer_id:
            form["customer"] = customer_id

        from nexus.core.config import get_settings

        if getattr(get_settings(), "stripe_automatic_tax", False):
            # Opt-in: Stripe errors on this parameter unless Stripe Tax is configured on the
            # account. A rate needs an address, and Checkout does not collect one unless asked.
            form["automatic_tax[enabled]"] = "true"
            form["billing_address_collection"] = "required"
            if customer_id:
                # Only valid alongside an existing customer; sending it without one is an error.
                form["customer_update[address]"] = "auto"
        # Deliberately NO idempotency key. Checkout sessions expire, and replaying the key
        # would hand a customer a dead URL instead of a fresh one; nothing is charged by
        # creating a session, so there is no double-spend to guard against.
        data = await self._post("/checkout/sessions", form)
        return {
            "id": str(data.get("id", "")),
            "url": str(data.get("url", "")),
            "provider": self.name,
            "customer_id": str(data.get("customer") or customer_id or ""),
            "tenant_id": tenant_id,
            "plan_id": plan_id,
        }

    async def create_invoice(
        self, *, customer_id: str, lines: list[dict], currency: str = "USD",
        idempotency_key: str = "", description: str = "", auto_charge: bool = True,
    ) -> dict:
        """Draft → line items → finalize → (optionally) pay.

        Stripe builds an invoice from *pending* items on the customer, so the order matters and is
        the opposite of what it looks like: create the invoice with ``pending_invoice_items_behavior
        = exclude``, then attach each item to that invoice id explicitly. Adding items first and
        creating the invoice after sweeps in anything else pending on that customer — including
        items a subscription put there — and bills it on our usage invoice.
        """
        self._require()
        cur = (currency or "USD").lower()

        invoice = await self._post(
            "/invoices",
            {
                "customer": customer_id,
                "currency": cur,
                "collection_method": "charge_automatically" if auto_charge else "send_invoice",
                # Our own invoice id, so a retry finds the same object and `reconcile.py` can tie
                # the two sides together without guessing.
                "metadata[nexus_invoice_id]": idempotency_key,
                "description": description,
                "auto_advance": "false",
                "pending_invoice_items_behavior": "exclude",
                **({} if auto_charge else {"days_until_due": "14"}),
            },
            idempotency_key=f"inv:{idempotency_key}" if idempotency_key else "",
        )
        invoice_id = str(invoice.get("id", ""))

        for i, ln in enumerate(lines):
            amount = int(ln.get("amount_cents", 0))
            if amount == 0:
                continue        # Stripe rejects a zero-amount item; a $0 line carries no meaning
            await self._post(
                "/invoiceitems",
                {
                    "customer": customer_id, "invoice": invoice_id, "currency": cur,
                    "amount": str(amount),
                    "description": str(ln.get("description") or "Usage")[:250],
                },
                idempotency_key=f"invitem:{idempotency_key}:{i}" if idempotency_key else "",
            )

        final = await self._post(f"/invoices/{invoice_id}/finalize", {"auto_advance": "false"})
        if auto_charge and final.get("status") == "open":
            try:
                final = await self._post(f"/invoices/{invoice_id}/pay", {})
            except PaymentError as exc:
                # The invoice EXISTS and is finalized — the customer can still pay it from the
                # hosted page. Reporting this as a failed creation would hide a real, payable
                # invoice and invite a second one to be raised for the same period.
                logger.warning("stripe invoice %s finalized but payment failed: %s",
                               invoice_id, exc)
        return {
            "id": invoice_id,
            "status": str(final.get("status", "")),
            "hosted_url": str(final.get("hosted_invoice_url") or ""),
            "pdf_url": str(final.get("invoice_pdf") or ""),
            "amount_cents": int(final.get("total") or 0),
            "currency": str(final.get("currency") or cur).upper(),
        }

    async def get_subscription(self, *, subscription_id: str) -> dict:
        self._require()
        try:
            return await self._get(f"/subscriptions/{subscription_id}")
        except PaymentError:
            # A 404 means the subscription is gone at the provider. That is itself a drift
            # finding, not a sweep-breaking error.
            return {}

    async def create_billing_portal_session(
        self, *, customer_id: str, return_url: str = ""
    ) -> dict:
        self._require()
        form: dict[str, Any] = {"customer": customer_id}
        if return_url:
            form["return_url"] = return_url
        data = await self._post("/billing_portal/sessions", form)
        return {
            "id": str(data.get("id", "")),
            "url": str(data.get("url", "")),
            "provider": self.name,
            "customer_id": customer_id,
        }

    async def fetch_subscription(self, subscription_id: str) -> dict:
        """Read one subscription back from Stripe. Used by the reconciliation job.

        Not on the ABC: reconciling against a provider that holds no state (``noop``) is
        meaningless, and forcing a synthetic answer out of it would make drift-reporting lie.
        The reconciler feature-detects this method instead.
        """
        self._require()
        return await self._get(f"/subscriptions/{subscription_id}")


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
    global _provider, _resolved_at
    _provider = provider
    _resolved_at = 0.0


# How long a resolved provider is reused before the managed credential is re-read. Mirrors
# `providers/resolver.POOL_TTL_S`, and for the same reason: the worker is a separate process, so
# without a TTL a credential changed in the panel would need a restart — which is the redeploy this
# whole surface exists to remove.
_CREDENTIAL_TTL_S = 30.0
_resolved_at = 0.0


async def resolve_payment_provider() -> PaymentProvider:
    """The provider to bill with, preferring an operator-managed credential over the environment.

    Async because reading the managed credential is a database call. `get_payment_provider()` stays
    for the synchronous callers and for tests that inject a double — an explicit override always
    wins here, so `set_payment_provider` still does what it says.
    """
    global _provider, _resolved_at
    import time

    if _provider is not None and (time.monotonic() - _resolved_at) < _CREDENTIAL_TTL_S:
        return _provider

    from nexus.core.config import get_settings

    s = get_settings()
    if s.payment_provider != "stripe":
        # Nothing to resolve: the noop provider has no credential. Build it the old way so a
        # deployment with no payment provider never touches the credentials table.
        if _provider is None:
            _provider = build_payment_provider_from_settings()
        return _provider

    try:
        from nexus.billing.credentials import resolve_stripe_secrets

        secret, _hook, _pub = await resolve_stripe_secrets()
    except Exception:
        # Falling back rather than raising: losing the ability to bill because a lookup hiccuped
        # is worse than billing with the configuration we already had.
        logger.warning("could not resolve the managed payment credential", exc_info=True)
        secret = s.stripe_secret_key

    _provider = StripePaymentProvider(secret)
    _resolved_at = time.monotonic()
    return _provider
