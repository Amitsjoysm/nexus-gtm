# Billing Milestone 2 — Entitlement Engine + Metering Seam — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the single enforcement seam — `check_and_meter()` — that resolves entitlements and records immutable usage events, running in **shadow mode** so it changes no product behavior.

**Architecture:** One function is the only billing touchpoint application code ever sees. It resolves the tenant's entitlement for a capability (subscription → plan entitlement → catalog default), decides allow/warn/block/throttle, and appends an idempotent usage event. Enforcement is globally gated by `NEXUS_BILLING_ENFORCEMENT` (`off`/`shadow`/`on`) and per-capability by the catalog `default_mode`. **Unknown capability ⇒ allow + log. Engine exception ⇒ allow + log.** Regression is therefore impossible by construction.

**Tech Stack:** Python 3.11, async SQLAlchemy 2.0, Pydantic v2, Alembic, pytest (`asyncio_mode=auto`), offline SQLite.

**Run tests with `py -3.10 -m pytest`.**

**Prerequisite:** Milestone 1 merged (`docs/superpowers/plans/2026-07-28-billing-m1-foundation.md`) — catalog, plans, entitlements, subscriptions, platform admins all exist.

**Design refs:** [01-Billing-Architecture](../../billing/01-Billing-Architecture.md) §5–6,
[02-Entitlement-Engine](../../billing/02-Entitlement-Engine.md),
[03-Metering-Architecture](../../billing/03-Metering-Architecture.md),
[15-Migration-Strategy](../../billing/15-Migration-Strategy.md).

**Non-breaking guarantee:** new tables + new module only. No existing endpoint or worker is
modified in this milestone (integration is M5). The seam exists and is unit-tested, but nothing
calls it from product code yet.

---

## File structure

**Create:**
- `nexus/billing/usage.py` — usage-event model helpers + `record_usage()` (Task 2)
- `nexus/billing/entitlements.py` — resolution + `check_and_meter()` (Task 3, 4)
- `nexus/billing/errors.py` — `QuotaExceeded` / `BillingThrottled` exceptions (Task 3)
- `migrations/versions/0022_billing_usage.py` (Task 5)
- `tests/test_billing_usage.py`, `tests/test_billing_entitlements.py`, `tests/test_billing_shadow_safety.py`

**Modify (append-only):**
- `nexus/models/billing.py` — add `BillingUsageEvent`, `BillingUsageRollup` (Task 1)
- `nexus/models/__init__.py` — register the 2 new models (Task 1)

---

## Task 1: Usage-event + rollup models

**Files:** Modify `nexus/models/billing.py`, `nexus/models/__init__.py`; Test: `tests/test_billing_usage.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_usage.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


def test_usage_models_registered():
    import nexus.models as m

    assert hasattr(m, "BillingUsageEvent")
    assert hasattr(m, "BillingUsageRollup")


async def test_usage_event_round_trip_is_tenant_scoped():
    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(
            BillingUsageEvent(
                capability_id="ai.email_draft", quantity=1, unit="action",
                source="api", idempotency_key="req-1", occurred_at=utcnow(),
                attrs={"tokens_in": 1500},
            )
        )
        await ts.flush()
        rows = await ts.list(BillingUsageEvent)
        assert len(rows) == 1
        assert rows[0].tenant_id == tid
        assert rows[0].attrs["tokens_in"] == 1500
        assert rows[0].quantity == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_billing_usage.py -v`
Expected: FAIL — `ImportError: cannot import name 'BillingUsageEvent'`

- [ ] **Step 3: Add the models**

Append to `nexus/models/billing.py`:

```python
class BillingUsageEvent(IdMixin, TimestampMixin, TenantScoped, Base):
    """One immutable, idempotent record of a billable action.

    This is the system of truth for billing: invoices, quotas, and margin reports are all
    derived from this stream (docs/billing/03-Metering-Architecture.md §1). Rows are never
    updated or deleted inside the retention window — corrections are compensating rows with a
    negative ``quantity``.

    ``unit_cost_usd`` is stamped AT WRITE TIME from the cost-rate table so margin reports reflect
    the cost when the action happened, immune to later provider repricing.
    """

    __tablename__ = "billing_usage_events"
    __table_args__ = (
        # Replay safety: a retried queue job or duplicated webhook can never double-bill.
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_usage_idempotency"),
        Index("ix_usage_tenant_cap_time", "tenant_id", "capability_id", "occurred_at"),
        Index("ix_usage_occurred", "occurred_at"),
    )

    capability_id: Mapped[str] = mapped_column(String(80), index=True)
    # Numeric, not int: tokens and GB are fractional units.
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), default=0, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), default="action")
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(20), default="api")  # api|worker|middleware|system
    idempotency_key: Mapped[str] = mapped_column(String(120))
    attrs: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    unit_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 8), nullable=True)
    billed_credits: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime(), index=True)


class BillingUsageRollup(IdMixin, TimestampMixin, TenantScoped, Base):
    """Pre-aggregated usage per tenant/capability/period — the fast path for quota checks and
    dashboards. Derived from events; safe to rebuild at any time."""

    __tablename__ = "billing_usage_rollups"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "capability_id", "period_kind", "period_key",
            name="uq_usage_rollup_period",
        ),
        Index("ix_usage_rollup_lookup", "tenant_id", "capability_id", "period_kind"),
    )

    capability_id: Mapped[str] = mapped_column(String(80), index=True)
    period_kind: Mapped[str] = mapped_column(String(10))       # hour | day | period
    period_key: Mapped[str] = mapped_column(String(40))        # "2026-07-28T14" | "2026-07-28"
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), default=0, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(14, 8), default=0, nullable=False)
```

Add `Numeric` to the sqlalchemy import list at the top of the file.

- [ ] **Step 4: Register the models**

In `nexus/models/__init__.py`, add `BillingUsageEvent,` and `BillingUsageRollup,` to the existing
`from nexus.models.billing import (...)` block and to `__all__`.

- [ ] **Step 5: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_billing_usage.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add nexus/models/billing.py nexus/models/__init__.py tests/test_billing_usage.py
git commit -m "feat(billing): immutable usage-event + rollup models"
```

---

## Task 2: record_usage() — idempotent event append

**Files:** Create `nexus/billing/usage.py`; Test: append to `tests/test_billing_usage.py`

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_billing_usage.py  (append)
async def test_record_usage_is_idempotent():
    """The same idempotency key must never produce a second billable row — queue retries and
    duplicated webhooks are routine in production."""
    from nexus.billing.usage import record_usage
    from nexus.models.billing import BillingUsageEvent

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        first = await record_usage(
            ts, capability_id="ai.email_draft", quantity=1,
            idempotency_key="run-7:step-2", unit="action",
        )
        second = await record_usage(
            ts, capability_id="ai.email_draft", quantity=1,
            idempotency_key="run-7:step-2", unit="action",
        )
        assert first is True and second is False        # second was a no-op
        assert len(await ts.list(BillingUsageEvent)) == 1


async def test_record_usage_without_key_autogenerates():
    from nexus.billing.usage import record_usage
    from nexus.models.billing import BillingUsageEvent

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await record_usage(ts, capability_id="api.request", quantity=1)
        await record_usage(ts, capability_id="api.request", quantity=1)
        # No key supplied -> each call is a distinct event (blanket meters are high-volume).
        assert len(await ts.list(BillingUsageEvent)) == 2


async def test_record_usage_never_raises_on_bad_input():
    """Metering must never take the product down (docs/billing/01 §6)."""
    from nexus.billing.usage import record_usage

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        assert await record_usage(ts, capability_id="", quantity=1) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_billing_usage.py -k record -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.billing.usage'`

- [ ] **Step 3: Write the implementation**

