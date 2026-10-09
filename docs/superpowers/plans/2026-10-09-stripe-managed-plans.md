# Stripe-managed plans and subscriptions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stripe ids that remember their Stripe account (fixing "No such price/product" after a key
change), plans that live in Stripe, and superadmin-only Stripe subscriptions (start by invoice or
Checkout link, prorated plan change), without changing any existing behaviour.

**Architecture:** One new module, `nexus/billing/stripe_catalog.py`, owns "which Stripe id is
usable in the CURRENT account" for plans and customers; checkout, collection and the admin plan
endpoints call it. A second, `nexus/billing/stripe_subscriptions.py`, owns starting and moving
Stripe subscriptions from the console. The provider seam (`payments.py`) gains non-abstract calls
with safe defaults, so every existing provider and test double keeps working. A new platform
permission `stripe.manage` (superadmin preset only, migration `0067` backfill) gates all of it.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, Pydantic v2, httpx against the Stripe REST API,
pytest (asyncio_mode=auto), React 18 + TypeScript + CSS Modules for the console.

**Spec:** `docs/superpowers/specs/2026-10-09-stripe-managed-plans-design.md`.

**Run tests from the worktree root** (`.claude/worktrees/stripe-managed-plans`). Iterate with the
local interpreter, which imports this worktree's `nexus` (verified):
`python -m pytest <file> -n0 -q -p no:cacheprovider`. Task 12 runs the CI-equivalent on Python 3.11.

---

## File map

| File | Responsibility |
|---|---|
| `nexus/billing/permissions.py` (modify) | `STRIPE_MANAGE`, frozen pre-permission sets, `widened_with` |
| `migrations/versions/0067_stripe_manage.py` (create) | backfill `stripe.manage` into full superadmin rows |
| `nexus/billing/payments.py` (modify) | provider seam: account id, existence check, archive, rename, invoiced subscription, price swap, product reuse, HTTP status on errors |
| `nexus/billing/stripe_catalog.py` (create) | account-aware plan price and customer ids, archive/rename, plan→provider sync |
| `nexus/billing/stripe_subscriptions.py` (create) | start a Stripe subscription, change a Stripe-billed plan |
| `nexus/api/routers/billing.py` (modify) | checkout uses the catalog |
| `nexus/billing/collection.py` (modify) | invoice collection uses the catalog for the customer |
| `nexus/api/routers/admin_billing_write.py` (modify) | plan endpoints → `stripe.manage` + provider sync; sync endpoint; start endpoint; plan-change routing |
| `nexus/api/routers/admin_billing.py` (modify) | Stripe fields on plans; `stripe_billed` on subscriptions |
| `nexus/api/routers/admin_payment_credentials.py` (modify) | sync plans after a superadmin activates a credential |
| `nexus/billing/webhooks.py` (modify) | defer credits for invoiced subscriptions until `invoice.paid` |
| `frontend/src/lib/{permissions,types,api}.ts` (modify) | permission, types, client calls |
| `frontend/src/pages/AdminBillingPage.tsx` (modify) | Plans tab gates, Stripe column, Sync button |
| `frontend/src/pages/admin/TenantActionsDialog.tsx` (modify) | Stripe-billed plan change copy; Start a Stripe subscription |
| `frontend/src/pages/admin/PaymentsTab.tsx` (modify) | show the sync report after activation |
| `tests/stripe_fakes.py` (create) | `TwoAccountStripe`, an offline Stripe that knows each object's account |
| `tests/test_stripe_manage_permission.py`, `tests/test_stripe_catalog_adapter.py`, `tests/test_stripe_catalog.py`, `tests/test_stripe_managed_plans.py`, `tests/test_stripe_invoice_credits.py`, `tests/test_stripe_admin_subscriptions.py`, `tests/test_stripe_managed_ui.py` (create) | tests |
| `tests/test_plan_authoring.py`, `tests/test_admin_permissions.py` (modify) | encode the two decided behaviour changes |
| `CLAUDE.md`, `docs/billing/stripe-connection-guide.txt` (modify) | docs |

---

### Task 1: The `stripe.manage` permission and its backfill

**Files:**
- Modify: `nexus/billing/permissions.py`
- Create: `migrations/versions/0067_stripe_manage.py`
- Modify: `frontend/src/lib/permissions.ts`
- Test: `tests/test_stripe_manage_permission.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stripe_manage_permission.py
"""`stripe.manage`: creating and changing objects at Stripe is the superadmin's alone (2026-10-09)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from nexus.billing.permissions import (
    ALL_PERMISSIONS,
    FEATURES_MANAGE,
    PERMISSIONS_BEFORE_FEATURES_MANAGE,
    PERMISSIONS_BEFORE_STRIPE_MANAGE,
    ROLE_PRESETS,
    STRIPE_MANAGE,
    widened_with,
)

ROOT = Path(__file__).resolve().parents[1]


def test_only_the_superadmin_preset_holds_it():
    assert STRIPE_MANAGE == "stripe.manage"
    assert STRIPE_MANAGE in ALL_PERMISSIONS
    assert STRIPE_MANAGE in ROLE_PRESETS["superadmin"]
    assert STRIPE_MANAGE not in ROLE_PRESETS["finance"]
    assert STRIPE_MANAGE not in ROLE_PRESETS["support"]


def test_the_features_manage_backfill_set_stays_frozen():
    """0055 matched rows against the permissions that existed before `features.manage`. Deriving
    that set from ALL_PERMISSIONS would now include `stripe.manage`, and a database upgrading
    across both migrations would get neither grant."""
    assert FEATURES_MANAGE not in PERMISSIONS_BEFORE_FEATURES_MANAGE
    assert STRIPE_MANAGE not in PERMISSIONS_BEFORE_FEATURES_MANAGE
    assert set(PERMISSIONS_BEFORE_STRIPE_MANAGE) == set(ALL_PERMISSIONS) - {STRIPE_MANAGE}


def test_a_full_admin_gains_the_new_permission():
    held = list(PERMISSIONS_BEFORE_STRIPE_MANAGE)
    assert widened_with(held, STRIPE_MANAGE, PERMISSIONS_BEFORE_STRIPE_MANAGE) == sorted(
        set(held) | {STRIPE_MANAGE}
    )


def test_a_narrowed_admin_stays_narrowed():
    held = [p for p in PERMISSIONS_BEFORE_STRIPE_MANAGE if p != "pricing.write"]
    assert widened_with(held, STRIPE_MANAGE, PERMISSIONS_BEFORE_STRIPE_MANAGE) is None


def test_an_empty_list_and_an_existing_grant_are_left_alone():
    assert widened_with([], STRIPE_MANAGE, PERMISSIONS_BEFORE_STRIPE_MANAGE) is None
    assert widened_with(list(ALL_PERMISSIONS), STRIPE_MANAGE,
                        PERMISSIONS_BEFORE_STRIPE_MANAGE) is None


def test_the_migration_sits_on_the_previous_head():
    path = ROOT / "migrations" / "versions" / "0067_stripe_manage.py"
    spec = importlib.util.spec_from_file_location("m0067", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0067_stripe_manage"
    assert mod.down_revision == "0066_page_visibility"


def test_the_console_names_the_permission():
    src = (ROOT / "frontend" / "src" / "lib" / "permissions.ts").read_text(encoding="utf-8")
    assert 'export const STRIPE_MANAGE = "stripe.manage";' in src
    assert "[STRIPE_MANAGE]:" in src
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_stripe_manage_permission.py -n0 -q -p no:cacheprovider`
Expected: FAIL, `ImportError: cannot import name 'PERMISSIONS_BEFORE_STRIPE_MANAGE'`.

- [ ] **Step 3: Implement in `nexus/billing/permissions.py`**

After the `FEATURES_MANAGE = "features.manage"` line add:

```python
# Create and change objects AT THE PAYMENT PROVIDER: a plan's Stripe product and price, a
# subscription started from the console, a plan change that moves what Stripe charges. Superadmin
# only (product owner, 2026-10-09): each one moves a real customer's money, and the console cannot
# undo it the way it can undo a rate card. Finance keeps rate cards and entitlements
# (`pricing.write`) and NEXUS-only plan changes (`subscriptions.write`).
STRIPE_MANAGE = "stripe.manage"
```

Append `STRIPE_MANAGE` to the `ALL_PERMISSIONS` tuple (last element). Replace the
`PERMISSIONS_BEFORE_FEATURES_MANAGE` assignment with:

```python
# Every permission that existed BEFORE `features.manage` was added. Migration 0055 uses it to
# recognise a stored superadmin row that held everything at the time it was written. FROZEN: it
# excludes every permission added since, or a database upgrading across 0055 and a later backfill
# would match neither.
PERMISSIONS_BEFORE_FEATURES_MANAGE = tuple(
    p for p in ALL_PERMISSIONS if p not in (FEATURES_MANAGE, STRIPE_MANAGE)
)
# The same, for migration 0067.
PERMISSIONS_BEFORE_STRIPE_MANAGE = tuple(p for p in ALL_PERMISSIONS if p != STRIPE_MANAGE)


def widened_with(held: list, new_permission: str, before: tuple) -> list | None:
    """The stored permission list once ``new_permission`` exists, or None to leave the row alone.

    A row holding every permission in ``before`` meant "everything" when it was written, so a
    permission added later belongs to it. A row missing even one was deliberately narrowed and
    stays exactly as it is. An empty list resolves through the role preset and needs nothing.
    """
    if not held or new_permission in held:
        return None
    if not set(before).issubset(set(held)):
        return None
    return sorted(set(held) | {new_permission})
```

- [ ] **Step 4: Create `migrations/versions/0067_stripe_manage.py`**

```python
"""stripe.manage: creating plans and subscriptions at Stripe is superadmin-only

No schema change. `platform_admins.permissions` stores the EXPANDED set, so a new permission
reaches nobody with a stored list: backfill it into rows that held every permission existing
before it, exactly as 0055 did for `features.manage`. A narrowed admin stays narrowed.

Revision ID: 0067_stripe_manage
Revises: 0066_page_visibility
"""
from alembic import op
import sqlalchemy as sa

revision = "0067_stripe_manage"
down_revision = "0066_page_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import json

    from nexus.billing.permissions import (
        PERMISSIONS_BEFORE_STRIPE_MANAGE,
        STRIPE_MANAGE,
        widened_with,
    )

    bind = op.get_bind()
    try:
        rows = bind.execute(sa.text("SELECT id, permissions FROM platform_admins")).fetchall()
    except Exception:
        return  # table absent on a partial chain; nothing to backfill
    for row_id, raw in rows:
        held = raw if isinstance(raw, list) else (json.loads(raw) if raw else [])
        if not isinstance(held, list):
            continue
        widened = widened_with(held, STRIPE_MANAGE, PERMISSIONS_BEFORE_STRIPE_MANAGE)
        if widened is not None:
            bind.execute(
                sa.text("UPDATE platform_admins SET permissions = :p WHERE id = :i"),
                {"p": json.dumps(widened), "i": row_id},
            )


def downgrade() -> None:
    # Nothing to undo in the schema. A granted permission left in a row is harmless on an older
    # release, which simply does not check it.
    pass
```

- [ ] **Step 5: Mirror it in `frontend/src/lib/permissions.ts`**

After the `FEATURES_MANAGE` export add:

```ts
/**
 * Create and change objects at Stripe: plans, prices, subscriptions started from the console and
 * Stripe plan changes. Superadmin only, mirroring the server.
 */
export const STRIPE_MANAGE = "stripe.manage";
```

and in `PERMISSION_LABELS` add `[STRIPE_MANAGE]: "Manage Stripe plans & subscriptions",`.

- [ ] **Step 6: Run the new test and the permission tests**

Run: `python -m pytest tests/test_stripe_manage_permission.py tests/test_admin_permissions.py tests/test_billing_platform_admins.py tests/test_prospecting_schema.py -n0 -q -p no:cacheprovider`
Expected: all PASS (the preset-equals-ALL_PERMISSIONS test still holds; one alembic head).

- [ ] **Step 7: Commit**

```bash
git add nexus/billing/permissions.py migrations/versions/0067_stripe_manage.py frontend/src/lib/permissions.ts tests/test_stripe_manage_permission.py
git commit -m "feat(billing): stripe.manage, superadmin only, with its 0067 backfill"
```

---

### Task 2: The provider seam learns accounts, archiving, renaming and subscriptions

