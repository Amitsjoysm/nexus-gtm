# Billing Milestone 6 — Subscription Lifecycle & Admin Writes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every tenant a subscription, roll billing periods automatically, and let a platform
admin change plans, prices and credits from an API instead of a SQL client.

**The keystone is Task 1.** Right now *no tenant has a subscription at all*, so
`resolve_entitlement` falls through to catalog defaults for everyone. That is harmless while
enforcement is `shadow`, but it means enforcement could never be switched on without instantly
mis-gating every existing customer. Backfilling every tenant onto `legacy-unlimited` — a $0,
unlimited, `grandfathered` plan — is the single step that makes the platform safe to arm.

**Tech Stack:** Python 3.11, async SQLAlchemy 2.0, FastAPI, Alembic (no new tables), pytest.

**Run tests with `PYTEST_XDIST_WORKER=m6 py -3.10 -m pytest`.**

**Prerequisites:** M1–M5 merged.

**Design refs:** [05-Subscription-System](../../billing/05-Subscription-System.md) ·
[06-Admin-Portal](../../billing/06-Admin-Portal.md) ·
[07-Enterprise-Licensing](../../billing/07-Enterprise-Licensing.md) ·
[04-Pricing-Engine](../../billing/04-Pricing-Engine.md) §3

**Non-breaking guarantee:** no schema change (M1's `BillingSubscription` already carries every
field needed). The backfill is idempotent and only ever *adds* a subscription. Admin writes are
behind `require_platform_admin`, which no tenant role can reach.

---

## File structure

**Create:** `nexus/billing/subscriptions.py`, `nexus/api/routers/admin_billing_write.py`, tests
`test_billing_subscriptions.py`, `test_billing_admin_writes.py`
**Modify (append-only):** `nexus/main.py` (backfill on startup), `nexus/workers/tasks.py`
(period-roll handler), `nexus/workers/scheduler.py`, `nexus/api/routers/__init__.py`

---

## Task 1: Backfill every tenant onto the legacy plan

**Files:** Create `nexus/billing/subscriptions.py`; Test: `tests/test_billing_subscriptions.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_subscriptions.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def _seed():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()


async def test_backfill_gives_every_tenant_the_legacy_plan():
    """The safety keystone: an un-subscribed tenant must never be mis-gated when enforcement
    is armed, so everyone who predates billing lands on unlimited + grandfathered."""
    from nexus.billing.plans import LEGACY_PLAN_ID
    from nexus.billing.subscriptions import backfill_subscriptions
    from nexus.models.billing import BillingSubscription

    await _seed()
    t1 = await make_tenant(slug="bf1", name="BF One")
    t2 = await make_tenant(slug="bf2", name="BF Two")

    assert (await backfill_subscriptions())["created"] == 2
    for tid in (t1, t2):
        async with tenant_session(tid) as ts:
            subs = await ts.list(BillingSubscription)
            assert len(subs) == 1
            assert subs[0].plan_id == LEGACY_PLAN_ID
            assert subs[0].status == "active"
            assert subs[0].grandfathered is True


async def test_backfill_is_idempotent():
    from nexus.billing.subscriptions import backfill_subscriptions

    await _seed()
    await make_tenant(slug="bf3", name="BF Three")
    assert (await backfill_subscriptions())["created"] == 1
    assert (await backfill_subscriptions())["created"] == 0     # a redeploy changes nothing


async def test_backfill_never_overwrites_a_paying_tenant():
    """A tenant who has since upgraded must not be silently downgraded by a redeploy."""
    from nexus.billing.subscriptions import backfill_subscriptions
    from nexus.models.billing import BillingSubscription

    await _seed()
    tid = await make_tenant(slug="bf4", name="BF Four")
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="growth", status="active"))
        await ts.flush()

    assert (await backfill_subscriptions())["created"] == 0
    async with tenant_session(tid) as ts:
        subs = await ts.list(BillingSubscription)
        assert len(subs) == 1 and subs[0].plan_id == "growth"


async def test_backfill_sets_a_billing_period():
    from nexus.billing.subscriptions import backfill_subscriptions
    from nexus.models.billing import BillingSubscription

    await _seed()
    tid = await make_tenant(slug="bf5", name="BF Five")
    await backfill_subscriptions()
    async with tenant_session(tid) as ts:
        sub = (await ts.list(BillingSubscription))[0]
        assert sub.current_period_start is not None
        assert sub.current_period_end is not None
        assert sub.current_period_end > sub.current_period_start
```