```python
# nexus/billing/usage.py
"""Usage recording: the append-only, idempotent write path for the metering engine.

Two hard rules (docs/billing/01-Billing-Architecture.md §6):
  1. Recording usage must NEVER raise into product code. A metering outage degrades telemetry,
     never the feature.
  2. The same idempotency key must never bill twice.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.billing import BillingUsageEvent

logger = logging.getLogger("nexus.billing.usage")


async def record_usage(
    ts: TenantSession,
    *,
    capability_id: str,
    quantity: float = 1,
    unit: str = "action",
    user_id: str | None = None,
    source: str = "api",
    idempotency_key: str | None = None,
    attrs: dict | None = None,
    unit_cost_usd: float | None = None,
) -> bool:
    """Append one usage event. Returns True if a row was written, False if it was a no-op.

    A no-op means either a duplicate idempotency key (already billed) or a swallowed error —
    both are safe outcomes for the caller, which never needs to branch on the result.
    """
    if not capability_id:
        logger.warning("record_usage called without capability_id; ignoring")
        return False
    try:
        key = idempotency_key or f"auto:{uuid.uuid4().hex}"
        if idempotency_key:
            existing = (
                await ts.session.scalars(
                    ts.select(
                        BillingUsageEvent, BillingUsageEvent.idempotency_key == key
                    ).limit(1)
                )
            ).first()
            if existing is not None:
                return False
        ts.add(
            BillingUsageEvent(
                capability_id=capability_id, quantity=quantity, unit=unit, user_id=user_id,
                source=source, idempotency_key=key, attrs=attrs or {},
                unit_cost_usd=unit_cost_usd, occurred_at=utcnow(),
            )
        )
        await ts.flush()
        return True
    except Exception:  # metering must never break the product
        logger.warning("record_usage failed for %s", capability_id, exc_info=True)
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_billing_usage.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/usage.py tests/test_billing_usage.py
git commit -m "feat(billing): idempotent usage recording"
```

---

## Task 3: Entitlement resolution

**Files:** Create `nexus/billing/errors.py`, `nexus/billing/entitlements.py`; Test: `tests/test_billing_entitlements.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_entitlements.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def _seed(plan_id: str = "growth"):
    """Catalog + plans + a subscription on `plan_id`. Returns the tenant id."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.models.billing import BillingSubscription

    await sync_catalog()
    await sync_plans()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id=plan_id, status="active"))
        await ts.flush()
    return tid


async def test_resolve_uses_plan_entitlement_when_present():
    from nexus.billing.entitlements import resolve_entitlement

    tid = await _seed("free")
    async with tenant_session(tid) as ts:
        ent = await resolve_entitlement(ts, "ai.email_draft")
        assert ent.mode == "metered"
        assert ent.quota == 20          # from the Free plan seed
        assert ent.source == "plan"


async def test_resolve_falls_back_to_catalog_default():
    """A capability the plan says nothing about falls back to the catalog's safe default."""
    from nexus.billing.entitlements import resolve_entitlement

    tid = await _seed("growth")
    async with tenant_session(tid) as ts:
        ent = await resolve_entitlement(ts, "ai.research_brief")
        assert ent.mode == "metered"    # catalog default_mode
        assert ent.quota is None        # unlimited unless a plan says otherwise
        assert ent.source == "catalog"


async def test_unknown_capability_resolves_to_allow():
    """Shadow-safety: an unregistered capability must never block a feature."""
    from nexus.billing.entitlements import resolve_entitlement

    tid = await _seed()
    async with tenant_session(tid) as ts:
        ent = await resolve_entitlement(ts, "totally.unknown")
        assert ent.mode == "shadow"
        assert ent.source == "unknown"


async def test_no_subscription_resolves_to_catalog_default():
    """A tenant with no subscription row (pre-billing) is never blocked."""
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.billing.catalog import sync_catalog

    await sync_catalog()
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        ent = await resolve_entitlement(ts, "ai.email_draft")
        assert ent.source in ("catalog", "unknown")
        assert ent.mode != "disabled"


async def test_unlimited_plan_class_overrides_everything():
    """legacy-unlimited tenants keep every capability regardless of plan entitlement rows."""
    from nexus.billing.entitlements import resolve_entitlement

    tid = await _seed("legacy-unlimited")
    async with tenant_session(tid) as ts:
        ent = await resolve_entitlement(ts, "ai.email_draft")
        assert ent.mode == "unlimited"
        assert ent.quota is None
        assert ent.source == "plan_class"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_billing_entitlements.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.billing.entitlements'`