**Files:**
- Modify: `nexus/billing/payments.py`
- Test: `tests/test_stripe_catalog_adapter.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stripe_catalog_adapter.py
"""The Stripe calls behind Stripe-managed plans and subscriptions, checked at the wire."""
from __future__ import annotations

import httpx
import pytest

from nexus.billing.payments import (
    CREDITS_META_KEY,
    CREDITS_ON_PAYMENT,
    NoopPaymentProvider,
    PaymentError,
    StripePaymentProvider,
)


def _fake(monkeypatch, responder):
    calls: list = []

    async def fake_request(self, method, path, *, form=None, idempotency_key=""):
        calls.append((method, path, dict(form or {}), idempotency_key))
        return responder(method, path, dict(form or {}))

    monkeypatch.setattr(StripePaymentProvider, "_request", fake_request)
    return calls


def _error(status: int, text: str) -> PaymentError:
    err = PaymentError(text)
    err.status = status
    return err


async def test_the_http_status_rides_on_the_error():
    p = StripePaymentProvider("sk_test_x", max_retries=1)
    p._transport = httpx.MockTransport(
        lambda req: httpx.Response(404, json={"error": {"message": "No such price"}})
    )
    with pytest.raises(PaymentError) as exc:
        await p._get("/prices/price_gone")
    assert exc.value.status == 404


async def test_the_account_is_read_once_per_key(monkeypatch):
    from nexus.billing import payments

    payments._account_ids.clear()
    calls = _fake(monkeypatch, lambda m, p, f: {"id": "acct_one"})
    assert await StripePaymentProvider("sk_test_acct1").account_id() == "acct_one"
    assert await StripePaymentProvider("sk_test_acct1").account_id() == "acct_one"
    assert [c[1] for c in calls] == ["/account"]


async def test_a_404_means_the_object_is_not_in_this_account(monkeypatch):
    def responder(m, p, f):
        raise _error(404, "stripe /prices/price_old -> 404: No such price")

    _fake(monkeypatch, responder)
    assert await StripePaymentProvider("sk_test_x").object_exists("prices", "price_old") is False


async def test_any_other_failure_is_not_read_as_missing(monkeypatch):
    def responder(m, p, f):
        raise _error(401, "stripe /prices/x -> 401: invalid key")

    _fake(monkeypatch, responder)
    with pytest.raises(PaymentError):
        await StripePaymentProvider("sk_test_x").object_exists("prices", "x")


async def test_a_deleted_customer_reads_as_missing(monkeypatch):
    _fake(monkeypatch, lambda m, p, f: {"id": "cus_1", "deleted": True})
    assert await StripePaymentProvider("sk_test_x").object_exists("customers", "cus_1") is False


async def test_a_known_product_gets_a_new_price_and_no_new_product(monkeypatch):
    calls = _fake(monkeypatch, lambda m, p, f: {"id": "price_new"})
    refs = await StripePaymentProvider("sk_test_x").ensure_plan_price(
        plan_id="launch", name="Launch", amount_cents=9900, product_id="prod_keep",
    )
    assert refs == {"product_id": "prod_keep", "price_id": "price_new"}
    assert [c[1] for c in calls] == ["/prices"]
    assert calls[0][2]["product"] == "prod_keep"


async def test_archiving_and_renaming(monkeypatch):
    calls = _fake(monkeypatch, lambda m, p, f: {"id": "x"})
    p = StripePaymentProvider("sk_test_x")
    await p.set_price_active(price_id="price_1", active=False)
    await p.rename_product(product_id="prod_1", name="Scale")
    assert calls[0][:3] == ("POST", "/prices/price_1", {"active": "false"})
    assert calls[1][:3] == ("POST", "/products/prod_1", {"name": "Scale"})


async def test_an_invoiced_subscription_is_created_finalized_and_sent(monkeypatch):
    def responder(m, p, f):
        if p == "/subscriptions":
            return {"id": "sub_1", "status": "active", "customer": "cus_1",
                    "latest_invoice": "in_1", "collection_method": "send_invoice",
                    "current_period_start": 1, "current_period_end": 2}
        if p == "/invoices/in_1/finalize":
            return {"id": "in_1", "status": "open"}
        if p == "/invoices/in_1/send":
            return {"id": "in_1", "status": "open",
                    "hosted_invoice_url": "https://invoice.stripe.com/i/1"}
        raise AssertionError(p)

    calls = _fake(monkeypatch, responder)
    out = await StripePaymentProvider("sk_test_x").create_invoiced_subscription(
        customer_id="cus_1", price_id="price_1", tenant_id="t1", plan_id="launch",
        days_until_due=14, idempotency_key="k1",
    )
    form = calls[0][2]
    assert form["collection_method"] == "send_invoice"
    assert form["days_until_due"] == "14"
    assert form["items[0][price]"] == "price_1"
    assert form["metadata[tenant_id]"] == "t1" and form["metadata[plan_id]"] == "launch"
    assert form[f"metadata[{CREDITS_META_KEY}]"] == CREDITS_ON_PAYMENT
    assert calls[0][3] == "k1"
    assert [c[1] for c in calls] == ["/subscriptions", "/invoices/in_1/finalize",
                                     "/invoices/in_1/send"]
    assert out["id"] == "sub_1"
    assert out["hosted_invoice_url"] == "https://invoice.stripe.com/i/1"
    assert out["collection_method"] == "send_invoice"


async def test_an_invoice_that_cannot_be_sent_does_not_lose_the_subscription(monkeypatch):
    def responder(m, p, f):
        if p == "/subscriptions":
            return {"id": "sub_2", "status": "active", "customer": "cus_1",
                    "latest_invoice": "in_2"}
        raise _error(400, "stripe /invoices/in_2/finalize -> 400: already finalized")

    _fake(monkeypatch, responder)
    out = await StripePaymentProvider("sk_test_x").create_invoiced_subscription(
        customer_id="cus_1", price_id="p", tenant_id="t", plan_id="launch",
        days_until_due=14, idempotency_key="k2",
    )
    assert out["id"] == "sub_2"
    assert "already finalized" in out["invoice_note"]


async def test_a_price_change_moves_the_single_item_and_invoices_the_difference(monkeypatch):
    def responder(m, p, f):
        if m == "GET":
            return {"id": "sub_1", "items": {"data": [{"id": "si_1"}]}}
        return {"id": "sub_1", "status": "active", "collection_method": "charge_automatically"}

    calls = _fake(monkeypatch, responder)
    await StripePaymentProvider("sk_test_x").change_subscription_price(
        subscription_id="sub_1", price_id="price_2", plan_id="accelerate",
    )
    assert calls[1][:2] == ("POST", "/subscriptions/sub_1")
    assert calls[1][2] == {
        "items[0][id]": "si_1", "items[0][price]": "price_2",
        "proration_behavior": "always_invoice", "metadata[plan_id]": "accelerate",
    }


async def test_a_multi_item_subscription_is_refused(monkeypatch):
    _fake(monkeypatch, lambda m, p, f: {"id": "sub_1", "items": {"data": [{"id": "a"},
                                                                        {"id": "b"}]}})
    with pytest.raises(PaymentError):
        await StripePaymentProvider("sk_test_x").change_subscription_price(
            subscription_id="sub_1", price_id="p", plan_id="x",
        )


async def test_checkout_reports_when_its_link_expires(monkeypatch):
    _fake(monkeypatch, lambda m, p, f: {"id": "cs_1", "url": "https://checkout.stripe.com/c/1",
                                        "expires_at": 1999999999})
    out = await StripePaymentProvider("sk_test_x").create_checkout_session(
        tenant_id="t", plan_id="launch", price_id="price_1",
    )
    assert out["expires_at"] == 1999999999


async def test_the_offline_provider_supports_every_new_call():
    p = NoopPaymentProvider()
    assert await p.account_id() == "noop"
    assert await p.object_exists("prices", "anything") is True
    refs = await p.ensure_plan_price(plan_id="launch", name="Launch", amount_cents=9900,
                                     product_id="prod_x")
    assert refs["product_id"] == "prod_x"
    await p.set_price_active(price_id=refs["price_id"], active=False)
    assert p.price_active[refs["price_id"]] is False
    await p.rename_product(product_id="prod_x", name="Launch Plus")
    assert p.renames[-1] == {"product_id": "prod_x", "name": "Launch Plus"}
    sub = await p.create_invoiced_subscription(
        customer_id="c", price_id=refs["price_id"], tenant_id="t", plan_id="launch",
        days_until_due=14, idempotency_key="k",
    )
    again = await p.create_invoiced_subscription(
        customer_id="c", price_id=refs["price_id"], tenant_id="t", plan_id="launch",
        days_until_due=14, idempotency_key="k",
    )
    assert sub["id"] == again["id"] and sub["collection_method"] == "send_invoice"
    changed = await p.change_subscription_price(subscription_id=sub["id"], price_id="p2",
                                                plan_id="accelerate")
    assert changed["metadata"]["plan_id"] == "accelerate"
    assert changed["collection_method"] == "send_invoice"
    link = await p.create_checkout_session(tenant_id="t", plan_id="launch", price_id="p")
    assert link["expires_at"] > 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_stripe_catalog_adapter.py -n0 -q -p no:cacheprovider`
Expected: FAIL, `ImportError: cannot import name 'CREDITS_META_KEY'`.

- [ ] **Step 3: Implement in `nexus/billing/payments.py`**

3a. Give errors a status. Replace the `PaymentError` class body with:

```python
class PaymentError(RuntimeError):
    """Base class for payment-provider failures.

    ``status`` is the provider's HTTP status when there was one (0 for a transport failure), so a
    caller can tell "this object is not in this account" (404) from "the key is wrong" (401).
    """

    status: int = 0
```

In `StripePaymentProvider._request`, where a >= 400 response builds `last = PaymentError(...)`,
add on the next line: `last.status = resp.status_code`.

3b. Constants, after the `PaymentNotConfigured` class:

```python
#: Subscription metadata that says the plan's credits arrive when an invoice is PAID, not when the
#: subscription starts. Set on subscriptions a superadmin starts by emailed invoice (2026-10-09).
CREDITS_META_KEY = "nexus_credits"
CREDITS_ON_PAYMENT = "on_payment"
```

3c. In `PaymentProvider`, change `ensure_plan_price`'s signature to add `product_id: str = ""` as
the last keyword argument and append to its docstring: "``product_id`` reuses a product this
account already holds, so only a new price is created." Then add these NON-abstract methods at the
end of the class (defaults keep every existing provider and test double working):

```python
    async def account_id(self) -> str:
        """Which account at the provider the active credentials belong to.

        Every id this deployment caches is only meaningful inside one account; reusing it after
        the key changed is the "No such price" failure (reported 2026-10-09).
        """
        return self.name

    async def object_exists(self, kind: str, object_id: str) -> bool:
        """Whether a cached id still names an object in the CURRENT account.

        Asked once for caches written before ids recorded their account. The default answers yes,
        which is exactly how those caches were treated before.
        """
        return True

    async def set_price_active(self, *, price_id: str, active: bool) -> None:
        raise PaymentError(f"{self.name} cannot archive or reactivate a price")

    async def rename_product(self, *, product_id: str, name: str) -> None:
        raise PaymentError(f"{self.name} cannot rename a product")

    async def create_invoiced_subscription(
        self, *, customer_id: str, price_id: str, tenant_id: str, plan_id: str,
        days_until_due: int, idempotency_key: str,
    ) -> dict:
        """Start a subscription paid by emailed invoice and send its first invoice now.

        Returns at least ``{"id", "status", "customer", "collection_method",
        "current_period_start", "current_period_end", "hosted_invoice_url", "invoice_note"}``.
        """
        raise PaymentError(f"{self.name} cannot create a subscription")

    async def change_subscription_price(
        self, *, subscription_id: str, price_id: str, plan_id: str,
    ) -> dict:
        """Move a one-item subscription to ``price_id`` now, invoicing the prorated difference."""
        raise PaymentError(f"{self.name} cannot change a subscription's price")
```

3d. `NoopPaymentProvider`: in `__init__` add

```python
        self.account = "noop"
        self.price_active: dict[str, bool] = {}
        self.renames: list[dict[str, Any]] = []
        self.price_changes: list[dict[str, Any]] = []
        self._subs_by_key: dict[str, dict[str, Any]] = {}
```

replace its `ensure_plan_price` with

```python
    async def ensure_plan_price(
        self, *, plan_id: str, name: str, amount_cents: int, currency: str = "USD",
        interval: str = "month", product_id: str = "",
    ) -> dict:
        out = {"product_id": product_id or f"noop_prod_{plan_id}",
               "price_id": f"noop_price_{plan_id}"}
        self.prices[plan_id] = {**out, "amount_cents": amount_cents, "name": name}
        return out
```

and add

```python
    async def account_id(self) -> str:
        return self.account

    async def object_exists(self, kind: str, object_id: str) -> bool:
        return True

    async def set_price_active(self, *, price_id: str, active: bool) -> None:
        self.price_active[price_id] = active

    async def rename_product(self, *, product_id: str, name: str) -> None:
        self.renames.append({"product_id": product_id, "name": name})

    async def create_invoiced_subscription(
        self, *, customer_id: str, price_id: str, tenant_id: str, plan_id: str,
        days_until_due: int, idempotency_key: str,
    ) -> dict:
        import time

        if idempotency_key in self._subs_by_key:
            return self._subs_by_key[idempotency_key]
        now = int(time.time())
        sid = f"noop_sub_{len(self._subs_by_key) + 1}"
        out = {
            "id": sid, "status": "active", "customer": customer_id, "price_id": price_id,
            "collection_method": "send_invoice", "days_until_due": days_until_due,
            "metadata": {"tenant_id": tenant_id, "plan_id": plan_id,
                         CREDITS_META_KEY: CREDITS_ON_PAYMENT},
            "current_period_start": now, "current_period_end": now + 30 * 86400,
            "hosted_invoice_url": "", "invoice_note": "",
        }
        self._subs_by_key[idempotency_key] = out
        self.subscriptions[sid] = out
        return out

    async def change_subscription_price(
        self, *, subscription_id: str, price_id: str, plan_id: str,
    ) -> dict:
        current = self.subscriptions.get(subscription_id) or {"id": subscription_id,
                                                              "status": "active"}
        out = {**current, "price_id": price_id,
               "metadata": {**(current.get("metadata") or {}), "plan_id": plan_id}}
        self.subscriptions[subscription_id] = out
        self.price_changes.append({"id": subscription_id, "price_id": price_id,
                                   "plan_id": plan_id})
        return out
```

