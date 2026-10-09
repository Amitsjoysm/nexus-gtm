# tests/stripe_fakes.py
"""An offline Stripe that knows which account every object lives in.

`NoopPaymentProvider` answers yes to every existence check, which is right for the tests that
predate accounts. These tests need the opposite: an id from another account must read as missing,
exactly as Stripe's 404 does.

`view(account)` is the same Stripe seen through another account's key: the objects are shared,
what is visible is not. That is how a test holds the old account's key and the new one at once.
"""
from __future__ import annotations

import copy

from nexus.billing.payments import NoopPaymentProvider, PaymentError


class TwoAccountStripe(NoopPaymentProvider):
    name = "stripe"

    def __init__(self, account: str = "acct_old") -> None:
        super().__init__()
        self.account = account
        self.owner: dict[str, str] = {}
        self.ensured: list[dict] = []
        #: (account, id) for objects Stripe's own migration copied into another account, ids kept.
        self.copied: set[tuple[str, str]] = set()
        #: (account, customer id) -> the card that customer pays with in that account.
        self.cards: dict[tuple[str, str], str] = {}
        self.carried: list[dict] = []
        self.refuse_end = False

    def view(self, account: str) -> "TwoAccountStripe":
        other = copy.copy(self)
        other.account = account
        return other

    def _own(self, object_id: str) -> str:
        self.owner[object_id] = self.account
        return object_id

    def _here(self, object_id: str) -> bool:
        return (self.owner.get(object_id) == self.account
                or (self.account, object_id) in self.copied)

    def _elsewhere(self, object_id: str) -> bool:
        # An id nobody registered is treated as here, as the tests that predate accounts expect.
        return object_id in self.owner and not self._here(object_id)

    @staticmethod
    def _gone(object_id: str) -> PaymentError:
        exc = PaymentError(f"stripe /subscriptions/{object_id} -> 404: "
                           f"No such subscription: '{object_id}'")
        exc.status = 404
        return exc

    async def object_exists(self, kind: str, object_id: str) -> bool:
        return self._here(object_id)

    async def ensure_plan_price(self, *, plan_id, name, amount_cents, currency="USD",
                                interval="month", product_id=""):
        self.ensured.append({"plan_id": plan_id, "amount_cents": amount_cents,
                             "product_id": product_id, "account": self.account})
        n = len(self.ensured)
        product = product_id or self._own(f"prod_{self.account}_{plan_id}_{n}")
        price = self._own(f"price_{self.account}_{plan_id}_{amount_cents}_{n}")
        self.prices[plan_id] = {"product_id": product, "price_id": price,
                                "amount_cents": amount_cents, "name": name}
        return {"product_id": product, "price_id": price}

    async def ensure_customer(self, *, tenant_id, email, name=""):
        return self._own(f"cus_{self.account}_{tenant_id[:8]}")

    async def create_invoiced_subscription(self, **kw):
        out = await super().create_invoiced_subscription(**kw)
        self._own(out["id"])
        return out

    async def create_carried_subscription(self, **kw):
        self.carried.append({**kw, "account": self.account})
        out = await super().create_carried_subscription(**kw)
        self._own(out["id"])
        return out

    async def get_subscription(self, *, subscription_id: str) -> dict:
        return self.subscriptions.get(subscription_id, {}) if self._here(subscription_id) else {}

    async def cancel_subscription(self, *, subscription_id: str, at_period_end: bool = True):
        if self._elsewhere(subscription_id):
            raise self._gone(subscription_id)
        out = await super().cancel_subscription(subscription_id=subscription_id,
                                                at_period_end=at_period_end)
        self.cancellations[-1]["account"] = self.account
        if subscription_id in self.subscriptions:
            self.subscriptions[subscription_id] = {
                **self.subscriptions[subscription_id], "cancel_at_period_end": at_period_end,
                "status": "active" if at_period_end else "canceled"}
        return out

    async def end_carried_subscription(self, *, subscription_id: str, carried_to: str) -> dict:
        if not self._here(subscription_id):
            raise self._gone(subscription_id)
        if self.refuse_end:
            raise PaymentError(f"stripe /subscriptions/{subscription_id} -> 400: refused")
        return await super().end_carried_subscription(subscription_id=subscription_id,
                                                      carried_to=carried_to)

    async def change_subscription_price(self, *, subscription_id: str, price_id: str,
                                        plan_id: str) -> dict:
        if self._elsewhere(subscription_id):
            raise self._gone(subscription_id)
        return await super().change_subscription_price(
            subscription_id=subscription_id, price_id=price_id, plan_id=plan_id)

    async def default_payment_method(self, customer_id: str) -> str:
        return self.cards.get((self.account, customer_id), "")