- [ ] **Step 2: Run to verify it fails.** Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement**

```python
# nexus/billing/subscriptions.py
"""Subscription lifecycle: assignment, plan change, period roll.

Every tenant must hold exactly one subscription, because the entitlement chain resolves
plan -> capability. A tenant without one falls through to catalog defaults, which is fine in
shadow mode and dangerous the moment enforcement is armed — so ``backfill_subscriptions`` runs
on every boot and is the reason arming enforcement is safe.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from nexus.core.db import get_sessionmaker, utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.billing import BillingPlan, BillingSubscription

logger = logging.getLogger("nexus.billing.subscriptions")

ACTIVE_STATUSES = ("trialing", "active", "past_due")


def next_period_end(start: datetime, interval: str = "month") -> datetime:
    """End of the billing period beginning at ``start``.

    Calendar-month arithmetic, not "+30 days": customers reconcile invoices against calendar
    months, and a drifting anniversary makes every support conversation harder.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if interval == "year":
        return start.replace(year=start.year + 1)
    year, month = start.year, start.month
    if month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    # Clamp the day so the 31st of a month does not overflow a shorter one.
    day = min(start.day, [31, 29 if year % 4 == 0 and (year % 100 or not year % 400) else 28,
                          31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return start.replace(year=year, month=month, day=day)


async def ensure_subscription(
    ts: TenantSession, *, plan_id: str, status: str = "active", grandfathered: bool = False,
) -> BillingSubscription | None:
    """Give this tenant a subscription if it has none. Returns the new row, or None if one
    already existed — never modifies an existing subscription."""
    existing = await ts.list(BillingSubscription, limit=1)
    if existing:
        return None
    now = utcnow()
    sub = BillingSubscription(
        plan_id=plan_id, status=status, grandfathered=grandfathered,
        current_period_start=now, current_period_end=next_period_end(now),
    )
    ts.add(sub)
    await ts.flush()
    return sub


async def backfill_subscriptions() -> dict:
    """Put every tenant that has no subscription onto the legacy unlimited plan.

    Idempotent and additive: a tenant that has since upgraded is left completely alone, so a
    redeploy can never downgrade a paying customer.
    """
    from nexus.billing.plans import LEGACY_PLAN_ID
    from nexus.models.identity import Tenant

    async with get_sessionmaker()() as session:
        if await session.get(BillingPlan, LEGACY_PLAN_ID) is None:
            logger.warning("legacy plan missing; skipping subscription backfill")
            return {"created": 0, "skipped": "no_legacy_plan"}
        tenant_ids = list((await session.scalars(select(Tenant.id))).all())

    created = 0
    for tid in tenant_ids:
        try:
            async with get_sessionmaker()() as session:
                ts = TenantSession(session, tid)
                sub = await ensure_subscription(
                    ts, plan_id=LEGACY_PLAN_ID, status="active", grandfathered=True,
                )
                if sub is not None:
                    created += 1
                await session.commit()
        except Exception:  # one bad tenant must not stop the backfill
            logger.warning("subscription backfill failed for %s", tid, exc_info=True)
    if created:
        logger.info("subscription backfill: %d tenant(s) placed on %s", created, LEGACY_PLAN_ID)
    return {"created": created}
```

- [ ] **Step 4: Run** → PASS (4 tests)
- [ ] **Step 5: Commit** — `git commit -m "feat(billing): backfill every tenant onto the legacy plan"`