- [ ] **Step 3: Write errors.py**

```python
# nexus/billing/errors.py
"""Billing exceptions. Routers translate these into 402/429 with upgrade context."""
from __future__ import annotations


class BillingError(Exception):
    """Base for billing enforcement failures."""


class QuotaExceeded(BillingError):
    """The tenant's plan does not permit this action right now (HTTP 402).

    Carries everything the UI needs to render a useful upsell instead of a dead error.
    """

    def __init__(
        self, capability_id: str, *, reason: str, used: float = 0,
        quota: int | None = None, plan_id: str | None = None,
    ):
        self.capability_id = capability_id
        self.reason = reason          # quota_exhausted | disabled | dependency
        self.used = used
        self.quota = quota
        self.plan_id = plan_id
        super().__init__(f"{capability_id}: {reason}")

    def to_payload(self) -> dict:
        return {
            "error": "quota_exceeded",
            "capability": self.capability_id,
            "reason": self.reason,
            "used": self.used,
            "quota": self.quota,
            "plan": self.plan_id,
            "upgrade_url": "/settings/billing",
        }


class BillingThrottled(BillingError):
    """A burst/rate/cooldown limit was hit (HTTP 429)."""

    def __init__(self, capability_id: str, *, retry_after_s: int):
        self.capability_id = capability_id
        self.retry_after_s = retry_after_s
        super().__init__(f"{capability_id}: throttled")
```

- [ ] **Step 4: Write entitlements.py (resolution only — the decision engine lands in Task 4)**

