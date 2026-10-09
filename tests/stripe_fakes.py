# tests/stripe_fakes.py
"""An offline Stripe that knows which account every object lives in.

`NoopPaymentProvider` answers yes to every existence check, which is right for the tests that
predate accounts. These tests need the opposite: an id from another account must read as missing,
exactly as Stripe's 404 does.
"""
from __future__ import annotations

from nexus.billing.payments import NoopPaymentProvider


class TwoAccountStripe(NoopPaymentProvider):
    name = "stripe"

    def __init__(self, account: str = "acct_old") -> None:
        super().__init__()
        self.account = account
        self.owner: dict[str, str] = {}
        self.ensured: list[dict] = []

    def _own(self, object_id: str) -> str:
        self.owner[object_id] = self.account
        return object_id

    async def object_exists(self, kind: str, object_id: str) -> bool:
        return self.owner.get(object_id) == self.account

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