---

## Task 2: Run the backfill on startup

**Files:** Modify `nexus/main.py`

- [ ] **Step 1:** In the existing billing-seed `try` block in the lifespan, after `sync_rates()`:

```python
            from nexus.billing.subscriptions import backfill_subscriptions

            await backfill_subscriptions()
```

It must stay INSIDE the existing try/except so a backfill failure can never stop the app booting.

- [ ] **Step 2: Run** `PYTEST_XDIST_WORKER=m6 py -3.10 -m pytest tests/test_api.py -q` → PASS
- [ ] **Step 3: Commit** — `git commit -m "feat(billing): backfill subscriptions on startup"`

---

## Task 3: Plan changes

**Files:** Modify `nexus/billing/subscriptions.py`; Test: append

- [ ] **Step 1: Write the failing test (append)**

```python
async def test_change_plan_switches_the_active_subscription():
    from nexus.billing.subscriptions import change_plan, ensure_subscription
    from nexus.models.billing import BillingSubscription

    await _seed()
    tid = await make_tenant(slug="cp1", name="CP One")
    async with tenant_session(tid) as ts:
        await ensure_subscription(ts, plan_id="free")
        sub = await change_plan(ts, "growth", actor="admin@nexus")
        assert sub.plan_id == "growth" and sub.status == "active"
        assert len(await ts.list(BillingSubscription)) == 1     # switched, not duplicated
        assert sub.meta["previous_plan_id"] == "free"           # auditable


async def test_change_plan_clears_grandfathering():
    """Choosing a new plan is choosing its current terms; frozen legacy pricing does not
    survive an upgrade."""
    from nexus.billing.plans import LEGACY_PLAN_ID
    from nexus.billing.subscriptions import change_plan, ensure_subscription

    await _seed()
    tid = await make_tenant(slug="cp2", name="CP Two")
    async with tenant_session(tid) as ts:
        await ensure_subscription(ts, plan_id=LEGACY_PLAN_ID, grandfathered=True)
        sub = await change_plan(ts, "growth", actor="admin@nexus")
        assert sub.grandfathered is False


async def test_change_plan_rejects_an_unknown_plan():
    from nexus.billing.subscriptions import change_plan, ensure_subscription

    await _seed()
    tid = await make_tenant(slug="cp3", name="CP Three")
    async with tenant_session(tid) as ts:
        await ensure_subscription(ts, plan_id="free")
        try:
            await change_plan(ts, "no-such-plan", actor="admin@nexus")
        except ValueError:
            return
        raise AssertionError("change_plan must reject an unknown plan")


async def test_cancel_at_period_end_keeps_service_running():
    """Cancelling is not cutting off — the customer paid through the period."""
    from nexus.billing.subscriptions import cancel_subscription, ensure_subscription

    await _seed()
    tid = await make_tenant(slug="cp4", name="CP Four")
    async with tenant_session(tid) as ts:
        await ensure_subscription(ts, plan_id="growth")
        sub = await cancel_subscription(ts, at_period_end=True)
        assert sub.cancel_at_period_end is True
        assert sub.status == "active"
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement** (append to `nexus/billing/subscriptions.py`)

```python
async def _active(ts: TenantSession) -> BillingSubscription | None:
    subs = await ts.session.scalars(
        ts.select(BillingSubscription, BillingSubscription.status.in_(ACTIVE_STATUSES))
        .order_by(BillingSubscription.created_at.desc(), BillingSubscription.id.desc())
    )
    return subs.first()