```python
# nexus/billing/entitlements.py
"""The entitlement engine: resolve policy, decide, and meter — the ONE billing seam.

Resolution order (docs/billing/02-Entitlement-Engine.md §2):
    plan class 'unlimited'  ->  plan entitlement  ->  catalog default  ->  unknown (allow)

Everything about this module is biased toward NOT breaking the product: unknown capabilities,
missing subscriptions, and internal errors all resolve to "allow".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from nexus.core.tenancy import TenantSession
from nexus.models.billing import (
    BillingCapability,
    BillingPlan,
    BillingPlanEntitlement,
    BillingSubscription,
)

logger = logging.getLogger("nexus.billing.entitlements")

# Plan classes that are never limited: they exist to observe cost, not to gate features.
_UNLIMITED_CLASSES = {"unlimited", "internal", "partner"}
# Subscription states in which entitlements still apply normally.
_LIVE_STATUSES = {"trialing", "active", "past_due"}


@dataclass(slots=True)
class ResolvedEntitlement:
    """The effective policy for one tenant x capability, plus where it came from (for admin
    debugging and the 402 payload)."""

    capability_id: str
    mode: str                       # shadow|enabled|metered|unlimited|disabled|enterprise
    quota: int | None = None
    soft_limit_pct: int = 80
    hard_limit: int | None = None
    overage_price_credits: int | None = None
    cooldown_s: int | None = None
    burst_limit: int | None = None
    reset_policy: str = "monthly_anniversary"
    depends_on: tuple[str, ...] = ()
    unit: str = "action"
    plan_id: str | None = None
    source: str = "catalog"         # plan_class | plan | catalog | unknown


async def _active_subscription(ts: TenantSession) -> BillingSubscription | None:
    rows = await ts.list(BillingSubscription, limit=5)
    live = [s for s in rows if s.status in _LIVE_STATUSES]
    return live[0] if live else (rows[0] if rows else None)


async def resolve_entitlement(ts: TenantSession, capability_id: str) -> ResolvedEntitlement:
    """Compute the effective entitlement. Never raises."""
    try:
        cap = await ts.session.get(BillingCapability, capability_id)
        if cap is None:
            # Unregistered capability: allow and record. This is what makes shipping the engine
            # incapable of breaking a feature nobody remembered to catalog.
            return ResolvedEntitlement(capability_id, mode="shadow", source="unknown")

        base = ResolvedEntitlement(
            capability_id=capability_id,
            mode=cap.default_mode,
            unit=cap.unit,
            depends_on=tuple(cap.depends_on or ()),
            source="catalog",
        )

        sub = await _active_subscription(ts)
        if sub is None:
            return base
        base.plan_id = sub.plan_id

        plan = await ts.session.get(BillingPlan, sub.plan_id)
        if plan is not None and plan.plan_class in _UNLIMITED_CLASSES:
            base.mode = "unlimited"
            base.quota = None
            base.source = "plan_class"
            return base

        ent = (
            await ts.session.scalars(
                select(BillingPlanEntitlement).where(
                    BillingPlanEntitlement.plan_id == sub.plan_id,
                    BillingPlanEntitlement.capability_id == capability_id,
                )
            )
        ).first()
        if ent is None:
            return base

        base.mode = ent.mode
        base.quota = ent.quota
        base.soft_limit_pct = ent.soft_limit_pct
        base.hard_limit = ent.hard_limit
        base.overage_price_credits = ent.overage_price_credits
        base.cooldown_s = ent.cooldown_s
        base.burst_limit = ent.burst_limit
        base.reset_policy = ent.reset_policy
        base.source = "plan"
        return base
    except Exception:  # resolution failure must degrade to allow, never to a 500
        logger.warning("entitlement resolution failed for %s", capability_id, exc_info=True)
        return ResolvedEntitlement(capability_id, mode="shadow", source="unknown")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_billing_entitlements.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add nexus/billing/errors.py nexus/billing/entitlements.py tests/test_billing_entitlements.py
git commit -m "feat(billing): entitlement resolution engine"
```

---

## Task 4: check_and_meter() — the decision + metering seam

**Files:** Modify `nexus/billing/entitlements.py`; Test: append to `tests/test_billing_entitlements.py`

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_billing_entitlements.py  (append)
async def test_check_and_meter_allows_and_records_in_shadow_mode():
    from nexus.billing.entitlements import check_and_meter
    from nexus.models.billing import BillingUsageEvent

    tid = await _seed("free")
    async with tenant_session(tid) as ts:
        # Free plan quota for ai.email_draft is 20; ask for 1 -> allowed and recorded.
        res = await check_and_meter(ts, capability_id="ai.email_draft", quantity=1)
        assert res.allowed is True
        assert res.recorded is True
        assert len(await ts.list(BillingUsageEvent)) == 1


async def test_shadow_mode_never_blocks_even_over_quota(monkeypatch):
    """The whole safety story: over quota in shadow mode still runs."""
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "shadow")
    tid = await _seed("free")
    async with tenant_session(tid) as ts:
        res = await check_and_meter(ts, capability_id="ai.email_draft", quantity=999)
        assert res.allowed is True
        assert res.would_block is True      # observable for dashboards, not enforced


async def test_enforcement_on_blocks_over_quota(monkeypatch):
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    tid = await _seed("free")
    async with tenant_session(tid) as ts:
        res = await check_and_meter(ts, capability_id="ai.email_draft", quantity=999)
        assert res.allowed is False
        assert res.reason == "quota_exhausted"


async def test_enforcement_on_blocks_disabled_module(monkeypatch):
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    tid = await _seed("free")            # Free disables module.network
    async with tenant_session(tid) as ts:
        res = await check_and_meter(ts, capability_id="module.network", quantity=1)
        assert res.allowed is False and res.reason == "disabled"