In `NoopPaymentProvider.create_checkout_session`, add to `record`:
`"expires_at": int(__import__("time").time()) + 24 * 3600,` (Stripe's own limit).

3e. `StripePaymentProvider`: add a module-level cache next to `_portal_configurations`:

```python
#: Account id per key digest. One `GET /v1/account` per key per process: the answer cannot change
#: for a given key, and every cached plan, price and customer id is checked against it.
_account_ids: dict[str, str] = {}
```

replace `ensure_plan_price` with

```python
    async def ensure_plan_price(
        self, *, plan_id: str, name: str, amount_cents: int, currency: str = "USD",
        interval: str = "month", product_id: str = "",
    ) -> dict:
        self._require()
        if not product_id:
            # Keyed on the plan id, so a retry inside Stripe's 24-hour idempotency window reuses
            # the product. Beyond that window the CALLER passes the product it already holds
            # (`stripe_catalog.plan_price_id`), which is what stops duplicates for good.
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
            # Product and amount are in the key: a new amount mints a NEW Stripe price, because
            # Stripe prices are immutable once created.
            idempotency_key=f"price:{product_id}:{amount_cents}:{currency}:{interval}",
        )
        return {"product_id": product_id, "price_id": str(price.get("id", ""))}
```

and add

```python
    async def account_id(self) -> str:
        import hashlib

        self._require()
        digest = hashlib.sha256(self.secret_key.encode()).hexdigest()
        if digest not in _account_ids:
            account = str((await self._get("/account")).get("id") or "")
            if not account:
                raise PaymentError("stripe /account returned no account id")
            _account_ids[digest] = account
        return _account_ids[digest]

    async def object_exists(self, kind: str, object_id: str) -> bool:
        self._require()
        if kind not in ("prices", "products", "customers"):
            raise ValueError(f"unknown object kind {kind!r}")
        try:
            obj = await self._get(f"/{kind}/{object_id}")
        except PaymentError as exc:
            if exc.status == 404:
                return False
            raise
        return not obj.get("deleted")

    async def set_price_active(self, *, price_id: str, active: bool) -> None:
        self._require()
        await self._post(f"/prices/{price_id}", {"active": "true" if active else "false"})

    async def rename_product(self, *, product_id: str, name: str) -> None:
        self._require()
        await self._post(f"/products/{product_id}", {"name": name})

    async def create_invoiced_subscription(
        self, *, customer_id: str, price_id: str, tenant_id: str, plan_id: str,
        days_until_due: int, idempotency_key: str,
    ) -> dict:
        self._require()
        sub = await self._post(
            "/subscriptions",
            {
                "customer": customer_id, "items[0][price]": price_id,
                "collection_method": "send_invoice", "days_until_due": str(int(days_until_due)),
                "metadata[tenant_id]": tenant_id, "metadata[plan_id]": plan_id,
                f"metadata[{CREDITS_META_KEY}]": CREDITS_ON_PAYMENT,
            },
            idempotency_key=idempotency_key,
        )
        invoice_id = str(sub.get("latest_invoice") or "")
        hosted, note = "", ""
        if invoice_id:
            # Finalize and send NOW, so Stripe emails it while the superadmin is still on the
            # call. Left alone, Stripe finalizes it about an hour later. A failure here never
            # loses the subscription, which already exists: the invoice still goes out on
            # Stripe's own schedule, and the note says so.
            try:
                inv = await self._post(f"/invoices/{invoice_id}/finalize", {})
                if inv.get("status") == "open":
                    inv = await self._post(f"/invoices/{invoice_id}/send", {})
                hosted = str(inv.get("hosted_invoice_url") or "")
            except PaymentError as exc:
                note = f"Stripe will send the first invoice on its own schedule: {exc}"[:300]
        return {
            "id": str(sub.get("id", "")), "status": str(sub.get("status", "")),
            "customer": str(sub.get("customer") or customer_id),
            "collection_method": str(sub.get("collection_method") or "send_invoice"),
            "current_period_start": sub.get("current_period_start"),
            "current_period_end": sub.get("current_period_end"),
            "invoice_id": invoice_id, "hosted_invoice_url": hosted, "invoice_note": note,
        }

    async def change_subscription_price(
        self, *, subscription_id: str, price_id: str, plan_id: str,
    ) -> dict:
        self._require()
        current = await self._get(f"/subscriptions/{subscription_id}")
        items = (current.get("items") or {}).get("data") or []
        if len(items) != 1:
            # Every subscription NEXUS creates has exactly one item. Anything else was built by
            # hand in the Stripe dashboard, and guessing which item is "the plan" would reprice the
            # wrong line.
            raise PaymentError(
                f"stripe subscription {subscription_id} has {len(items)} items; "
                "change it in the Stripe dashboard"
            )
        return await self._post(
            f"/subscriptions/{subscription_id}",
            {
                "items[0][id]": str(items[0]["id"]), "items[0][price]": price_id,
                "proration_behavior": "always_invoice", "metadata[plan_id]": plan_id,
            },
        )
```

In `StripePaymentProvider.create_checkout_session`'s return dict add
`"expires_at": int(data.get("expires_at") or 0),`.

- [ ] **Step 4: Run the new test and the existing Stripe tests**

Run: `python -m pytest tests/test_stripe_catalog_adapter.py tests/test_stripe_http_hardening.py tests/test_billing_payments.py tests/test_stripe_operator_fixes.py tests/test_billing_custom_plans.py tests/test_billing_checkout_api.py -n0 -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/payments.py tests/test_stripe_catalog_adapter.py
git commit -m "feat(billing): the Stripe seam reads its account, archives, renames and starts invoiced subscriptions"
```

---

### Task 3: `stripe_catalog` — ids that belong to the current account

**Files:**
- Create: `nexus/billing/stripe_catalog.py`
- Create: `tests/stripe_fakes.py`
- Test: `tests/test_stripe_catalog.py`

- [ ] **Step 1: Write the test double**

```python
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
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_stripe_catalog.py
"""Cached Stripe ids are reused only inside the account they were made in (reported 2026-10-09:
"product id not available" after production moved to a live key and staging to a new test
account)."""
from __future__ import annotations

from nexus.billing.stripe_catalog import (
    archive_plan_price,
    customer_id_for,
    plan_price_id,
    rename_plan_product,
)
from nexus.models.billing import BillingPlan, BillingSubscription
from tests.stripe_fakes import TwoAccountStripe

LEGACY_TERMS = {"price_amount_cents": 9900, "price_currency": "USD", "price_interval": "month"}


def _plan(meta: dict | None = None) -> BillingPlan:
    return BillingPlan(id="launch", name="Launch", plan_class="standard", status="active",
                       base_price_cents=9900, currency="USD", interval="month",
                       meta=dict(meta or {}))


def _sub(customer: str | None = None) -> BillingSubscription:
    return BillingSubscription(tenant_id="tenant-1", plan_id="launch",
                               psp_customer_id=customer, meta={})


async def test_a_price_is_created_once_and_reused_in_the_same_account():
    stripe, plan = TwoAccountStripe(), _plan()
    first = await plan_price_id(plan, stripe)
    assert await plan_price_id(plan, stripe) == first
    assert len(stripe.ensured) == 1
    assert plan.meta["price_account"] == "acct_old"


async def test_a_new_account_gets_its_own_product_and_price():
    stripe, plan = TwoAccountStripe(), _plan()
    old = await plan_price_id(plan, stripe)
    stripe.account = "acct_new"
    new = await plan_price_id(plan, stripe)
    assert new != old and new.startswith("price_acct_new")
    assert stripe.ensured[-1]["product_id"] == ""      # the old account's product is not reused
    previous = plan.meta["previous_stripe"][-1]
    assert previous["price_id"] == old and previous["account"] == "acct_old"
    assert previous["product_id"].startswith("prod_acct_old")


async def test_a_new_amount_keeps_the_product_and_archives_the_old_price():
    stripe, plan = TwoAccountStripe(), _plan()
    old = await plan_price_id(plan, stripe)
    product = plan.meta["product_id"]
    plan.base_price_cents = 12900
    new = await plan_price_id(plan, stripe)
    assert new != old and plan.meta["product_id"] == product
    assert stripe.ensured[-1]["product_id"] == product
    assert stripe.price_active[old] is False


async def test_a_cache_from_before_accounts_were_recorded_is_checked_once():
    stripe = TwoAccountStripe()
    stripe.owner["price_legacy"] = "acct_old"
    plan = _plan({"product_id": "prod_legacy", "price_id": "price_legacy", **LEGACY_TERMS})
    assert await plan_price_id(plan, stripe) == "price_legacy"
    assert stripe.ensured == []
    assert plan.meta["price_account"] == "acct_old"


async def test_a_legacy_cache_from_another_account_is_replaced():
    stripe = TwoAccountStripe("acct_live")
    plan = _plan({"product_id": "prod_test", "price_id": "price_test", **LEGACY_TERMS})
    assert (await plan_price_id(plan, stripe)).startswith("price_acct_live")
    assert plan.meta["previous_stripe"][-1]["price_id"] == "price_test"


async def test_an_archived_price_is_reactivated_when_the_plan_is_sold_again():
    stripe, plan = TwoAccountStripe(), _plan()
    price = await plan_price_id(plan, stripe)
    assert await archive_plan_price(plan, stripe) == ""
    assert stripe.price_active[price] is False
    assert await plan_price_id(plan, stripe) == price
    assert stripe.price_active[price] is True


async def test_a_rename_reaches_the_product_in_this_account_only():
    stripe, plan = TwoAccountStripe(), _plan()
    await plan_price_id(plan, stripe)
    plan.name = "Launch Plus"
    assert await rename_plan_product(plan, stripe) == ""
    assert stripe.renames[-1]["name"] == "Launch Plus"
    stripe.account = "acct_other"
    assert await rename_plan_product(plan, stripe) == ""
    assert len(stripe.renames) == 1


async def test_a_customer_from_another_account_is_replaced():
    stripe, sub = TwoAccountStripe(), _sub("cus_test_1")
    cid = await customer_id_for(sub, stripe, tenant_id="tenant-1", email="o@x.com")
    assert cid.startswith("cus_acct_old") and sub.psp_customer_id == cid
    assert sub.meta["previous_psp_customers"][-1]["id"] == "cus_test_1"
    assert sub.meta["psp_customer_account"] == "acct_old"


async def test_a_customer_in_this_account_is_kept():
    stripe, sub = TwoAccountStripe(), _sub("cus_keep")
    stripe.owner["cus_keep"] = "acct_old"
    assert await customer_id_for(sub, stripe, tenant_id="tenant-1", email="o@x.com") == "cus_keep"
    assert sub.meta["psp_customer_account"] == "acct_old"


async def test_no_email_and_no_usable_customer_returns_nothing():
    assert await customer_id_for(_sub(), TwoAccountStripe(), tenant_id="tenant-1",
                                 email="") == ""
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python -m pytest tests/test_stripe_catalog.py -n0 -q -p no:cacheprovider`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.billing.stripe_catalog'`.

- [ ] **Step 4: Create `nexus/billing/stripe_catalog.py`**

```python
# nexus/billing/stripe_catalog.py
"""Stripe ids that remember which Stripe account they belong to.

Checkout cached each plan's product and price ids in ``billing_plans.meta`` and each workspace's
customer id in ``billing_subscriptions.psp_customer_id``, and reused them without recording WHICH
account they were made in. Reported 2026-10-09: production moved from a test key to a live key and
staging to a new test account, and checkout sent the old account's ids to the new one, which
answered "No such price".

The rule, in one place: an id is reused only inside the account it was made in. A cache written
before accounts were recorded is checked once (a 404 means another account). Ids from another
account are kept under ``previous_stripe`` / ``previous_psp_customers`` so an operator can still
trace them; nothing is deleted.

Callers flush. Every function here writes only to the ORM objects it is handed.
"""
from __future__ import annotations

import logging

from nexus.billing.payments import PaymentError

logger = logging.getLogger("nexus.billing.stripe_catalog")

_PRICE_KEYS = ("product_id", "price_id", "price_account", "price_amount_cents",
               "price_currency", "price_interval", "price_active")
_UNKNOWN = "?"
_HISTORY = 5


def _terms(plan) -> dict:
    return {"price_amount_cents": int(plan.base_price_cents), "price_currency": plan.currency,
            "price_interval": plan.interval}


def sellable_at_provider(plan) -> bool:
    """Plans that exist at Stripe: standard tiers with a price. Free, trial, internal and
    unlimited plans never charge anyone, and custom plans keep their own path."""
    return plan.plan_class == "standard" and int(plan.base_price_cents or 0) > 0


async def current_account(provider) -> str:
    return str(await provider.account_id() or "")


def _retire(meta: dict, account: str) -> dict:
    previous = list(meta.get("previous_stripe") or [])
    previous.append({"product_id": meta.get("product_id") or "",
                     "price_id": meta.get("price_id") or "", "account": account})
    out = {k: v for k, v in meta.items() if k not in _PRICE_KEYS}
    out["previous_stripe"] = previous[-_HISTORY:]
    return out


async def plan_price_id(plan, provider, *, account: str = "") -> str:
    """The price for ``plan`` in the CURRENT account, creating what is missing.

    Within one account a plan keeps one product; a new price is made only when the amount,
    currency or interval changed, and the superseded price is archived. Raises ``PaymentError``
    when the provider refuses; ``plan.meta`` is then left as it was.
    """
    account = account or await current_account(provider)
    meta = dict(plan.meta or {})
    price_id = str(meta.get("price_id") or "")
    recorded = str(meta.get("price_account") or "")
    if price_id and not recorded:
        recorded = account if await provider.object_exists("prices", price_id) else _UNKNOWN
    if price_id and recorded != account:
        meta = _retire(meta, recorded)
        price_id = ""
    terms = _terms(plan)
    if price_id and all(meta.get(k) == v for k, v in terms.items()):
        if meta.get("price_active") is False:
            await provider.set_price_active(price_id=price_id, active=True)
        meta.update({"price_account": account, "price_active": True})
        plan.meta = meta
        return price_id

    kwargs = {"plan_id": plan.id, "name": plan.name, "amount_cents": int(plan.base_price_cents),
              "currency": plan.currency, "interval": plan.interval}
    product_id = str(meta.get("product_id") or "") if price_id or recorded == account else ""
    if product_id:
        kwargs["product_id"] = product_id
    refs = await provider.ensure_plan_price(**kwargs)
    new_price = str(refs.get("price_id") or "")
    if price_id and price_id != new_price:
        await _set_active_quietly(provider, price_id, False)
    meta.update({"product_id": str(refs.get("product_id") or product_id), "price_id": new_price,
                 "price_account": account, "price_active": True, **terms})
    plan.meta = meta
    return new_price


async def _set_active_quietly(provider, price_id: str, active: bool) -> str:
    """Archive or reactivate, never failing the caller. Returns a warning, or ''."""
    try:
        await provider.set_price_active(price_id=price_id, active=active)
    except PaymentError as exc:
        logger.warning("could not set price %s active=%s: %s", price_id, active, exc)
        return f"Could not {'reactivate' if active else 'archive'} {price_id}: {exc}"[:300]
    return ""


async def _in_this_account(plan, provider) -> bool:
    meta = plan.meta or {}
    return bool(meta.get("price_id")) and meta.get("price_account") == await current_account(
        provider)


async def archive_plan_price(plan, provider) -> str:
    """Stop new purchases of ``plan`` at Stripe. Existing subscriptions keep billing on it.
    Never raises: a hold must not be blocked by Stripe. Returns a warning, or ''."""
    if not await _in_this_account(plan, provider):
        return ""
    warning = await _set_active_quietly(provider, str(plan.meta["price_id"]), False)
    if not warning:
        plan.meta = {**plan.meta, "price_active": False}
    return warning


async def rename_plan_product(plan, provider) -> str:
    """Rename the plan's product in this account. Never raises. Returns a warning, or ''."""
    if not await _in_this_account(plan, provider) or not (plan.meta or {}).get("product_id"):
        return ""
    try:
        await provider.rename_product(product_id=str(plan.meta["product_id"]), name=plan.name)
    except PaymentError as exc:
        return f"Could not rename the Stripe product: {exc}"[:300]
    return ""


async def apply_plan_to_provider(plan, provider, *, renamed: bool = False,
                                 was_status: str | None = None) -> dict:
    """Make Stripe match a plan a superadmin just created, edited, published or held.

    A plan on sale must be buyable, so a price it NEEDS raises ``PaymentError`` and the caller
    refuses the change. Archiving and renaming only warn.
    """
    out: dict = {"price_id": "", "warnings": []}
    if not sellable_at_provider(plan):
        return out
    if plan.status == "active":
        out["price_id"] = await plan_price_id(plan, provider)
    elif was_status == "active":
        warning = await archive_plan_price(plan, provider)
        if warning:
            out["warnings"].append(warning)
    if renamed:
        warning = await rename_plan_product(plan, provider)
        if warning:
            out["warnings"].append(warning)
    return out


async def sync_plans(session, provider, *, account: str = "") -> list[dict]:
    """Make every plan on sale exist in the current account. One row per plan:
    ``created`` / ``reused`` / ``failed`` (with Stripe's words)."""
    from sqlalchemy import select

    from nexus.models.billing import BillingPlan

    account = account or await current_account(provider)
    plans = (await session.scalars(
        select(BillingPlan).where(BillingPlan.status == "active",
                                  BillingPlan.plan_class == "standard",
                                  BillingPlan.base_price_cents > 0)
        .order_by(BillingPlan.sort_order, BillingPlan.id)
    )).all()
    report: list[dict] = []
    for plan in plans:
        before = str((plan.meta or {}).get("price_id") or "")
        try:
            price = await plan_price_id(plan, provider, account=account)
        except PaymentError as exc:
            report.append({"plan_id": plan.id, "outcome": "failed", "detail": str(exc)[:300]})
            continue
        report.append({"plan_id": plan.id, "price_id": price,
                       "outcome": "reused" if price == before else "created"})
    await session.flush()
    return report


async def customer_id_for(sub, provider, *, tenant_id: str, email: str, name: str = "",
                          account: str = "") -> str:
    """The workspace's customer in the CURRENT account, created when missing.

    Returns '' when there is no usable customer and no email to create one with; the caller
    decides what that means. Writes ``sub.psp_customer_id`` and ``sub.meta`` when ``sub`` exists.
    """
    account = account or await current_account(provider)
    meta = dict(sub.meta or {}) if sub is not None else {}
    existing = str(sub.psp_customer_id or "") if sub is not None else ""
    recorded = str(meta.get("psp_customer_account") or "")
    if existing and not recorded:
        recorded = account if await provider.object_exists("customers", existing) else _UNKNOWN
    if existing and recorded == account:
        if meta.get("psp_customer_account") != account:
            meta["psp_customer_account"] = account
            sub.meta = meta
        return existing
    if not email:
        return ""
    customer_id = await provider.ensure_customer(tenant_id=tenant_id, email=email, name=name)
    if sub is not None:
        if existing:
            previous = list(meta.get("previous_psp_customers") or [])
            previous.append({"id": existing, "account": recorded})
            meta["previous_psp_customers"] = previous[-_HISTORY:]
        meta["psp_customer_account"] = account
        sub.psp_customer_id = customer_id
        sub.meta = meta
    return customer_id
```

- [ ] **Step 5: Run the test**

Run: `python -m pytest tests/test_stripe_catalog.py -n0 -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add nexus/billing/stripe_catalog.py tests/stripe_fakes.py tests/test_stripe_catalog.py
git commit -m "feat(billing): stripe_catalog, cached Stripe ids that belong to the current account"
```

---

### Task 4: Checkout and invoice collection use the catalog (the reported fix)

**Files:**
- Modify: `nexus/api/routers/billing.py` (the price and customer block of `create_checkout`)
- Modify: `nexus/billing/collection.py` (the customer block of `collect_invoice`)
- Test: `tests/test_stripe_account_switch.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stripe_account_switch.py
"""After the Stripe key changes account, checkout uses ids from the NEW account."""
from __future__ import annotations

import pytest

from tests.conftest import auth
from tests.stripe_fakes import TwoAccountStripe
from tests.test_billing_checkout_api import _seeded_tenant

BODY = {"plan_id": "accelerate", "success_url": "https://app/ok", "cancel_url": "https://app/no"}


@pytest.fixture
def stripe():
    from nexus.billing.payments import set_payment_provider

    fake = TwoAccountStripe()
    set_payment_provider(fake)
    yield fake
    set_payment_provider(None)


async def test_checkout_after_an_account_switch_uses_the_new_accounts_ids(client, stripe):
    token, _ = await _seeded_tenant(client, slug="acsw1")
    assert (await client.post("/api/billing/checkout", json=BODY,
                              headers=auth(token))).status_code == 200
    first = stripe.checkout_sessions[-1]
    assert first["price_id"].startswith("price_acct_old")
    assert first["customer_id"].startswith("cus_acct_old")

    stripe.account = "acct_new"
    r = await client.post("/api/billing/checkout", json=BODY, headers=auth(token))
    assert r.status_code == 200, r.text
    second = stripe.checkout_sessions[-1]
    assert second["price_id"].startswith("price_acct_new")
    assert second["customer_id"].startswith("cus_acct_new")


async def test_checkout_inside_one_account_reuses_everything(client, stripe):
    token, _ = await _seeded_tenant(client, slug="acsw2")
    for _ in range(3):
        assert (await client.post("/api/billing/checkout", json=BODY,
                                  headers=auth(token))).status_code == 200
    assert len(stripe.ensured) == 1
    assert len({s["customer_id"] for s in stripe.checkout_sessions}) == 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_stripe_account_switch.py -n0 -q -p no:cacheprovider`
Expected: FAIL on `second["price_id"].startswith("price_acct_new")` (checkout reuses the old id).

- [ ] **Step 3: Rewire `create_checkout`**

In `nexus/api/routers/billing.py`, inside `create_checkout`'s `try:`, replace everything from the
comment block `# The price object has to exist at the PSP...` down to and including the customer
block that ends with `await ts.flush()` (just before `session_out = await
provider.create_checkout_session(`) with:

```python
        # The price and the customer must exist in the CURRENT Stripe account. Both were cached
        # without recording their account, so after the key moved to another account (test to
        # live, or a new test account) checkout sent ids Stripe could not find: "No such price"
        # (reported 2026-10-09). `stripe_catalog` reuses an id only inside its own account, keeps
        # one product per plan, and makes a new price only when the plan's price terms changed.
        from nexus.billing.stripe_catalog import current_account, customer_id_for, plan_price_id

        account = await current_account(provider)
        price_id = await plan_price_id(plan, provider, account=account)
        # billing_plans is platform-global (no tenant_id, no RLS policy), so caching the
        # reference here is a plain write, not a cross-tenant one.
        await ts.session.flush()

        email = await _billing_email(principal.user_id)
        customer_id = await customer_id_for(
            sub, provider, tenant_id=ts.tenant_id, email=email, account=account,
        )
        if sub is not None:
            await ts.flush()
```

- [ ] **Step 4: Rewire `collect_invoice`**

In `nexus/billing/collection.py`, replace the block from `customer_id = sub.psp_customer_id if sub
is not None else None` through its `await ts.flush()` with:

```python
    # The customer must exist in the CURRENT Stripe account; one cached from another account is
    # replaced rather than charged (see `stripe_catalog`).
    from nexus.billing.stripe_catalog import customer_id_for

    customer_id = await customer_id_for(
        sub, provider, tenant_id=ts.tenant_id, email=email or "", name=name or "",
    )
    if not customer_id:
        raise CollectionError(
            "no billing contact for this workspace; cannot create a payment customer"
        )
    if sub is not None:
        await ts.flush()
```

- [ ] **Step 5: Run the new test and every checkout, collection and dunning test**

Run: `python -m pytest tests/test_stripe_account_switch.py tests/test_billing_checkout_api.py tests/test_checkout_plan_gating.py tests/test_stripe_checkout_and_events.py tests/test_billing_dunning.py tests/test_provider_invoice_is_visible.py tests/test_billing_lifecycle_wiring.py tests/test_billing_payments.py -n0 -q -p no:cacheprovider`
Expected: all PASS. If a collection test fails with "no billing contact", read it: a test that set
`psp_customer_id` and passed no email relies on reuse, which `NoopPaymentProvider.object_exists`
(always True) preserves; any other failure is a real regression to fix before continuing.

- [ ] **Step 6: Commit**

```bash
git add nexus/api/routers/billing.py nexus/billing/collection.py tests/test_stripe_account_switch.py
git commit -m "fix(billing): checkout and collection use Stripe ids from the current account"
```

---

### Task 5: Plans are superadmin-only and live in Stripe

**Files:**
- Modify: `nexus/api/routers/admin_billing_write.py` (`update_plan`, `create_sellable_plan_endpoint`, `set_sellable_plan_status`, new `sync_plans_to_stripe`)
- Modify: `nexus/api/routers/admin_billing.py` (`PlanOut` + list)
- Modify: `tests/test_plan_authoring.py`, `tests/test_admin_permissions.py`
- Test: `tests/test_stripe_managed_plans.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stripe_managed_plans.py
"""Plans live in Stripe, and only a superadmin creates, edits, publishes, holds or syncs them."""
from __future__ import annotations

import pytest

from tests.conftest import auth
from tests.stripe_fakes import TwoAccountStripe
from tests.test_admin_permissions import _bootstrap_admin, _make_role
from tests.test_plan_authoring import _plan, _superadmin


@pytest.fixture
def stripe():
    from nexus.billing.payments import set_payment_provider

    fake = TwoAccountStripe()
    set_payment_provider(fake)
    yield fake
    set_payment_provider(None)


async def _scale(client, token, **over):
    r = await client.post("/api/admin/billing/plans", headers=auth(token), json=_plan(**over))
    assert r.status_code == 201, r.text
    return r


async def test_finance_cannot_create_edit_publish_or_sync_plans(client, monkeypatch):
    boss = await _bootstrap_admin(client, monkeypatch, "smp1")
    finance = await _make_role(client, boss, "cfo@smp1.com", "finance", "smp1f")
    h = auth(finance)
    assert (await client.post("/api/admin/billing/plans", headers=h,
                              json=_plan(plan_id="fin"))).status_code == 403
    assert (await client.patch("/api/admin/billing/plans/launch", headers=h,
                               json={"base_price_cents": 1})).status_code == 403
    assert (await client.put("/api/admin/billing/plans/launch/status", headers=h,
                             json={"status": "draft"})).status_code == 403
    assert (await client.post("/api/admin/billing/plans/stripe-sync",
                              headers=h)).status_code == 403
    # What Finance keeps: rate cards are still theirs.
    kept = await client.put("/api/admin/billing/rates/ai.research", headers=h,
                            json={"credits_per_unit": 50})
    assert kept.status_code != 403, kept.text


async def test_a_draft_costs_nothing_at_stripe_and_publishing_creates_the_price(
        client, monkeypatch, stripe):
    token = await _superadmin(client, monkeypatch, slug="smp2", email="boss@smp2.com")
    await _scale(client, token)
    assert stripe.ensured == []
    r = await client.put("/api/admin/billing/plans/scale/status", headers=auth(token),
                         json={"status": "active"})
    assert r.status_code == 200, r.text
    assert stripe.ensured[-1]["plan_id"] == "scale"
    plans = (await client.get("/api/admin/billing/plans", headers=auth(token))).json()
    scale = next(p for p in plans if p["id"] == "scale")
    assert scale["stripe_price_id"].startswith("price_acct_old")
    assert scale["stripe_account"] == "acct_old"


async def test_when_stripe_refuses_the_plan_stays_unpublished(client, monkeypatch, stripe):
    from nexus.billing.payments import PaymentError

    token = await _superadmin(client, monkeypatch, slug="smp3", email="boss@smp3.com")
    await _scale(client, token)

    async def refuse(**kw):
        raise PaymentError("stripe /prices -> 400: Invalid currency")

    monkeypatch.setattr(stripe, "ensure_plan_price", refuse)
    r = await client.put("/api/admin/billing/plans/scale/status", headers=auth(token),
                         json={"status": "active"})
    assert r.status_code == 502 and "Invalid currency" in r.text
    plans = (await client.get("/api/admin/billing/plans", headers=auth(token))).json()
    assert next(p for p in plans if p["id"] == "scale")["status"] == "draft"


async def test_a_new_price_on_a_plan_on_sale_creates_a_new_stripe_price(
        client, monkeypatch, stripe):
    token = await _superadmin(client, monkeypatch, slug="smp4", email="boss@smp4.com")
    await _scale(client, token, status="active")
    old = stripe.prices["scale"]["price_id"]
    r = await client.patch("/api/admin/billing/plans/scale", headers=auth(token),
                           json={"base_price_cents": 29900})
    assert r.status_code == 200, r.text
    assert stripe.prices["scale"]["amount_cents"] == 29900
    assert stripe.price_active[old] is False


async def test_renaming_renames_the_stripe_product(client, monkeypatch, stripe):
    token = await _superadmin(client, monkeypatch, slug="smp5", email="boss@smp5.com")
    await _scale(client, token, status="active")
    r = await client.patch("/api/admin/billing/plans/scale", headers=auth(token),
                           json={"name": "Scale Pro"})
    assert r.status_code == 200, r.text
    assert stripe.renames[-1]["name"] == "Scale Pro"


async def test_holding_archives_the_price(client, monkeypatch, stripe):
    token = await _superadmin(client, monkeypatch, slug="smp6", email="boss@smp6.com")
    await _scale(client, token, status="active")
    price = stripe.prices["scale"]["price_id"]
    r = await client.put("/api/admin/billing/plans/scale/status", headers=auth(token),
                         json={"status": "draft"})
    assert r.status_code == 200, r.text
    assert stripe.price_active[price] is False


async def test_sync_creates_what_is_missing_and_reports_it(client, monkeypatch, stripe):
    token = await _superadmin(client, monkeypatch, slug="smp7", email="boss@smp7.com")
    r = await client.post("/api/admin/billing/plans/stripe-sync", headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["account"] == "acct_old"
    rows = {row["plan_id"]: row for row in r.json()["plans"]}
    assert rows["launch"]["outcome"] == "created"
    assert rows["accelerate"]["outcome"] == "created"
    assert "free" not in rows
    again = (await client.post("/api/admin/billing/plans/stripe-sync",
                               headers=auth(token))).json()["plans"]
    assert {row["outcome"] for row in again} == {"reused"}


async def test_sync_without_stripe_says_so(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="smp8", email="boss@smp8.com")
    r = await client.post("/api/admin/billing/plans/stripe-sync", headers=auth(token))
    assert r.status_code == 409 and "Payments tab" in r.text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_stripe_managed_plans.py -n0 -q -p no:cacheprovider`
Expected: FAIL (finance gets 200 on the plan endpoints; sync endpoint 405/404).

- [ ] **Step 3: Gate and wire the plan endpoints**

In `nexus/api/routers/admin_billing_write.py`, add `STRIPE_MANAGE` to the existing
`from nexus.billing.permissions import (...)` list.

Replace `update_plan` with:

```python
@router.patch("/plans/{plan_id}")
async def update_plan(
    plan_id: str,
    body: PlanPatch,
    principal: Principal = Depends(require_platform_permission(STRIPE_MANAGE)),
) -> dict:
    """Edit a plan. Superadmin only (2026-10-09): a plan on sale lives in Stripe, so a new name
    renames its product and a new price on an active plan creates a new Stripe price (Stripe
    prices are immutable; existing subscribers stay on theirs until a plan change moves them)."""
    from nexus.billing.payments import PaymentError, resolve_payment_provider
    from nexus.billing.stripe_catalog import apply_plan_to_provider

    async with get_sessionmaker()() as session:
        plan = await session.get(BillingPlan, plan_id)
        if plan is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown plan '{plan_id}'")
        before = snapshot(plan, _PLAN_FIELDS)
        was_name, was_status = plan.name, plan.status
        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(plan, field, value)
        await session.flush()
        try:
            stripe = await apply_plan_to_provider(
                plan, await resolve_payment_provider(),
                renamed=plan.name != was_name, was_status=was_status,
            )
        except PaymentError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"Stripe refused, nothing was changed: {exc}"
            ) from exc
        await session.flush()
        await record_admin_action(
            session, actor=principal.user_id, action="plan.update", target=plan_id,
            before=before, after=snapshot(plan, _PLAN_FIELDS),
        )
        await session.commit()
        return {
            "id": plan.id, "name": plan.name, "status": plan.status,
            "base_price_cents": plan.base_price_cents,
            "seat_price_cents": plan.seat_price_cents,
            "included_credits": plan.included_credits,
            "stripe": stripe,
        }
```

In `create_sellable_plan_endpoint`: change the dependency to
`require_platform_permission(STRIPE_MANAGE)`, add "Superadmin only (2026-10-09)." to its docstring,
and between the `except PlanAuthoringError` block and `await record_admin_action(` insert:

```python
        if result.get("status") == "active":
            from nexus.billing.payments import PaymentError, resolve_payment_provider
            from nexus.billing.stripe_catalog import apply_plan_to_provider

            plan = await session.get(BillingPlan, result["plan_id"])
            try:
                result["stripe"] = await apply_plan_to_provider(
                    plan, await resolve_payment_provider())
            except PaymentError as exc:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f"Stripe refused, the plan was not created: {exc}",
                ) from exc
```

In `set_sellable_plan_status`: change the dependency to `STRIPE_MANAGE`, and between the
`except PlanAuthoringError` block and `await record_admin_action(` insert:

```python
        from nexus.billing.payments import PaymentError, resolve_payment_provider
        from nexus.billing.stripe_catalog import apply_plan_to_provider

        try:
            stripe = await apply_plan_to_provider(
                plan, await resolve_payment_provider(), was_status=was)
        except PaymentError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Stripe refused, the plan was not published: {exc}",
            ) from exc
```

and add `"stripe": stripe` to its returned dict.

Add the sync endpoint right after `set_sellable_plan_status`:

```python
@router.post("/plans/stripe-sync")
async def sync_plans_to_stripe(
    principal: Principal = Depends(require_platform_permission(STRIPE_MANAGE)),
) -> dict:
    """Make every plan on sale exist in the Stripe account the active key belongs to, and say
    what was created, reused or refused. Safe to repeat: inside one account everything is
    reused."""
    from nexus.billing.payments import PaymentError, resolve_payment_provider
    from nexus.billing.stripe_catalog import current_account, sync_plans

    provider = await resolve_payment_provider()
    if provider.name != "stripe":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No Stripe credential is in force. Add and activate one on the Payments tab first.",
        )
    async with get_sessionmaker()() as session:
        try:
            account = await current_account(provider)
        except PaymentError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Stripe refused: {exc}") from exc
        report = await sync_plans(session, provider, account=account)
        await record_admin_action(
            session, actor=principal.user_id, action="plan.stripe_sync", target=account,
            after={"plans": report},
        )
        await session.commit()
    return {"account": account, "plans": report}
```

- [ ] **Step 4: Show Stripe state on the plan list**

In `nexus/api/routers/admin_billing.py`, add to `PlanOut`:

```python
    # Which Stripe price and account the plan's cached id belongs to ('' when it has none). The
    # Plans tab compares the account with the active credential's.
    stripe_price_id: str = ""
    stripe_account: str = ""
```

and in the list builder pass
`stripe_price_id=str((p.meta or {}).get("price_id") or ""),`
`stripe_account=str((p.meta or {}).get("price_account") or ""),`.

- [ ] **Step 5: Encode the two decided behaviour changes in existing tests**

In `tests/test_admin_permissions.py`, replace `test_finance_can_reprice_but_not_manage_admins`'s
first assertion block (the `ok = await client.patch(... "/api/admin/billing/plans/starter" ...)`
lines) with:

```python
    # Plans are superadmin-only since 2026-10-09 (they live in Stripe). Rate cards stay Finance's.
    plan_edit = await client.patch(
        "/api/admin/billing/plans/starter", headers=auth(finance),
        json={"base_price_cents": 4200},
    )
    assert plan_edit.status_code == 403
    rate = await client.put(
        "/api/admin/billing/rates/ai.research", headers=auth(finance),
        json={"credits_per_unit": 50},
    )
    assert rate.status_code != 403, rate.text
```

and rename the test to `test_finance_can_set_rates_but_not_plans_or_admins`.

In `tests/test_plan_authoring.py`, replace `test_no_stripe_object_is_created_for_a_plan_nobody_has_bought`
with:

```python
async def test_a_draft_creates_nothing_at_stripe(client, monkeypatch):
    """A draft is not for sale, so it must not litter the Stripe account with products."""
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider

    provider = NoopPaymentProvider()
    set_payment_provider(provider)
    try:
        token = await _superadmin(client, monkeypatch, slug="pa20", email="boss@pa20.com")
        await client.post("/api/admin/billing/plans", headers=auth(token), json=_plan())
        assert provider.prices == {}
    finally:
        set_payment_provider(None)


async def test_a_plan_created_on_sale_exists_at_stripe(client, monkeypatch):
    """Decided 2026-10-09: plans live in Stripe, so a plan on sale is always buyable."""
    from nexus.billing.payments import NoopPaymentProvider, set_payment_provider

    provider = NoopPaymentProvider()
    set_payment_provider(provider)
    try:
        token = await _superadmin(client, monkeypatch, slug="pa20b", email="boss@pa20b.com")
        r = await client.post("/api/admin/billing/plans", headers=auth(token),
                              json=_plan(status="active"))
        assert r.status_code == 201, r.text
        assert "scale" in provider.prices
    finally:
        set_payment_provider(None)
```

- [ ] **Step 6: Run the plan, permission and admin-write suites**

Run: `python -m pytest tests/test_stripe_managed_plans.py tests/test_plan_authoring.py tests/test_admin_permissions.py tests/test_billing_admin_writes.py tests/test_billing_admin_api.py tests/test_billing_platform_admins.py tests/test_plan_families.py tests/test_billing_abuse.py tests/test_billing_flag_admin.py tests/test_capability_authoring.py -n0 -q -p no:cacheprovider`
Expected: all PASS. A failure in a suite not listed in the File map that PATCHes a plan as a
non-superadmin is a behaviour this plan deliberately changes: update its expectation to 403 with a
one-line comment citing 2026-10-09; anything else is a regression to fix.

- [ ] **Step 7: Commit**

```bash
git add nexus/api/routers/admin_billing_write.py nexus/api/routers/admin_billing.py tests/test_stripe_managed_plans.py tests/test_plan_authoring.py tests/test_admin_permissions.py
git commit -m "feat(billing): plans are superadmin-only and live in Stripe; Sync to Stripe"
```

---

### Task 6: Invoiced subscriptions get their credits when the invoice is paid

**Files:**
- Modify: `nexus/billing/webhooks.py`
- Test: `tests/test_stripe_invoice_credits.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stripe_invoice_credits.py
"""Decided 2026-10-09: a subscription paid by emailed invoice receives its plan's credits when the
invoice is PAID. A card subscription (Checkout) still receives them at once."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

from tests.test_billing_checkout_api import _seeded_tenant

SECRET = "whsec_credits"


async def _send(client, monkeypatch, event_type: str, obj: dict):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "stripe_webhook_secret", SECRET)
    payload = json.dumps({"id": f"evt_{uuid.uuid4().hex[:12]}", "type": event_type,
                          "data": {"object": obj}}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    r = await client.post("/api/billing/webhooks/stripe", content=payload,
                          headers={"Stripe-Signature": f"t={ts},v1={sig}",
                                   "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    return r


async def _state(tid: str):
    from sqlalchemy import select

    from nexus.billing.credits import balance
    from nexus.core.db import get_platform_sessionmaker
    from nexus.core.tenancy import TenantSession, apply_rls
    from nexus.models.billing import BillingPlan, BillingSubscription

    async with get_platform_sessionmaker()() as s:
        await apply_rls(s, tid)
        sub = (await s.scalars(select(BillingSubscription).where(
            BillingSubscription.tenant_id == tid))).first()
        launch = await s.get(BillingPlan, "launch")
        return sub, float(await balance(TenantSession(s, tid))), int(launch.included_credits)


def _subscription(tid: str, sub_id: str, *, invoiced: bool) -> dict:
    now = int(time.time())
    meta = {"tenant_id": tid, "plan_id": "launch"}
    if invoiced:
        meta["nexus_credits"] = "on_payment"
    return {"id": sub_id, "object": "subscription", "customer": f"cus_{sub_id}",
            "status": "active", "metadata": meta, "current_period_start": now,
            "current_period_end": now + 30 * 86400}


async def test_an_invoiced_subscription_is_credited_when_the_invoice_is_paid(client, monkeypatch):
    _, tid = await _seeded_tenant(client, slug="ic1", plan_id="free")
    _, before, credits = await _state(tid)

    await _send(client, monkeypatch, "customer.subscription.created",
                _subscription(tid, "sub_inv1", invoiced=True))
    sub, now, _ = await _state(tid)
    assert sub.plan_id == "launch"
    assert now == before                                   # the invoice is unpaid
    assert sub.meta["credits_pending_plan"] == "launch"

    invoice = {"id": "in_inv1", "object": "invoice", "subscription": "sub_inv1",
               "customer": "cus_sub_inv1", "status": "paid", "amount_paid": 9900,
               "billing_reason": "subscription_create", "metadata": {}}
    await _send(client, monkeypatch, "invoice.paid", invoice)
    sub, paid, _ = await _state(tid)
    assert paid == before + credits
    assert "credits_pending_plan" not in sub.meta

    await _send(client, monkeypatch, "invoice.paid", {**invoice, "id": "in_inv1b"})
    assert (await _state(tid))[1] == before + credits          # never twice


async def test_a_card_subscription_is_still_credited_at_once(client, monkeypatch):
    _, tid = await _seeded_tenant(client, slug="ic2", plan_id="free")
    _, before, credits = await _state(tid)
    await _send(client, monkeypatch, "customer.subscription.created",
                _subscription(tid, "sub_card1", invoiced=False))
    assert (await _state(tid))[1] == before + credits
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_stripe_invoice_credits.py -n0 -q -p no:cacheprovider`
Expected: FAIL in the invoiced test (`now == before` fails: credits granted at creation).

- [ ] **Step 3: Defer the grant in `_apply_subscription_event`**

In `nexus/billing/webhooks.py`, in the `else:  # customer.subscription.created | updated` branch,
replace

```python
                take_plan_terms(sub, bought)
                # Same reasoning as the Checkout branch above. ...
                await _grant_plan_credits(session, sub, bought)
```

(keep the existing comment lines) with:

```python
                take_plan_terms(sub, bought)
                if _credits_on_payment(meta_in):
                    # Paid by emailed invoice (started from the console, 2026-10-09): the credits
                    # wait for `invoice.paid`, so a customer who never pays never spends them.
                    meta["credits_pending_plan"] = bought.id
                else:
                    # Same reasoning as the Checkout branch above. Stripe can deliver the plan on
                    # either event depending on how the subscription was created, so both grant —
                    # and the grant is keyed per plan per period, so whichever arrives second is a
                    # no-op rather than a second helping.
                    await _grant_plan_credits(session, sub, bought)
```

Add near `_grant_plan_credits`:

```python
def _credits_on_payment(metadata: dict) -> bool:
    from nexus.billing.payments import CREDITS_META_KEY, CREDITS_ON_PAYMENT

    return str((metadata or {}).get(CREDITS_META_KEY) or "") == CREDITS_ON_PAYMENT


async def _grant_pending_credits(session, sub, outcome: dict) -> None:
    """On `invoice.paid`, deliver credits an invoiced subscription has been waiting for.

    Only for the plan the workspace is still on: a customer who moved plans before paying is owed
    the plan they are on, and that move set its own marker. Never raises (see
    `_grant_plan_credits`)."""
    from nexus.core.tenancy import apply_rls

    pending = str((sub.meta or {}).get("credits_pending_plan") or "")
    if not pending:
        return
    if pending == sub.plan_id:
        plan = await session.get(BillingPlan, pending)
        if plan is not None:
            await _grant_plan_credits(session, sub, plan)
            outcome["credits_granted_for"] = pending
    await apply_rls(session, sub.tenant_id)
    sub.meta = {k: v for k, v in (sub.meta or {}).items() if k != "credits_pending_plan"}
    await session.flush()
```

In `_apply_invoice_event`, right after the `if sub is not None: outcome["tenant_id"] =
sub.tenant_id` lines, insert:

```python
    if event.event_type == "invoice.paid" and sub is not None:
        await _grant_pending_credits(session, sub, outcome)
```

- [ ] **Step 4: Run the new test and every webhook suite**

Run: `python -m pytest tests/test_stripe_invoice_credits.py tests/test_billing_webhook_lifecycle.py tests/test_billing_webhook_security.py tests/test_webhook_forgery.py tests/test_webhook_invoice_lookup.py tests/test_stripe_checkout_and_events.py tests/test_plan_change_grants_credits.py tests/test_stripe_operator_fixes.py -n0 -q -p no:cacheprovider`
Expected: all PASS (`test_every_event_the_handler_branches_on_is_in_the_published_list` still sees
13 events: no new event type is compared).

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/webhooks.py tests/test_stripe_invoice_credits.py
git commit -m "feat(billing): invoiced subscriptions are credited when the invoice is paid"
```

---

### Task 7: A superadmin starts a Stripe subscription (emailed invoice or Checkout link)

**Files:**
- Create: `nexus/billing/stripe_subscriptions.py`
- Modify: `nexus/api/routers/admin_billing_write.py` (new `StripeSubscriptionIn` + endpoint)
- Test: `tests/test_stripe_admin_subscriptions.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stripe_admin_subscriptions.py
"""A superadmin starts a workspace on a Stripe subscription, or moves a Stripe-billed workspace to
another plan at Stripe (decided 2026-10-09)."""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup
from tests.stripe_fakes import TwoAccountStripe
from tests.test_admin_permissions import _bootstrap_admin, _make_role


@pytest.fixture
def stripe():
    from nexus.billing.payments import set_payment_provider

    fake = TwoAccountStripe()
    set_payment_provider(fake)
    yield fake
    set_payment_provider(None)


async def _boss_and_customer(client, monkeypatch, slug: str):
    boss = await _bootstrap_admin(client, monkeypatch, slug)
    company = f"{slug} Customer"
    await signup(client, slug=f"{slug}-c", email=f"o@{slug}-c.com", company=company)
    rows = (await client.get(f"/api/admin/billing/customers?q={company}",
                             headers=auth(boss))).json()
    return boss, rows[0]["tenant_id"]


async def _sub(tid: str):
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingSubscription

    async with get_platform_sessionmaker()() as s:
        return (await s.scalars(select(BillingSubscription).where(
            BillingSubscription.tenant_id == tid))).first()


def _start(client, boss, tid, **body):
    return client.post(f"/api/admin/billing/tenants/{tid}/stripe-subscription",
                       headers=auth(boss), json={"plan_id": "launch", **body})


async def test_an_emailed_invoice_starts_the_subscription_with_credits_waiting(
        client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas1")
    r = await _start(client, boss, tid, method="invoice")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "invoice" and body["days_until_due"] == 14
    sub = await _sub(tid)
    assert sub.plan_id == "launch" and sub.psp_subscription_id == body["psp_subscription_id"]
    assert sub.psp_customer_id.startswith("cus_acct_old")
    assert sub.meta["credits_pending_plan"] == "launch"
    assert sub.meta["collection"] == "send_invoice"
    created = stripe.subscriptions[sub.psp_subscription_id]
    assert created["days_until_due"] == 14


async def test_the_days_to_pay_can_be_changed(client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas2")
    r = await _start(client, boss, tid, method="invoice", days_until_due=30)
    assert r.status_code == 200, r.text
    assert stripe.subscriptions[(await _sub(tid)).psp_subscription_id]["days_until_due"] == 30


async def test_a_checkout_link_changes_nothing_until_the_customer_pays(
        client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas3")
    plan_before = (await _sub(tid)).plan_id
    r = await _start(client, boss, tid, method="checkout_link")
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://") and r.json()["expires_at"] > 0
    assert stripe.checkout_sessions[-1]["tenant_id"] == tid
    assert (await _sub(tid)).plan_id == plan_before


async def test_a_workspace_already_on_stripe_is_refused(client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas4")
    assert (await _start(client, boss, tid, method="invoice")).status_code == 200
    again = await _start(client, boss, tid, method="invoice")
    assert again.status_code == 409 and "Change its plan" in again.text


async def test_a_plan_with_nothing_to_bill_is_refused(client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas5")
    r = await _start(client, boss, tid, plan_id="free", method="invoice")
    assert r.status_code == 409


async def test_without_stripe_it_says_so(client, monkeypatch):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas6")
    r = await _start(client, boss, tid, method="invoice")
    assert r.status_code == 409 and "Payments tab" in r.text


async def test_only_a_superadmin_can_start_one(client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas7")
    finance = await _make_role(client, boss, "cfo@sas7.com", "finance", "sas7f")
    assert (await _start(client, finance, tid, method="invoice")).status_code == 403
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_stripe_admin_subscriptions.py -n0 -q -p no:cacheprovider`
Expected: FAIL (404/405 on `/stripe-subscription`).

- [ ] **Step 3: Create `nexus/billing/stripe_subscriptions.py`**

```python
# nexus/billing/stripe_subscriptions.py
"""Stripe subscriptions a superadmin starts or moves from the console (decided 2026-10-09).

Two ways to start one: an emailed Stripe invoice (the subscription starts now, its credits arrive
when the invoice is paid) or a Checkout link (nothing changes until the customer pays, then the
existing webhook applies it). A plan change for a Stripe-billed workspace moves the Stripe price
now and invoices the prorated difference.

Every Stripe write happens before our row changes, so a refusal leaves NEXUS untouched.
"""
from __future__ import annotations

from nexus.billing.payments import resolve_payment_provider
from nexus.core.db import utcnow
from nexus.models.billing import BillingPlan

#: Plan classes that can be billed through a Stripe subscription. Everything else charges nobody
#: (free, trial, internal, unlimited) or is invoiced from the console (enterprise).
BILLABLE_CLASSES = ("standard", "custom")
LIVE_STATUSES = ("active", "trialing", "past_due")
NO_CREDENTIAL = ("No Stripe credential is in force. Add and activate one on the Payments tab "
                 "first.")


class StripeSubscriptionError(Exception):
    """A request the current state forbids. ``status_code`` is the HTTP answer."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def _app_url(path: str) -> str:
    from nexus.core.config import get_settings

    base = (get_settings().app_base_url or "").rstrip("/")
    return f"{base}{path}" if base else path


async def _newest_subscription(ts):
    from nexus.billing.subscriptions import _active, _any_subscription

    return await _active(ts) or await _any_subscription(ts)


async def owner_email(ts) -> str:
    """The workspace owner's email: who a Stripe invoice is sent to by default."""
    from sqlalchemy import select

    from nexus.models.identity import Membership, User

    row = (await ts.session.execute(
        select(User.email).join(Membership, Membership.user_id == User.id)
        .where(Membership.tenant_id == ts.tenant_id, Membership.role == "owner")
        .order_by(Membership.created_at)
    )).first()
    return str(row[0] or "") if row else ""


def _check_billable(plan, sub) -> None:
    if plan.plan_class not in BILLABLE_CLASSES or int(plan.base_price_cents or 0) <= 0:
        raise StripeSubscriptionError(f"'{plan.name}' has no price to bill through Stripe.")
    if plan.plan_class == "custom" and (sub is None or sub.plan_id != plan.id):
        raise StripeSubscriptionError(
            "A custom plan can only be billed for the workspace it was made for.")


async def stripe_subscription_of(ts):
    """The workspace's live subscription when it is billed through Stripe, else None."""
    from nexus.billing.subscriptions import _active

    sub = await _active(ts)
    return sub if sub is not None and (sub.psp_subscription_id or "").strip() else None


async def start_subscription(ts, *, plan_id: str, method: str, days_until_due: int = 14,
                             billing_email: str = "", actor: str = "system") -> dict:
    from nexus.billing.stripe_catalog import current_account, customer_id_for, plan_price_id
    from nexus.billing.subscriptions import ensure_subscription, take_plan_terms
    from nexus.billing.webhooks import STRIPE_SUBSCRIPTION_STATUS, _epoch

    plan = await ts.session.get(BillingPlan, plan_id)
    if plan is None:
        raise StripeSubscriptionError(f"Unknown plan '{plan_id}'", 404)
    sub = await _newest_subscription(ts)
    _check_billable(plan, sub)
    if sub is not None and (sub.psp_subscription_id or "") and sub.status in LIVE_STATUSES:
        raise StripeSubscriptionError(
            "This workspace already has a Stripe subscription. Change its plan instead.")
    provider = await resolve_payment_provider()
    if provider.name != "stripe":
        raise StripeSubscriptionError(NO_CREDENTIAL)
    email = billing_email or await owner_email(ts)
    if not email:
        raise StripeSubscriptionError(
            "This workspace has no owner email to bill. Enter a billing email.")

    account = await current_account(provider)
    price_id = await plan_price_id(plan, provider, account=account)
    customer_id = await customer_id_for(sub, provider, tenant_id=ts.tenant_id, email=email,
                                        account=account)
    if method == "checkout_link":
        link = await provider.create_checkout_session(
            tenant_id=ts.tenant_id, plan_id=plan.id, price_id=price_id, customer_id=customer_id,
            success_url=_app_url("/settings/billing?checkout=success"),
            cancel_url=_app_url("/settings/billing?checkout=cancelled"),
        )
        await ts.flush()
        return {"method": "checkout_link", "plan_id": plan.id,
                "url": str(link.get("url") or ""), "expires_at": int(link.get("expires_at") or 0)}

    created = await provider.create_invoiced_subscription(
        customer_id=customer_id, price_id=price_id, tenant_id=ts.tenant_id, plan_id=plan.id,
        days_until_due=days_until_due,
        idempotency_key=f"admin-sub:{account}:{ts.tenant_id}:{plan.id}",
    )
    if sub is None:
        sub = await ensure_subscription(ts, plan_id=plan.id)
    take_plan_terms(sub, plan)
    sub.psp_subscription_id = str(created.get("id") or "")
    sub.psp_customer_id = customer_id
    sub.status = STRIPE_SUBSCRIPTION_STATUS.get(str(created.get("status") or ""), "active")
    sub.cancel_at_period_end = False
    start, end = _epoch(created.get("current_period_start")), _epoch(
        created.get("current_period_end"))
    if start is not None:
        sub.current_period_start = start
    if end is not None:
        sub.current_period_end = end
    sub.meta = {**(sub.meta or {}), "collection": "send_invoice",
                "credits_pending_plan": plan.id, "started_by": actor,
                "started_at": utcnow().isoformat(), "psp_customer_account": account}
    await ts.flush()
    return {"method": "invoice", "plan_id": plan.id, "status": sub.status,
            "psp_subscription_id": sub.psp_subscription_id,
            "invoice_url": str(created.get("hosted_invoice_url") or ""),
            "invoice_note": str(created.get("invoice_note") or ""),
            "days_until_due": days_until_due}
```

Before writing it, confirm `Membership` has `role` and `created_at` and `User` has `email`
(`grep -n "class Membership" -A 15 nexus/models/identity.py`). If `created_at` is absent, order by
`Membership.id` instead.

- [ ] **Step 4: Add the endpoint**

In `nexus/api/routers/admin_billing_write.py`, add `Literal` to the `typing` import (or
`from typing import Literal`) and `EmailStr` to the pydantic import, then after
`set_tenant_subscription`:

```python
class StripeSubscriptionIn(BaseModel):
    model_config = {"extra": "forbid"}

    plan_id: str = Field(min_length=1)
    method: Literal["invoice", "checkout_link"]
    # 14 by default (decided 2026-10-09), editable per subscription.
    days_until_due: int = Field(default=14, ge=1, le=90)
    billing_email: EmailStr | None = None


@router.post("/tenants/{tenant_id}/stripe-subscription")
async def start_stripe_subscription(
    tenant_id: str,
    body: StripeSubscriptionIn,
    principal: Principal = Depends(require_platform_permission(STRIPE_MANAGE)),
) -> dict:
    """Start a workspace on a Stripe subscription: by emailed invoice (starts now, credits when
    paid) or by a Checkout link for the customer (starts when paid). Superadmin only."""
    from nexus.billing.payments import PaymentError
    from nexus.billing.stripe_subscriptions import StripeSubscriptionError, start_subscription

    async with get_sessionmaker()() as session:
        await apply_rls(session, tenant_id)
        ts = TenantSession(session, tenant_id)
        try:
            result = await start_subscription(
                ts, plan_id=body.plan_id, method=body.method,
                days_until_due=body.days_until_due,
                billing_email=str(body.billing_email or ""), actor=principal.user_id,
            )
        except StripeSubscriptionError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        except PaymentError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"Stripe refused, nothing was changed here: {exc}"
            ) from exc
        await record_admin_action(
            session, actor=principal.user_id, action="subscription.stripe_start",
            target=body.plan_id, subject_tenant_id=tenant_id, after=result,
        )
        await session.commit()
    return result
```

- [ ] **Step 5: Run the test**

Run: `python -m pytest tests/test_stripe_admin_subscriptions.py -n0 -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add nexus/billing/stripe_subscriptions.py nexus/api/routers/admin_billing_write.py tests/test_stripe_admin_subscriptions.py
git commit -m "feat(billing): a superadmin starts a Stripe subscription by emailed invoice or Checkout link"
```

---

### Task 8: Changing a Stripe-billed workspace's plan changes it at Stripe

**Files:**
- Modify: `nexus/billing/stripe_subscriptions.py` (add `change_stripe_plan`)
- Modify: `nexus/api/routers/admin_billing_write.py` (`set_tenant_subscription`)
- Modify: `nexus/api/routers/admin_billing.py` (`SubscriptionOut.stripe_billed`)
- Test: append to `tests/test_stripe_admin_subscriptions.py`

- [ ] **Step 1: Find existing tests this routing could reach**

Run: `grep -rln "psp_subscription_id" tests | xargs grep -ln "tenants/.*/subscription\""`
For each hit, check whether it sets `psp_subscription_id` BEFORE posting a plan change. The
helper in `tests/test_stripe_operator_fixes.py` sets it after, so it is unaffected. A test that sets
it before now reaches Stripe routing: decide from its intent whether it must use a non-Stripe
workspace or expect the new behaviour, and record that in its docstring.

- [ ] **Step 2: Write the failing tests (append)**

```python
def _change(client, token, tid, plan_id="accelerate"):
    return client.post(f"/api/admin/billing/tenants/{tid}/subscription",
                       headers=auth(token), json={"plan_id": plan_id})


async def test_a_card_billed_plan_change_happens_at_stripe_with_credits_now(
        client, monkeypatch, stripe):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingSubscription

    boss, tid = await _boss_and_customer(client, monkeypatch, "sas8")
    sub = await _sub(tid)
    async with get_platform_sessionmaker()() as s:
        row = await s.get(BillingSubscription, sub.id)
        row.psp_subscription_id, row.psp_customer_id = "sub_card8", "cus_card8"
        await s.commit()
    r = await _change(client, boss, tid)
    assert r.status_code == 200, r.text
    assert stripe.price_changes[-1]["id"] == "sub_card8"
    assert stripe.price_changes[-1]["plan_id"] == "accelerate"
    after = await _sub(tid)
    assert after.plan_id == "accelerate" and "credits_pending_plan" not in after.meta


async def test_an_invoice_billed_plan_change_waits_for_payment(client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas9")
    assert (await _start(client, boss, tid, method="invoice")).status_code == 200
    r = await _change(client, boss, tid)
    assert r.status_code == 200, r.text
    after = await _sub(tid)
    assert after.plan_id == "accelerate"
    assert after.meta["credits_pending_plan"] == "accelerate"


async def test_finance_cannot_change_a_stripe_billed_plan_but_can_change_others(
        client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas10")
    finance = await _make_role(client, boss, "cfo@sas10.com", "finance", "sas10f")
    assert (await _change(client, finance, tid, "launch")).status_code == 200   # NEXUS-only
    assert (await _start(client, boss, tid, plan_id="accelerate",
                         method="invoice")).status_code == 200
    denied = await _change(client, finance, tid, "launch")
    assert denied.status_code == 403 and "superadmin" in denied.text


async def test_when_stripe_refuses_the_plan_stays(client, monkeypatch, stripe):
    from nexus.billing.payments import PaymentError

    boss, tid = await _boss_and_customer(client, monkeypatch, "sas11")
    assert (await _start(client, boss, tid, method="invoice")).status_code == 200

    async def refuse(**kw):
        raise PaymentError("stripe /subscriptions -> 400: no such item")

    monkeypatch.setattr(stripe, "change_subscription_price", refuse)
    r = await _change(client, boss, tid)
    assert r.status_code == 502
    assert (await _sub(tid)).plan_id == "launch"


async def test_the_subscription_list_says_which_workspaces_stripe_bills(
        client, monkeypatch, stripe):
    boss, tid = await _boss_and_customer(client, monkeypatch, "sas12")
    rows = (await client.get("/api/admin/billing/subscriptions", headers=auth(boss))).json()
    assert next(r for r in rows if r["tenant_id"] == tid)["stripe_billed"] is False
    assert (await _start(client, boss, tid, method="invoice")).status_code == 200
    rows = (await client.get("/api/admin/billing/subscriptions", headers=auth(boss))).json()
    assert next(r for r in rows if r["tenant_id"] == tid)["stripe_billed"] is True
```

Confirm the subscriptions list path before running:
`grep -n "response_model=list\[SubscriptionOut\]" -B 2 nexus/api/routers/admin_billing.py`.

- [ ] **Step 3: Run them to verify they fail**

Run: `python -m pytest tests/test_stripe_admin_subscriptions.py -n0 -q -p no:cacheprovider`
Expected: the five new tests FAIL (no Stripe call; no `stripe_billed`).

- [ ] **Step 4: Add `change_stripe_plan` to `nexus/billing/stripe_subscriptions.py`**

```python
async def change_stripe_plan(ts, sub, plan_id: str, *, actor: str = "system"):
    """Move a Stripe-billed workspace to ``plan_id`` AT STRIPE, now, prorated and invoiced.

    Our row changes only after Stripe agrees. Credits follow the subscription: a card subscription
    gets them now (keyed per plan per period, so the webhook's later grant is a no-op); an invoiced
    one gets them when the proration invoice is paid.
    """
    from nexus.billing.stripe_catalog import plan_price_id
    from nexus.billing.subscriptions import apply_plan_change_credits, take_plan_terms

    plan = await ts.session.get(BillingPlan, plan_id)
    if plan is None:
        raise StripeSubscriptionError(f"Unknown plan '{plan_id}'", 404)
    if plan.plan_class != "standard" or int(plan.base_price_cents or 0) <= 0:
        raise StripeSubscriptionError(
            f"'{plan.name}' cannot be billed through this Stripe subscription. Cancel the Stripe "
            "subscription first, then change the plan.")
    psp = (sub.psp_subscription_id or "").strip()
    provider = await resolve_payment_provider()
    if provider.name != "stripe" and psp.startswith("sub_"):
        raise StripeSubscriptionError(
            f"This subscription is billed through Stripe ({psp}). {NO_CREDENTIAL}")
    price_id = await plan_price_id(plan, provider)
    result = await provider.change_subscription_price(
        subscription_id=psp, price_id=price_id, plan_id=plan.id)
    previous = sub.plan_id
    take_plan_terms(sub, plan)
    invoiced = str(result.get("collection_method")
                   or (sub.meta or {}).get("collection") or "") == "send_invoice"
    sub.meta = {**(sub.meta or {}), "previous_plan_id": previous, "changed_by": actor,
                "changed_at": utcnow().isoformat(), "changed_via": "stripe",
                **({"credits_pending_plan": plan.id} if invoiced else {})}
    await ts.flush()
    if not invoiced:
        await apply_plan_change_credits(ts, plan)
    return sub
```

- [ ] **Step 5: Route `set_tenant_subscription`**

Replace its body inside `async with get_sessionmaker()() as session:` with:

```python
        if await session.get(BillingPlan, body.plan_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown plan '{body.plan_id}'")
        await apply_rls(session, tenant_id)
        ts = TenantSession(session, tenant_id)
        billed = await stripe_subscription_of(ts)
        if billed is not None:
            # Billed through Stripe: the change must happen at Stripe, or Stripe keeps charging
            # the old price while the workspace sits on the new plan (2026-10-09).
            if STRIPE_MANAGE not in await platform_permissions(principal):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "This workspace is billed through Stripe, so changing its plan changes what "
                    "Stripe charges. Only a superadmin can do that.",
                )
            try:
                sub = await change_stripe_plan(ts, billed, body.plan_id,
                                               actor=principal.user_id)
            except StripeSubscriptionError as exc:
                raise HTTPException(exc.status_code, str(exc)) from exc
            except PaymentError as exc:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    f"Stripe refused, nothing was changed here: {exc}",
                ) from exc
            via = "stripe"
        else:
            created = await ensure_subscription(ts, plan_id=body.plan_id)
            sub = created if created is not None else await change_plan(
                ts, body.plan_id, actor=principal.user_id
            )
            via = "nexus"
        await record_admin_action(
            session, actor=principal.user_id, action="subscription.change",
            target=body.plan_id, subject_tenant_id=tenant_id,
            after={"plan_id": sub.plan_id, "status": sub.status, "via": via},
        )
        await session.commit()
        return {"tenant_id": tenant_id, "plan_id": sub.plan_id, "status": sub.status}
```

with these imports at the top of the function:

```python
    from nexus.api.deps import platform_permissions
    from nexus.billing.payments import PaymentError
    from nexus.billing.stripe_subscriptions import (
        StripeSubscriptionError,
        change_stripe_plan,
        stripe_subscription_of,
    )
    from nexus.billing.subscriptions import change_plan, ensure_subscription
```

- [ ] **Step 6: `stripe_billed` on the subscription list**

In `nexus/api/routers/admin_billing.py`, add `stripe_billed: bool = False` to `SubscriptionOut`
and pass `stripe_billed=bool((sub.psp_subscription_id or "").strip()),` in its builder.

- [ ] **Step 7: Run the Stripe subscription, plan-change and lifecycle suites**

Run: `python -m pytest tests/test_stripe_admin_subscriptions.py tests/test_stripe_operator_fixes.py tests/test_billing_subscriptions.py tests/test_billing_lifecycle_wiring.py tests/test_billing_admin_writes.py tests/test_billing_custom_plans.py tests/test_plan_change_grants_credits.py -n0 -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add nexus/billing/stripe_subscriptions.py nexus/api/routers/admin_billing_write.py nexus/api/routers/admin_billing.py tests/test_stripe_admin_subscriptions.py
git commit -m "feat(billing): a Stripe-billed plan change happens at Stripe, prorated, superadmin only"
```

---

### Task 9: Activating a key for a new account syncs the plans (superadmin)

**Files:**
- Modify: `nexus/api/routers/admin_payment_credentials.py`
- Test: append to `tests/test_stripe_managed_plans.py`

- [ ] **Step 1: Write the failing tests (append)**

```python
async def _verified_credential() -> str:
    from nexus.billing.credentials import add_credential
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.payment_credential import PaymentCredential

    row = await add_credential(label="live", secret_key="sk_test_sync1",
                               webhook_secret="whsec_x")
    async with get_platform_sessionmaker()() as s:
        (await s.get(PaymentCredential, row.id)).status = "verified"
        await s.commit()
    return row.id


async def test_a_superadmin_activation_syncs_the_plans(client, monkeypatch):
    from nexus.billing import stripe_catalog
    from nexus.billing.payments import set_payment_provider

    set_payment_provider(None)
    token = await _superadmin(client, monkeypatch, slug="smp9", email="boss@smp9.com")

    async def fake_sync(session, provider, *, account=""):
        return [{"plan_id": "launch", "outcome": "created", "price_id": "price_x"}]

    monkeypatch.setattr(stripe_catalog, "sync_plans", fake_sync)
    cid = await _verified_credential()
    r = await client.post(f"/api/admin/billing/payment-credentials/{cid}/activate",
                          headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["stripe_sync"][0]["outcome"] == "created"
    set_payment_provider(None)


async def test_a_finance_activation_does_not_touch_stripe(client, monkeypatch):
    from nexus.billing import stripe_catalog
    from nexus.billing.payments import set_payment_provider

    set_payment_provider(None)
    boss = await _bootstrap_admin(client, monkeypatch, "smp10")
    finance = await _make_role(client, boss, "cfo@smp10.com", "finance", "smp10f")
    called: list = []

    async def fake_sync(session, provider, *, account=""):
        called.append(1)
        return []

    monkeypatch.setattr(stripe_catalog, "sync_plans", fake_sync)
    cid = await _verified_credential()
    r = await client.post(f"/api/admin/billing/payment-credentials/{cid}/activate",
                          headers=auth(finance))
    assert r.status_code == 200, r.text
    assert r.json().get("stripe_sync") is None and called == []
    set_payment_provider(None)
```

Confirm the credentials router prefix first:
`grep -n "APIRouter(prefix" nexus/api/routers/admin_payment_credentials.py`, and adjust the path in
both tests to match.

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_stripe_managed_plans.py -k activation -n0 -q -p no:cacheprovider`
Expected: FAIL (`KeyError: 'stripe_sync'`).

- [ ] **Step 3: Implement**

In `nexus/api/routers/admin_payment_credentials.py`, add to `CredentialOut`:

```python
    # Set only on activation by a superadmin: what syncing the plans to this account did.
    stripe_sync: list[dict] | None = None
```

add the helper:

```python
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
```

and at the end of `activate_payment_credential` replace `return _out(row)` with:

```python
    from nexus.api.deps import platform_permissions
    from nexus.billing.permissions import STRIPE_MANAGE

    out = _out(row)
    if STRIPE_MANAGE in await platform_permissions(principal):
        out = out.model_copy(update={"stripe_sync": await _sync_plans_after_activation()})
    return out
```

- [ ] **Step 4: Run the credential suites**

Run: `python -m pytest tests/test_stripe_managed_plans.py tests/test_payment_credentials.py tests/test_stripe_operator_fixes.py -n0 -q -p no:cacheprovider`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add nexus/api/routers/admin_payment_credentials.py tests/test_stripe_managed_plans.py
git commit -m "feat(billing): a superadmin activating a Stripe key syncs the plans into that account"
```

---

### Task 10: The console

**REQUIRED:** invoke the `impeccable` skill before editing UI (CLAUDE.md), and follow `DESIGN.md`.

**Files:**
- Modify: `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts`
- Modify: `frontend/src/pages/AdminBillingPage.tsx` (`Plans()`)
- Modify: `frontend/src/pages/admin/TenantActionsDialog.tsx`
- Modify: `frontend/src/pages/admin/PaymentsTab.tsx`
- Modify: `tests/test_plan_authoring_ui.py` if it asserts `PRICING_WRITE` gates New plan
- Test: `tests/test_stripe_managed_ui.py`

- [ ] **Step 1: Write the failing UI source test**

```python
# tests/test_stripe_managed_ui.py
"""The console offers the Stripe controls only to holders of `stripe.manage` (no frontend test
runner, so these read the source, like the other *_ui tests)."""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "frontend" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_plan_authoring_is_gated_on_stripe_manage():
    page = _read("pages/AdminBillingPage.tsx")
    assert "const canManageStripe = can(STRIPE_MANAGE);" in page
    assert "{canManageStripe && (" in page              # New plan / Sync toolbar
    assert "Sync to Stripe" in page
    assert "api.syncPlansToStripe()" in page


def test_the_dialog_starts_and_moves_stripe_subscriptions_for_superadmins_only():
    dialog = _read("pages/admin/TenantActionsDialog.tsx")
    assert "STRIPE_MANAGE" in dialog
    assert "Start a Stripe subscription" in dialog
    assert "api.startStripeSubscription(" in dialog
    assert "tenant?.stripe_billed" in dialog or "tenant.stripe_billed" in dialog
    assert "Email an invoice" in dialog and "Checkout link" in dialog


def test_the_client_and_types_carry_the_new_calls():
    api = _read("lib/api.ts")
    types = _read("lib/types.ts")
    assert "syncPlansToStripe()" in api and "/admin/billing/plans/stripe-sync" in api
    assert "startStripeSubscription(" in api and "/stripe-subscription" in api
    assert "stripe_billed: boolean;" in types
    assert "stripe_price_id: string;" in types


def test_activation_shows_the_sync_report():
    assert "stripe_sync" in _read("pages/admin/PaymentsTab.tsx")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_stripe_managed_ui.py -n0 -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 3: Types and client**

`frontend/src/lib/types.ts`: add to `AdminPlan` `stripe_price_id: string;` and
`stripe_account: string;`; add to `AdminSubscription` `stripe_billed: boolean;`; and add:

```ts
export interface StripeSyncRow {
  plan_id: string;
  outcome: "created" | "reused" | "failed";
  price_id?: string;
  detail?: string;
}

export type StripeSubscriptionStart =
  | { method: "invoice"; plan_id: string; status: string; psp_subscription_id: string;
      invoice_url: string; invoice_note: string; days_until_due: number }
  | { method: "checkout_link"; plan_id: string; url: string; expires_at: number };
```

If the payment credential type has a name (find it with `grep -n "interface PaymentCredential"
frontend/src/lib/types.ts`), add `stripe_sync?: StripeSyncRow[] | null;` to it.

`frontend/src/lib/api.ts`, after `setPlanStatus`:

```ts
  /** Make every plan on sale exist in the active Stripe account. Superadmin only. */
  syncPlansToStripe() {
    return this.request<{ account: string; plans: StripeSyncRow[] }>(
      "/admin/billing/plans/stripe-sync", { method: "POST" },
    );
  }
```

and after `setTenantSubscription`:

```ts
  /** Start a workspace on a Stripe subscription. Superadmin only. */
  startStripeSubscription(tenantId: string, body: {
    plan_id: string; method: "invoice" | "checkout_link";
    days_until_due?: number; billing_email?: string;
  }) {
    return this.request<StripeSubscriptionStart>(
      `/admin/billing/tenants/${tenantId}/stripe-subscription`, { method: "POST", body },
    );
  }
```

importing `StripeSyncRow` and `StripeSubscriptionStart` in the existing type import list.

- [ ] **Step 4: Plans tab**

In `Plans()` of `frontend/src/pages/AdminBillingPage.tsx`:
- import `STRIPE_MANAGE` with the other permission imports; add
  `const canManageStripe = can(STRIPE_MANAGE);` beside `canEdit` and a
  `const [syncing, setSyncing] = useState(false);`.
- the actions column renders when `canEdit || canManageStripe`; inside it, **Edit** and
  **Publish/Hold** render only when `canManageStripe`, **What's included** only when `canEdit`.
- add a column after Status, for standard plans with a price:

```tsx
    {
      key: "stripe",
      header: "Stripe",
      width: "140px",
      hideOnMobile: true,
      render: (r) =>
        r.plan_class === "standard" && r.base_price_cents > 0 ? (
          r.stripe_price_id ? (
            <Badge tone="success" title={`${r.stripe_price_id} in ${r.stripe_account}`}>
              In Stripe
            </Badge>
          ) : (
            <Badge tone="neutral">Not in Stripe</Badge>
          )
        ) : (
          <span className={styles.subtle}>—</span>
        ),
    },
```

- replace the toolbar block with:

```tsx
          {canManageStripe && (
            <div className={styles.tableToolbar}>
              <Button iconLeft={<Icons.PlusIcon />} onClick={() => setPlanForm(null)}>
                New plan
              </Button>
              <Button variant="secondary" loading={syncing} onClick={() => void syncToStripe()}>
                Sync to Stripe
              </Button>
            </div>
          )}
```

- add the handler:

```tsx
  async function syncToStripe() {
    setSyncing(true);
    try {
      const res = await api.syncPlansToStripe();
      const count = (o: string) => res.plans.filter((p) => p.outcome === o).length;
      const failed = res.plans.filter((p) => p.outcome === "failed");
      const summary = `${count("created")} created, ${count("reused")} already there`;
      if (failed.length) {
        toast.error(`Stripe refused ${failed.length} plan${failed.length === 1 ? "" : "s"}`,
          failed.map((p) => `${p.plan_id}: ${p.detail ?? ""}`).join(" · "));
      } else {
        toast.success(`Plans are in Stripe (${res.account})`, summary);
      }
      plans.refetch();
    } catch (err) {
      toast.error("Couldn't sync to Stripe",
        err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSyncing(false);
    }
  }
```

- the DataTable `minWidth` uses `canEdit || canManageStripe`.

If `Badge` has no `title` prop, wrap it: `<span title={...}><Badge tone="success">In Stripe</Badge></span>`.

- [ ] **Step 5: Tenant actions dialog**

In `frontend/src/pages/admin/TenantActionsDialog.tsx`:
- import `STRIPE_MANAGE`; `const canManageStripe = can(STRIPE_MANAGE);`;
  `const stripeBilled = Boolean(tenant?.stripe_billed);`.
- the proration preview effect returns early (no fetch) when `stripeBilled`; the plan-change
  `Field` hint reads, when `stripeBilled`: "Billed through Stripe: the price changes at Stripe now
  and Stripe invoices the prorated difference immediately." and, when not, the existing text.
- when `stripeBilled && !canManageStripe`, render
  `<p className={styles.hint}>Only a superadmin can change the plan of a workspace billed through Stripe.</p>`
  and disable the Change plan button.
- add state and the section (rendered when `canManageStripe && !stripeBilled`), placed after the
  plan-change block:

```tsx
  const [startPlan, setStartPlan] = useState("");
  const [method, setMethod] = useState<"invoice" | "checkout_link">("invoice");
  const [days, setDays] = useState("14");
  const [billingEmail, setBillingEmail] = useState("");
  const [started, setStarted] = useState<StripeSubscriptionStart | null>(null);

  const billable = plans.filter(
    (p) => p.plan_class === "standard" && p.status === "active" && p.base_price_cents > 0,
  );

  async function startStripe() {
    if (!tenant || !startPlan) return;
    setBusy("stripe");
    try {
      const res = await api.startStripeSubscription(tenant.tenant_id, {
        plan_id: startPlan, method, days_until_due: Number(days) || 14,
        ...(billingEmail.trim() ? { billing_email: billingEmail.trim() } : {}),
      });
      setStarted(res);
      if (res.method === "invoice") {
        toast.success(`${tenant.tenant_name} is on ${startPlan}`,
          "Stripe emailed the first invoice. Credits arrive when it is paid.");
        onDone();
      }
    } catch (err) {
      toast.error("Couldn't start the Stripe subscription",
        err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }
```

(extend the `busy` union with `"stripe"`), and the JSX:

```tsx
        {canManageStripe && !stripeBilled && (
          <fieldset className={styles.fieldset}>
            <legend className={styles.legend}>Start a Stripe subscription</legend>
            <Field label="Plan">
              <Select
                value={startPlan}
                onChange={(e) => setStartPlan(e.target.value)}
                placeholder="Select a plan…"
                options={billable.map((p) => ({
                  value: p.id,
                  label: `${p.name} — $${(p.base_price_cents / 100).toFixed(2)}/${p.interval}`,
                }))}
              />
            </Field>
            <Field label="How the customer pays">
              <Select
                value={method}
                onChange={(e) => setMethod(e.target.value as "invoice" | "checkout_link")}
                options={[
                  { value: "invoice", label: "Email an invoice (starts now, credits when paid)" },
                  { value: "checkout_link", label: "Checkout link (starts when the customer pays)" },
                ]}
              />
            </Field>
            {method === "invoice" && (
              <div className={styles.row}>
                <Field label="Days to pay" hint="14 unless agreed otherwise.">
                  <Input type="number" min="1" max="90" value={days}
                         onChange={(e) => setDays(e.target.value)} />
                </Field>
                <Field label="Billing email" hint="Leave blank to bill the workspace owner.">
                  <Input type="email" value={billingEmail}
                         onChange={(e) => setBillingEmail(e.target.value)}
                         placeholder="finance@customer.com" />
                </Field>
              </div>
            )}
            {started?.method === "checkout_link" && (
              <div className={styles.hint}>
                <p>Send this link to the customer. It expires{" "}
                  {new Date(started.expires_at * 1000).toLocaleString()}.</p>
                <div className={styles.actions}>
                  <Input readOnly value={started.url} aria-label="Checkout link" />
                  <Button variant="secondary"
                          onClick={() => void navigator.clipboard.writeText(started.url)}>
                    Copy link
                  </Button>
                </div>
              </div>
            )}
            {started?.method === "invoice" && started.invoice_url && (
              <p className={styles.hint}>
                <a href={started.invoice_url} target="_blank" rel="noreferrer">Open the invoice</a>
              </p>
            )}
            <div className={styles.actions}>
              <Button onClick={startStripe} loading={busy === "stripe"} disabled={!startPlan}>
                {method === "invoice" ? "Email an invoice" : "Create Checkout link"}
              </Button>
            </div>
          </fieldset>
        )}
```

Reset `started` when the dialog closes or `tenant` changes (add it to the existing reset effect,
or `useEffect(() => setStarted(null), [tenant, open]);`).

- [ ] **Step 6: Payments tab**

In `frontend/src/pages/admin/PaymentsTab.tsx`, where activation succeeds (find `activate` /
`activatePaymentCredential`), read `res.stripe_sync` and, when it is an array with rows, show a
toast: success "Plans synced to Stripe" with "N created, M already there", or error listing failed
rows' `detail` — the same wording as the Plans tab handler.

- [ ] **Step 7: Update `tests/test_plan_authoring_ui.py` if needed, then run UI tests and the build**

Run: `python -m pytest tests/test_stripe_managed_ui.py tests/test_plan_authoring_ui.py -n0 -q -p no:cacheprovider`
Expected: PASS (if `test_plan_authoring_ui.py` asserted `PRICING_WRITE` gates New plan, change it
to `STRIPE_MANAGE` with a comment citing 2026-10-09).

Run: `cd frontend && npm run typecheck && npm run build`
Expected: no type errors; build succeeds.

- [ ] **Step 8: Verify in the browser** (preview workflow): open the console Plans tab and a
workspace's actions as the local superadmin (credentials in the main repo's gitignored
`.claude/local-qa-logins.md` or `.claude/local/qa-logins.md`), confirm the controls render, the
dialog submits against the noop provider gracefully (409 "No Stripe credential"), and take a
screenshot for the summary.

- [ ] **Step 9: Commit**

```bash
git add frontend/src tests/test_stripe_managed_ui.py tests/test_plan_authoring_ui.py
git commit -m "feat(console): Stripe plans and subscriptions for superadmins"
```

---

### Task 11: Docs

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/billing/stripe-connection-guide.txt` (lives in the main checkout, untracked: copy it
  into the worktree first with `cp ../../../docs/billing/stripe-connection-guide.txt docs/billing/`)

- [ ] **Step 1: CLAUDE.md**

- Migrations: head becomes `0067_stripe_manage`; append `-> 0067 (stripe.manage backfill, no
  schema)` to the chain.
- Add a section after "Authoring a sellable plan": **Stripe-managed plans and subscriptions
  (`nexus/billing/stripe_catalog.py`, `stripe_subscriptions.py`, 2026-10-09)** stating: ids carry
  their account and are reused only inside it (the "No such price" report); one product per plan,
  new price only on new terms, superseded archived; publish creates the price first and refuses on a
  Stripe error; Sync to Stripe and the activation sync; start by emailed invoice (credits on
  `invoice.paid`) or Checkout link; Stripe-billed plan change at Stripe with `always_invoice`;
  `stripe.manage` superadmin-only, Finance keeps rate cards/entitlements; restricted key needs
  Subscriptions: Write.
- In "Authoring a sellable plan", replace "No Stripe object is created" with a pointer to the new
  section (drafts still create nothing).

- [ ] **Step 2: The Stripe guide**

In the permissions table move **Subscriptions** from Read to **Write** with "create a subscription
paid by invoice and change a Stripe-billed plan"; add a short section "Superadmin-only Stripe
actions" listing Sync to Stripe, Start a Stripe subscription (invoice / Checkout link), and plan
changes; note that in test mode Stripe emails invoices only to your own team's addresses.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/billing/stripe-connection-guide.txt
git commit -m "docs: Stripe-managed plans and subscriptions, superadmin only"
```

---

### Task 12: Verify, review, land

- [ ] **Step 1: Full suite locally (fast signal)**

Run: `python -m pytest tests -q -p no:cacheprovider -n 6`
Expected: all PASS. Any failure: read it; a test encoding a behaviour this plan deliberately
changes gets its expectation updated with a comment; anything else is a regression to fix first.

- [ ] **Step 2: CI-equivalent on Python 3.11** (mirrors `.github/workflows/ci.yml`, per CLAUDE.md
"CI and what passed locally has to mean"): `git archive` the branch head into the scratchpad,
`ruff check nexus tests`, unit tests in `python:3.11-slim` with
`pip install -e ".[dev,migrate,metrics]"` (no constraints), integration on a Docker network with
`postgres:16-alpine` + `redis:7-alpine` and `.[dev,postgres,redis]`, frontend `npm ci && npm run
typecheck && npm run build` on `node:20`, gitleaks 8.24.3 over `master..feat/stripe-managed-plans`.
Expected: every stage green. Report any red stage with its output; do not land on red.

- [ ] **Step 3: Code review** with the `code-review` skill at medium effort on
`master..feat/stripe-managed-plans`; fix confirmed findings with a test each.

- [ ] **Step 4: Update the code-review graph**: `git add` is done by the commits; run
`code-review-graph build` and confirm `stripe_catalog.plan_price_id` is present.

- [ ] **Step 5: Land on local master, ff-only, no push** (memory: commit onto master): from the main
checkout, confirm it has no uncommitted edits besides the untracked Stripe guide, then
`git merge --ff-only feat/stripe-managed-plans` (rebase the branch onto master first if master
moved). Do not push or deploy without the user's go-ahead.

---

## Added after approval (2026-10-09)

Two requests arrived while the plan was being executed; both were built test-first in this branch.

**A. Everything an account switch needs is in the panel** ("whether any other setting or keys
needed when we switch account ... add those in superadmin panel"). Runtime settings `app_base_url`
and `stripe_automatic_tax` (Configuration, Billing); `POST /admin/payment-credentials/{id}/webhook-
endpoint` and the Payments tab's "Create webhook in Stripe", which registers the endpoint in that
credential's account, pinned to the handled API version and events, and seals its signing secret.
Tests: `tests/test_stripe_switch_setup.py`.

**B. The old account's subscriptions move with the switch** ("if switch only payment collection
destination should change ... the old one should come to new"). `nexus/billing/stripe_carryover.py`
recreates every live subscription in the new account, first billed where the old paid period ends,
by card when the new account holds one and by emailed invoice otherwise, and ends the old one with
the old key; activation runs it, plan change and cancel apply it to a missed one, the portal opens
on the new account's customer, and "Move subscriptions here" retries. A subscription left in a
previous account answers with what to do (`in_previous_account`) only when nothing could move it.
Tests: `tests/test_stripe_carry_over.py`.