async def change_plan(ts: TenantSession, plan_id: str, *, actor: str = "system"):
    """Move a tenant to another plan, in place.

    Switching the existing row rather than opening a second one keeps "one subscription per
    tenant" true at all times — two active rows would make rating ambiguous.
    """
    plan = await ts.session.get(BillingPlan, plan_id)
    if plan is None:
        raise ValueError(f"unknown plan: {plan_id}")
    sub = await _active(ts)
    if sub is None:
        return await ensure_subscription(ts, plan_id=plan_id)

    previous = sub.plan_id
    sub.plan_id = plan_id
    sub.status = "active"
    sub.currency = plan.currency
    sub.interval = plan.interval
    # Grandfathered terms are frozen legacy pricing; taking a new plan means taking its terms.
    sub.grandfathered = False
    sub.cancel_at_period_end = False
    sub.meta = {
        **(sub.meta or {}),
        "previous_plan_id": previous,
        "changed_by": actor,
        "changed_at": utcnow().isoformat(),
    }
    await ts.flush()
    return sub


async def cancel_subscription(ts: TenantSession, *, at_period_end: bool = True):
    """Cancel. At period end by default — the customer paid through it."""
    sub = await _active(ts)
    if sub is None:
        return None
    if at_period_end:
        sub.cancel_at_period_end = True
    else:
        sub.status = "canceled"
        sub.cancel_at_period_end = False
    await ts.flush()
    return sub
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** — `git commit -m "feat(billing): plan change and cancellation"`

---

## Task 4: Period roll

**Files:** Modify `nexus/billing/subscriptions.py`, `nexus/workers/tasks.py`,
`nexus/workers/scheduler.py`; Test: append

- [ ] **Step 1: Write the failing test (append)**

```python
async def test_period_roll_closes_rates_and_advances():
    """Close the books: rate the period, finalize the invoice, grant the new period's credits,
    then advance the window."""
    from nexus.billing.rollups import period_key
    from nexus.billing.subscriptions import ensure_subscription, roll_period
    from nexus.billing.credits import balance
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingInvoice, BillingSubscription
    from datetime import timedelta

    await _seed()
    tid = await make_tenant(slug="rp1", name="RP One")
    async with tenant_session(tid) as ts:
        sub = await ensure_subscription(ts, plan_id="growth")   # 2000 included credits
        sub.current_period_end = utcnow() - timedelta(minutes=1)   # due
        await ts.flush()

        rolled = await roll_period(ts)
        assert rolled is True

        inv = (await ts.list(BillingInvoice))[0]
        assert inv.status == "finalized"

        assert await balance(ts) == 2000                        # new period's credits granted

        sub = (await ts.list(BillingSubscription))[0]
        assert sub.current_period_end > utcnow()                # window advanced


async def test_period_roll_is_a_noop_before_the_period_ends():
    from nexus.billing.subscriptions import ensure_subscription, roll_period

    await _seed()
    tid = await make_tenant(slug="rp2", name="RP Two")
    async with tenant_session(tid) as ts:
        await ensure_subscription(ts, plan_id="growth")          # period ends in a month
        assert await roll_period(ts) is False


async def test_period_roll_grants_credits_exactly_once():
    """Idempotency at the money boundary: a retried job must not double-grant."""
    from nexus.billing.credits import balance
    from nexus.billing.subscriptions import ensure_subscription, roll_period
    from nexus.core.db import utcnow
    from datetime import timedelta

    await _seed()
    tid = await make_tenant(slug="rp3", name="RP Three")
    async with tenant_session(tid) as ts:
        sub = await ensure_subscription(ts, plan_id="growth")
        sub.current_period_end = utcnow() - timedelta(minutes=1)
        await ts.flush()
        await roll_period(ts)
        first = await balance(ts)

        # Force it due again and re-roll within the same calendar month. The grant is keyed by
        # period, so the second roll resolves to the same key and must not grant again.
        sub.current_period_end = utcnow() - timedelta(minutes=1)
        await ts.flush()
        await roll_period(ts)
        assert await balance(ts) == first           # exactly once, not twice
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement** (append to `nexus/billing/subscriptions.py`)

```python
async def roll_period(ts: TenantSession) -> bool:
    """Close the current period if it has ended. Returns True if a roll happened.

    Order matters: rate and finalize BEFORE advancing the window, so the invoice describes the
    period that just closed rather than the one starting.
    """
    from nexus.billing.credits import grant_credits
    from nexus.billing.rating import finalize_invoice, rate_period
    from nexus.billing.rollups import period_key, rebuild_rollups

    sub = await _active(ts)
    if sub is None or sub.current_period_end is None:
        return False
    now = utcnow()
    end = sub.current_period_end
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if end > now:
        return False

    closing_key = period_key(sub.current_period_start or end, "period")

    # Full-window rebuild: the heartbeat sweep only covers the current period, so stragglers
    # written just after the boundary are folded in here, at the one moment it matters.
    await rebuild_rollups(ts)
    invoice = await rate_period(ts, period_key=closing_key)
    await finalize_invoice(ts, invoice.id)

    if sub.cancel_at_period_end:
        sub.status = "canceled"
        await ts.flush()
        return True

    sub.current_period_start = now
    sub.current_period_end = next_period_end(now, sub.interval)
    await ts.flush()

    plan = await ts.session.get(BillingPlan, sub.plan_id)
    if plan is not None and plan.included_credits:
        new_key = period_key(now, "period")
        await grant_credits(
            ts, plan.included_credits, kind="grant",
            reason=f"{plan.name} included credits",
            # Keyed by period, so a retried job grants once and only once.
            idempotency_key=f"plan_grant:{new_key}", period_key=new_key,
        )
    return True