async def test_enforcement_off_is_a_passthrough(monkeypatch):
    """The incident kill switch: no evaluation, no recording, no blocking."""
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings
    from nexus.models.billing import BillingUsageEvent

    monkeypatch.setattr(get_settings(), "billing_enforcement", "off")
    tid = await _seed("free")
    async with tenant_session(tid) as ts:
        res = await check_and_meter(ts, capability_id="ai.email_draft", quantity=999)
        assert res.allowed is True and res.recorded is False
        assert await ts.list(BillingUsageEvent) == []


async def test_unlimited_plan_is_recorded_but_never_blocked(monkeypatch):
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings
    from nexus.models.billing import BillingUsageEvent

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    tid = await _seed("legacy-unlimited")
    async with tenant_session(tid) as ts:
        res = await check_and_meter(ts, capability_id="ai.email_draft", quantity=10_000)
        assert res.allowed is True                       # never blocked
        assert len(await ts.list(BillingUsageEvent)) == 1  # but COGS is visible
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_billing_entitlements.py -k check_and_meter -v`
Expected: FAIL — `ImportError: cannot import name 'check_and_meter'`

- [ ] **Step 3: Append the implementation to `nexus/billing/entitlements.py`**

```python
# ---- the seam ------------------------------------------------------------------------------
@dataclass(slots=True)
class MeterResult:
    """Outcome of one seam call. Callers only ever need ``allowed``."""

    allowed: bool
    recorded: bool = False
    reason: str | None = None        # quota_exhausted | disabled | dependency | throttled
    would_block: bool = False        # True when shadow mode suppressed a real block
    used: float = 0
    quota: int | None = None
    entitlement: "ResolvedEntitlement | None" = None

    def raise_if_blocked(self) -> None:
        """Convenience for routers: turn a block into the typed HTTP-mappable exception."""
        if self.allowed:
            return
        from nexus.billing.errors import QuotaExceeded

        ent = self.entitlement
        raise QuotaExceeded(
            ent.capability_id if ent else "unknown",
            reason=self.reason or "quota_exhausted",
            used=self.used,
            quota=self.quota,
            plan_id=ent.plan_id if ent else None,
        )


async def current_usage(ts: TenantSession, capability_id: str) -> float:
    """Usage of this capability in the current calendar month (authoritative counter path)."""
    from sqlalchemy import func

    from nexus.core.db import utcnow
    from nexus.models.billing import BillingUsageEvent

    start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = await ts.session.scalar(
        select(func.coalesce(func.sum(BillingUsageEvent.quantity), 0)).where(
            BillingUsageEvent.tenant_id == ts.tenant_id,
            BillingUsageEvent.capability_id == capability_id,
            BillingUsageEvent.occurred_at >= start,
        )
    )
    return float(total or 0)


async def check_and_meter(
    ts: TenantSession,
    *,
    capability_id: str,
    quantity: float = 1,
    user_id: str | None = None,
    source: str = "api",
    idempotency_key: str | None = None,
    attrs: dict | None = None,
) -> MeterResult:
    """THE billing seam. Resolve entitlement, decide, record usage.

    Application code calls exactly this and never mentions a plan. Behavior is governed by
    ``NEXUS_BILLING_ENFORCEMENT``:
      off    -> pure passthrough (no evaluation, no recording)
      shadow -> evaluate + record, but ALWAYS allow (``would_block`` reports what would happen)
      on     -> evaluate + record + enforce

    Never raises: any internal failure degrades to allow (docs/billing/01 §6).
    """
    from nexus.billing.usage import record_usage
    from nexus.core.config import get_settings

    mode = get_settings().billing_enforcement
    if mode == "off":
        return MeterResult(allowed=True, recorded=False)

    try:
        ent = await resolve_entitlement(ts, capability_id)

        blocked_reason: str | None = None
        used = 0.0
        if ent.mode == "disabled":
            blocked_reason = "disabled"
        elif ent.mode in ("shadow", "enabled", "unlimited"):
            blocked_reason = None            # never quota-limited
        elif ent.quota is not None:
            used = await current_usage(ts, capability_id)
            limit = ent.hard_limit if ent.hard_limit is not None else ent.quota
            # Overage pricing means "keep going and charge for it", not "stop".
            if used + quantity > limit and ent.overage_price_credits is None:
                blocked_reason = "quota_exhausted"

        enforced = mode == "on"
        allowed = True if not enforced else blocked_reason is None

        recorded = False
        if allowed:
            recorded = await record_usage(
                ts, capability_id=capability_id, quantity=quantity, unit=ent.unit,
                user_id=user_id, source=source, idempotency_key=idempotency_key, attrs=attrs,
            )
        return MeterResult(
            allowed=allowed,
            recorded=recorded,
            reason=blocked_reason if not allowed else None,
            would_block=blocked_reason is not None,
            used=used,
            quota=ent.quota,
            entitlement=ent,
        )
    except Exception:  # the seam must never break the product
        logger.warning("check_and_meter failed for %s", capability_id, exc_info=True)
        return MeterResult(allowed=True, recorded=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_billing_entitlements.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/entitlements.py tests/test_billing_entitlements.py
git commit -m "feat(billing): check_and_meter seam with shadow/enforce modes"
```

---

## Task 5: Migration 0022

**Files:** Create `migrations/versions/0022_billing_usage.py`; Test: `tests/test_billing_migration.py` (append)

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_billing_migration.py  (append)
def test_usage_migration_creates_tables():
    import importlib
    import inspect

    import nexus.models  # noqa: F401
    from nexus.core.db import Base

    mod = importlib.import_module("migrations.versions.0022_billing_usage")
    assert mod.revision == "0022_billing_usage"
    assert mod.down_revision == "0021_billing_foundation"
    src = inspect.getsource(mod.upgrade)
    for table in ("billing_usage_events", "billing_usage_rollups"):
        assert table in Base.metadata.tables
        assert f'"{table}"' in src or f"'{table}'" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_billing_migration.py -k usage -v`
Expected: FAIL — module not found

- [ ] **Step 3: Write the migration**

```python
# migrations/versions/0022_billing_usage.py
"""Billing usage events + rollups.

Additive only. ``billing_usage_events`` is the highest-volume table in the platform; it ships
with the composite indexes the quota path and dashboards need from day one.

Revision ID: 0022_billing_usage
Revises: 0021_billing_foundation
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_billing_usage"
down_revision = "0021_billing_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_usage_events",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("capability_id", sa.String(length=80), nullable=False, index=True),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(length=20), nullable=False, server_default="action"),
        sa.Column("user_id", sa.String(length=32), nullable=True, index=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="api"),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("attrs", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("unit_cost_usd", sa.Numeric(12, 8), nullable=True),
        sa.Column("billed_credits", sa.Numeric(12, 4), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_usage_idempotency"),
    )
    op.create_index(
        "ix_usage_tenant_cap_time", "billing_usage_events",
        ["tenant_id", "capability_id", "occurred_at"],
    )
    op.create_index("ix_usage_occurred", "billing_usage_events", ["occurred_at"])

    op.create_table(
        "billing_usage_rollups",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("capability_id", sa.String(length=80), nullable=False, index=True),
        sa.Column("period_kind", sa.String(length=10), nullable=False),
        sa.Column("period_key", sa.String(length=40), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 6), nullable=False, server_default="0"),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(14, 8), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id", "capability_id", "period_kind", "period_key",
            name="uq_usage_rollup_period",
        ),
    )
    op.create_index(
        "ix_usage_rollup_lookup", "billing_usage_rollups",
        ["tenant_id", "capability_id", "period_kind"],
    )


def downgrade() -> None:
    op.drop_table("billing_usage_rollups")
    op.drop_table("billing_usage_events")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_billing_migration.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/0022_billing_usage.py tests/test_billing_migration.py
git commit -m "feat(billing): migration 0022 (usage events + rollups)"
```

---

## Task 6: Shadow-safety guard test

**Files:** Test: `tests/test_billing_shadow_safety.py`

- [ ] **Step 1: Write the guard test**

This is the CI gate that proves the platform can't regress the product.

```python
# tests/test_billing_shadow_safety.py
"""The non-negotiable safety contract of the billing platform.

If any of these fail, the platform is capable of breaking a customer feature and must not ship.
"""
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def test_unknown_capability_always_allowed_even_when_enforcing(monkeypatch):
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        res = await check_and_meter(ts, capability_id="feature.nobody.registered")
        assert res.allowed is True


async def test_tenant_without_subscription_is_never_blocked(monkeypatch):
    """Every pre-billing tenant has no subscription row. They must keep working."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.entitlements import check_and_meter
    from nexus.core.config import get_settings

    await sync_catalog()
    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        for cap in ("ai.email_draft", "network.search", "outreach.email_send",
                    "discovery.account_added", "verify.email"):
            assert (await check_and_meter(ts, capability_id=cap)).allowed is True, cap


async def test_engine_failure_degrades_to_allow(monkeypatch):
    """A broken entitlement engine must not take the product down."""
    import nexus.billing.entitlements as ent_mod
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_enforcement", "on")

    async def boom(*a, **k):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(ent_mod, "resolve_entitlement", boom)
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        res = await ent_mod.check_and_meter(ts, capability_id="ai.email_draft")
        assert res.allowed is True
        assert res.recorded is False


async def test_default_enforcement_setting_is_shadow():
    """Ship dark: the default must never enforce."""
    from nexus.core.config import Settings

    assert Settings().billing_enforcement == "shadow"
```

- [ ] **Step 2: Run the guard tests**

Run: `py -3.10 -m pytest tests/test_billing_shadow_safety.py -v`
Expected: PASS (4 tests)

- [ ] **Step 3: Commit**

```bash
git add tests/test_billing_shadow_safety.py
git commit -m "test(billing): shadow-safety contract (cannot break the product)"
```

---

## Task 7: Regression gate

**Files:** none (verification only)

- [ ] **Step 1: Billing suites**

Run: `py -3.10 -m pytest tests/ -k billing -q`
Expected: all pass.

- [ ] **Step 2: Full suite**

Run: `py -3.10 -m pytest -q --deselect "tests/test_incident_hardening.py::test_worker_loop_survives_queue_outage"`
Expected: pre-existing suite green (the `test_metrics_off_by_default_and_app_serves` failure is a known stale-`nexus/web/dist` artifact, not a regression).

- [ ] **Step 3: Lint**

Run: `py -3.10 -m ruff check nexus/billing nexus/models/billing.py`
Expected: `All checks passed!`

---

## Self-review

**Spec coverage:** usage-event schema + idempotency ([03](../../billing/03-Metering-Architecture.md) §1) → T1/T2;
resolution chain incl. unlimited classes ([02](../../billing/02-Entitlement-Engine.md) §2) → T3;
outcomes + enforcement modes + kill switch ([02](../../billing/02-Entitlement-Engine.md) §3,
[15](../../billing/15-Migration-Strategy.md) §1) → T4; migration → T5; the
"cannot break the product" invariants ([15](../../billing/15-Migration-Strategy.md) §1) → T6.
Deferred by design: Valkey hot counters + rollup job (M3), rating/credits (M4), lifecycle/PSP
(M4), admin writes (M5), application integration at call sites (M5).

**Placeholder scan:** none — every step ships complete runnable code.

**Type consistency:** `ResolvedEntitlement` fields (mode/quota/unit/depends_on/plan_id/source)
defined in T3 and read in T4's `check_and_meter` + `MeterResult`. `record_usage(...)` signature
from T2 matches the call in T4. `QuotaExceeded(capability_id, reason=, used=, quota=, plan_id=)`
from T3 matches `MeterResult.raise_if_blocked()` in T4. Migration revision ids chain
0021 → 0022.