```

- [ ] **Step 4: Add the worker handler** in `nexus/workers/tasks.py` (append-only, mirroring
`handle_rollup_usage`): scan tenants with a subscription whose `current_period_end <= now`, then
`roll_period` each in its own tenant session, swallowing per-tenant errors. Register as
`HANDLERS["roll_billing_periods"]` with an `enqueue_roll_billing_periods`.

- [ ] **Step 5: Schedule it** in `nexus/workers/scheduler.py` alongside `enqueue_rollup_usage`,
outside the automation gates, and update the two scheduler count assertions (`test_continuous_automation.py`,
`test_crm_auto_sync.py`) to include `"roll_billing_periods"`. **Read those tests before editing
and change only the counts/job-sets, nothing else.**

- [ ] **Step 6: Run** the subscription tests plus `tests/test_continuous_automation.py` and
`tests/test_crm_auto_sync.py` → PASS
- [ ] **Step 7: Commit** — `git commit -m "feat(billing): automatic period roll with once-only credit grants"`

---

## Task 5: Admin write API

**Files:** Create `nexus/api/routers/admin_billing_write.py`; Test: `tests/test_billing_admin_writes.py`

Every route uses `Depends(require_platform_admin)`. Pricing must be changeable without a deploy —
that is the whole premise — but only by a platform admin, never by a tenant role.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_admin_writes.py
from __future__ import annotations

from tests.conftest import auth, signup  # `client` is an auto-discovered conftest fixture


async def test_admin_writes_reject_a_tenant_owner(client):
    """A workspace owner is not a platform admin. Tenant RBAC must grant nothing here."""
    token = await signup(client, slug="aw1", email="o@aw1.com", company="AW1")
    r = await client.patch("/api/admin/billing/plans/growth",
                           headers=auth(token), json={"base_price_cents": 1})
    assert r.status_code in (401, 403)


async def test_admin_writes_reject_anonymous(client):
    r = await client.patch("/api/admin/billing/plans/growth", json={"base_price_cents": 1})
    assert r.status_code in (401, 403)


async def test_platform_admin_can_reprice_a_plan(client, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()
    settings = get_settings()
    monkeypatch.setattr(settings, "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="aw2", email="boss@nexus.com", company="AW2")

    r = await client.patch("/api/admin/billing/plans/growth", headers=auth(token),
                           json={"base_price_cents": 8900})
    assert r.status_code == 200, r.text
    assert r.json()["base_price_cents"] == 8900


async def test_rate_card_write_refuses_a_below_floor_price(client, monkeypatch):
    """The margin floor is enforced at the API too, not just in the seed — an admin must not be
    able to click past it without recording an exception."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.rates import sync_rates
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_rates()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    token = await signup(client, slug="aw3", email="boss@nexus.com", company="AW3")

    r = await client.put("/api/admin/billing/rates/ai.account_qa", headers=auth(token),
                         json={"credits_per_unit": 1})     # $0.01 against $0.012 COGS
    assert r.status_code == 422
    assert "margin" in r.text.lower()

    r = await client.put("/api/admin/billing/rates/ai.account_qa", headers=auth(token),
                         json={"credits_per_unit": 1, "margin_exception": True,
                               "margin_exception_reason": "strategic loss leader"})
    assert r.status_code == 200, r.text
```

- [ ] **Step 2: Run to verify it fails.** Expected: 404 (routes absent).

- [ ] **Step 3: Implement** the router with:
  - `PATCH /admin/billing/plans/{plan_id}` — partial update of the mutable plan fields
    (`name`, `description`, `status`, `base_price_cents`, `seat_price_cents`,
    `included_credits`, `max_seats`, `trial_days`, `sort_order`). Reject unknown fields.
  - `PUT /admin/billing/plans/{plan_id}/entitlements/{capability_id}` — upsert one entitlement.
  - `PUT /admin/billing/rates/{capability_id}` — upsert a rate card, calling
    `validate_rate(...)` against the stored `BillingCostRate` and returning **422** with the
    `MarginFloorError` message unless `margin_exception` is set.
  - `POST /admin/billing/tenants/{tenant_id}/subscription` — `change_plan` for one tenant.
  - `POST /admin/billing/tenants/{tenant_id}/credits` — `grant_credits`, requiring an explicit
    `idempotency_key` in the body so a double-click cannot double-grant.

  Register in `nexus/api/routers/__init__.py` (append-only).

- [ ] **Step 4: Run** → PASS (4 tests). **Step 5: Commit** —
`git commit -m "feat(billing): platform-admin write API with the margin floor enforced"`

---

## Task 6: Gate

- [ ] `PYTEST_XDIST_WORKER=m6 py -3.10 -m pytest tests/ -k billing -q` → all pass
- [ ] `py -3.10 -m ruff check nexus/ tests/ migrations/` → All checks passed
- [ ] Confirm `billing_enforcement` still defaults to `shadow`.
- [ ] Orchestrator runs the full suite.

---

## Self-review

**Spec coverage:** subscription assignment + grandfathering
([05](../../billing/05-Subscription-System.md) §1) → T1/T2; plan change + cancellation
([05](../../billing/05-Subscription-System.md) §3) → T3; period close, invoice, credit grant
([04](../../billing/04-Pricing-Engine.md) §3, [05](../../billing/05-Subscription-System.md) §4)
→ T4; admin control of plans/prices/credits without a deploy
([06](../../billing/06-Admin-Portal.md) §2) → T5.
Deferred: PSP collection + dunning (needs live Stripe keys; the adapter stays inert until
`NEXUS_PAYMENT_PROVIDER` is set, matching every other provider seam in this repo), seat-day
proration, credit expiry sweep, enterprise contract import
([07](../../billing/07-Enterprise-Licensing.md)).

**Placeholder scan:** T4 step 4 and T5 step 3 describe the handler/router rather than pasting
them — deliberate, because both must mirror existing code the implementer has to read first
(`handle_rollup_usage`, `admin_billing.py`). Every other step ships complete code.

**Type consistency:** `ensure_subscription`/`change_plan`/`cancel_subscription`/`roll_period`
all take `TenantSession` first. `next_period_end(start, interval)` used by T1 and T4.
`grant_credits(ts, amount, *, kind, reason, idempotency_key, period_key)` matches M4's signature.
`rate_period(ts, period_key=)` and `finalize_invoice(ts, invoice_id)` match M4. `LEGACY_PLAN_ID`
imported from `nexus.billing.plans` as defined in M1.
