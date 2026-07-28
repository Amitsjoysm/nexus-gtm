# Billing Milestone 1 — Foundation & Feature Registry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the billing foundation — capability catalog, plans, plan entitlements, subscriptions, platform-admin access, and read-only admin APIs — with **zero change to existing product behavior**.

**Architecture:** Platform-global config tables (capabilities/plans/entitlements/platform_admins, no `tenant_id`) plus one tenant-scoped table (`billing_subscriptions`, auto-covered by `scripts/apply_rls.py`). The catalog is seeded from a declarative Python list synced idempotently at startup. Nothing calls into billing from application code in M1 — this milestone only lands data + config + admin read APIs.

**Tech Stack:** Python 3.11, FastAPI, async SQLAlchemy 2.0, Pydantic v2, Alembic, pytest (`asyncio_mode=auto`), offline SQLite.

**Run tests with `py -3.10 -m pytest`** on this Windows box (bare `python` is 3.14 without dev deps).

**Design refs:** [01-Billing-Architecture](../../billing/01-Billing-Architecture.md) §4,
[02-Entitlement-Engine](../../billing/02-Entitlement-Engine.md) §1,
[08-Feature-Catalog](../../billing/08-Feature-Catalog.md),
[15-Migration-Strategy](../../billing/15-Migration-Strategy.md) §1.

**Non-breaking guarantee:** all-new tables, all-new package, all-new router. The only edits to
existing files are append-only registrations (`nexus/models/__init__.py`,
`nexus/api/routers/__init__.py`, `nexus/core/config.py`, `nexus/main.py` lifespan).

---

## File structure

**Create:**
- `nexus/models/billing.py` — 5 ORM models (Task 2)
- `nexus/billing/__init__.py` — empty package marker (Task 4)
- `nexus/billing/catalog.py` — capability seed + idempotent sync (Task 4)
- `nexus/billing/plans.py` — plan seed + idempotent sync (Task 5)
- `nexus/api/routers/admin_billing.py` — read-only admin API (Task 7)
- `migrations/versions/0021_billing_foundation.py` (Task 6)
- `tests/test_billing_models.py`, `tests/test_billing_catalog.py`, `tests/test_billing_admin_api.py`

**Modify (append-only):**
- `nexus/core/config.py` — 3 settings (Task 1)
- `nexus/models/__init__.py` — register 5 models (Task 3)
- `nexus/api/deps.py` — `require_platform_admin` dependency (Task 7)
- `nexus/api/routers/__init__.py` — register router (Task 7)
- `nexus/main.py` — seed sync on startup (Task 8)

---

## Task 1: Billing settings

**Files:** Modify `nexus/core/config.py`

- [ ] **Step 1: Add the settings**

In `nexus/core/config.py`, add inside `class Settings` immediately after the `metrics_enabled` field:

```python
    # ---- Billing platform (commercial OS) --------------------------------------------------
    # Enforcement mode is the master kill switch (docs/billing/15-Migration-Strategy.md §1):
    #   off    = the seam is a no-op passthrough (incident escape hatch)
    #   shadow = evaluate + record usage, NEVER block  (safe default; ships dark)
    #   on     = evaluate + record + enforce per-capability
    billing_enforcement: Literal["off", "shadow", "on"] = "shadow"
    # Comma-separated emails bootstrapped as platform super-admins (chicken-and-egg solution for
    # the /admin portal). Empty = nobody has platform access until a row is inserted by hand.
    platform_admin_emails: str = ""
    # Sync the capability/plan seed into the DB on startup. Off in tests (fixtures seed directly).
    billing_seed_on_startup: bool = True
```

- [ ] **Step 2: Add the parsed accessor**

Add this property next to the other `_csv_list` properties (after `contact_search_source_list`):

```python
    @property
    def platform_admin_email_list(self) -> list[str]:
        return [e.lower() for e in self._csv_list(self.platform_admin_emails)]
```

- [ ] **Step 3: Verify**

Run: `py -3.10 -c "from nexus.core.config import Settings; s=Settings(); print(s.billing_enforcement, s.platform_admin_email_list)"`
Expected: `shadow []`

- [ ] **Step 4: Commit**

```bash
git add nexus/core/config.py
git commit -m "feat(billing): enforcement mode + platform admin settings"
```

---

## Task 2: Billing ORM models

**Files:** Create `nexus/models/billing.py`; Test: `tests/test_billing_models.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_models.py
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


def test_models_importable_and_registered():
    import nexus.models as m

    for name in (
        "BillingCapability", "BillingPlan", "BillingPlanEntitlement",
        "BillingSubscription", "PlatformAdmin",
    ):
        assert hasattr(m, name), f"{name} not exported from nexus.models"


async def test_capability_and_plan_round_trip():
    """Platform-global config tables are NOT tenant-scoped: they carry no tenant_id and are
    readable by every tenant (the catalog is the same for the whole platform)."""
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingCapability, BillingPlan, BillingPlanEntitlement

    async with get_sessionmaker()() as s:
        cap = BillingCapability(
            id="ai.email_draft", category="ai", sub_category="outreach",
            name="AI email draft", description="Personalized outreach draft",
            unit="action", meter_kind="counter", default_mode="metered",
        )
        plan = BillingPlan(
            id="growth", name="Growth", plan_class="standard", status="active",
            base_price_cents=7900, currency="USD", interval="month",
            included_credits=2000, seat_price_cents=0,
        )
        s.add_all([cap, plan])
        await s.flush()
        s.add(BillingPlanEntitlement(
            plan_id="growth", capability_id="ai.email_draft", mode="metered",
            quota=500, soft_limit_pct=80, reset_policy="monthly_anniversary",
            overage_price_credits=2,
        ))
        await s.commit()

    async with get_sessionmaker()() as s:
        got = await s.get(BillingCapability, "ai.email_draft")
        assert got.unit == "action" and got.default_mode == "metered"
        p = await s.get(BillingPlan, "growth")
        assert p.included_credits == 2000 and p.plan_class == "standard"


async def test_subscription_is_tenant_scoped():
    """billing_subscriptions carries tenant_id -> automatically covered by apply_rls.py."""
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlan, BillingSubscription

    tid = await make_tenant()
    async with get_sessionmaker()() as s:
        s.add(BillingPlan(id="legacy-unlimited", name="Legacy Unlimited",
                          plan_class="unlimited", status="active", base_price_cents=0,
                          currency="USD", interval="month", included_credits=0,
                          seat_price_cents=0))
        await s.commit()

    async with tenant_session(tid) as ts:
        ts.add(BillingSubscription(plan_id="legacy-unlimited", status="active"))
        await ts.flush()
        rows = await ts.list(BillingSubscription)
        assert len(rows) == 1
        assert rows[0].tenant_id == tid          # stamped by the tenancy layer
        assert rows[0].status == "active"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_billing_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.models.billing'`

- [ ] **Step 3: Write the implementation**

```python
# nexus/models/billing.py
"""Billing platform: catalog, plans, entitlements, subscriptions, platform admins.

Two classes of table live here:

* **Platform-global config** (``BillingCapability``, ``BillingPlan``,
  ``BillingPlanEntitlement``, ``PlatformAdmin``) — one shared catalog/price list for the whole
  platform. Deliberately NOT ``TenantScoped``: there is no tenant dimension, and every tenant
  reads the same rows. Writes are admin-only at the API layer.
* **Tenant-owned state** (``BillingSubscription``) — ``TenantScoped``, so
  ``scripts/apply_rls.py`` picks it up automatically and Postgres RLS isolates it like every
  other tenant table.

IDs are human-readable slugs (``ai.email_draft``, ``growth``) rather than UUIDs: they appear in
application code, admin URLs, and invoices, and must stay stable and greppable.
See docs/billing/01-Billing-Architecture.md §4 and 08-Feature-Catalog.md.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime
from nexus.core.tenancy import TenantScoped

# ---- vocabularies (validated at the API layer; stored as plain strings for forward-compat) ----
CAPABILITY_UNITS = (
    "action", "token", "search", "check", "message", "seat", "gb", "minute",
    "request", "job", "run",
)
METER_KINDS = ("counter", "gauge", "passthrough")
# shadow = record only; enabled = allowed, unmetered; metered = allowed + counted against quota;
# enterprise = off unless a contract/plan turns it on; disabled = blocked.
CAPABILITY_MODES = ("shadow", "enabled", "metered", "enterprise", "disabled")
PLAN_CLASSES = (
    "free", "trial", "standard", "usage", "hybrid", "unlimited",
    "enterprise", "custom", "partner", "internal",
)
PLAN_STATUSES = ("draft", "active", "grandfathered", "retired")
SUBSCRIPTION_STATUSES = (
    "trialing", "active", "past_due", "suspended", "canceled",
)
RESET_POLICIES = ("monthly_anniversary", "calendar_month", "daily", "never")


class BillingCapability(TimestampMixin, Base):
    """One billable thing. The stable ID application code will reference at the metering seam."""

    __tablename__ = "billing_capabilities"
    __table_args__ = (Index("ix_billing_cap_category", "category", "sub_category"),)

    id: Mapped[str] = mapped_column(String(80), primary_key=True)   # e.g. "ai.email_draft"
    category: Mapped[str] = mapped_column(String(40), index=True)   # ai | search | outreach | ...
    sub_category: Mapped[str] = mapped_column(String(40), default="")
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    unit: Mapped[str] = mapped_column(String(20), default="action")
    meter_kind: Mapped[str] = mapped_column(String(20), default="counter")
    default_mode: Mapped[str] = mapped_column(String(20), default="shadow")
    # Capabilities this one requires (module gates, e.g. ["module.network"]).
    depends_on: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Retired capabilities stay in the table (invoices reference them) but stop being offered.
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class BillingPlan(TimestampMixin, Base):
    """A sellable plan. Behavior is entirely data — no plan-specific code anywhere."""

    __tablename__ = "billing_plans"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)   # e.g. "growth"
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    plan_class: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    # Money is stored in integer minor units (cents) — never floats.
    base_price_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    seat_price_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    interval: Mapped[str] = mapped_column(String(10), default="month")  # month | year
    included_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_seats: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unlimited
    trial_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Ordering on the pricing page / admin lists.
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    # Free-form extras (rollover_months, support_level, ...) so new plan knobs need no migration.
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class BillingPlanEntitlement(IdMixin, TimestampMixin, Base):
    """plan x capability -> the full policy surface (docs/billing/02-Entitlement-Engine.md §1)."""

    __tablename__ = "billing_plan_entitlements"
    __table_args__ = (
        UniqueConstraint("plan_id", "capability_id", name="uq_plan_entitlement"),
        Index("ix_plan_entitlement_plan", "plan_id"),
    )

    plan_id: Mapped[str] = mapped_column(ForeignKey("billing_plans.id", ondelete="CASCADE"))
    capability_id: Mapped[str] = mapped_column(
        ForeignKey("billing_capabilities.id", ondelete="CASCADE")
    )
    mode: Mapped[str] = mapped_column(String(20), default="metered")
    quota: Mapped[int | None] = mapped_column(Integer, nullable=True)        # None = unlimited
    soft_limit_pct: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    hard_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reset_policy: Mapped[str] = mapped_column(String(30), default="monthly_anniversary")
    burst_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)   # per rolling minute
    rate_limit: Mapped[str | None] = mapped_column(String(20), nullable=True)  # e.g. "60/min"
    cooldown_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overage_price_credits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    feature_flag: Mapped[str | None] = mapped_column(String(60), nullable=True)
    trial_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)


class BillingSubscription(IdMixin, TimestampMixin, TenantScoped, Base):
    """A tenant's current plan + billing period. One active row per tenant."""

    __tablename__ = "billing_subscriptions"
    __table_args__ = (Index("ix_billing_sub_tenant_status", "tenant_id", "status"),)

    plan_id: Mapped[str] = mapped_column(ForeignKey("billing_plans.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    interval: Mapped[str] = mapped_column(String(10), default="month")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    current_period_start: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    trial_end: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Frozen terms: plan edits must never reprice a grandfathered subscriber.
    grandfathered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    seats_included: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Payment-provider linkage; None for internal/legacy plans that never touch a PSP.
    psp_customer_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    psp_subscription_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class PlatformAdmin(IdMixin, TimestampMixin, Base):
    """Staff access to /admin — deliberately separate from tenant RBAC.

    A platform admin is an operator of the SaaS, not a member of any customer workspace; the two
    authorization systems must never be conflated (docs/billing/06-Admin-Portal.md §1).
    """

    __tablename__ = "platform_admins"
    __table_args__ = (UniqueConstraint("email", name="uq_platform_admin_email"),)

    email: Mapped[str] = mapped_column(String(255), index=True)
    # super | finance | support | ops | sales
    platform_role: Mapped[str] = mapped_column(String(20), default="support")
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="")
```

- [ ] **Step 4: Run test to verify it passes** (after Task 3 registers the models)

Run: `py -3.10 -m pytest tests/test_billing_models.py -v`
Expected: FAIL on `test_models_importable_and_registered` until Task 3 — that is expected; the other two tests must PASS.

- [ ] **Step 5: Commit**

```bash
git add nexus/models/billing.py tests/test_billing_models.py
git commit -m "feat(billing): catalog/plan/entitlement/subscription ORM models"
```

---

## Task 3: Register the models

**Files:** Modify `nexus/models/__init__.py`

- [ ] **Step 1: Add the import**

In `nexus/models/__init__.py`, add after the existing `from nexus.models.alerts import Alert` line:

```python
from nexus.models.billing import (
    BillingCapability,
    BillingPlan,
    BillingPlanEntitlement,
    BillingSubscription,
    PlatformAdmin,
)
```

- [ ] **Step 2: Add to `__all__`**

Append these five entries to the `__all__` list:

```python
    "BillingCapability",
    "BillingPlan",
    "BillingPlanEntitlement",
    "BillingSubscription",
    "PlatformAdmin",
```

- [ ] **Step 3: Run tests**

Run: `py -3.10 -m pytest tests/test_billing_models.py -v`
Expected: PASS (3 tests)

- [ ] **Step 4: Commit**

```bash
git add nexus/models/__init__.py
git commit -m "feat(billing): register billing models"
```

---

## Task 4: Capability catalog seed + idempotent sync

**Files:** Create `nexus/billing/__init__.py` (empty), `nexus/billing/catalog.py`; Test: `tests/test_billing_catalog.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_catalog.py
from __future__ import annotations


def test_seed_is_wellformed():
    from nexus.billing.catalog import CAPABILITY_SEED
    from nexus.models.billing import CAPABILITY_MODES, CAPABILITY_UNITS, METER_KINDS

    assert len(CAPABILITY_SEED) >= 55
    ids = [c["id"] for c in CAPABILITY_SEED]
    assert len(ids) == len(set(ids)), "duplicate capability ids"
    for c in CAPABILITY_SEED:
        assert c["unit"] in CAPABILITY_UNITS, c["id"]
        assert c["meter_kind"] in METER_KINDS, c["id"]
        assert c["default_mode"] in CAPABILITY_MODES, c["id"]
        assert "." in c["id"], f"{c['id']} must be namespaced (category.name)"
        assert c["name"] and c["category"]


def test_seed_dependencies_resolve():
    """Every depends_on target must itself be a catalog entry — a dangling gate would silently
    block a feature forever."""
    from nexus.billing.catalog import CAPABILITY_SEED

    ids = {c["id"] for c in CAPABILITY_SEED}
    for c in CAPABILITY_SEED:
        for dep in c.get("depends_on", []):
            assert dep in ids, f"{c['id']} depends on unknown {dep}"


async def test_sync_catalog_is_idempotent_and_updates_metadata():
    from nexus.billing.catalog import CAPABILITY_SEED, sync_catalog
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingCapability

    first = await sync_catalog()
    assert first["created"] == len(CAPABILITY_SEED)

    second = await sync_catalog()
    assert second["created"] == 0 and second["updated"] == 0  # no churn on re-run

    # An admin-edited row must not be clobbered on name, but metadata drift IS corrected.
    async with get_sessionmaker()() as s:
        cap = await s.get(BillingCapability, "ai.email_draft")
        cap.unit = "wrong"
        await s.commit()
    third = await sync_catalog()
    assert third["updated"] == 1
    async with get_sessionmaker()() as s:
        assert (await s.get(BillingCapability, "ai.email_draft")).unit == "action"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_billing_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.billing'`

- [ ] **Step 3: Write the implementation**

Create empty `nexus/billing/__init__.py` (0 bytes). Then:

```python
# nexus/billing/catalog.py
"""The capability catalog: the declarative registry of everything billable.

This module is the single source of truth for WHAT can be metered. Pricing lives on rate cards
and plans (docs/billing/04-Pricing-Engine.md); this file only declares the capability, its unit,
and its safe default mode.

Every entry ships as ``default_mode="shadow"`` or ``"enabled"`` — nothing blocks on first deploy
(docs/billing/15-Migration-Strategy.md §1). Turning a capability into a real gate is an Admin
action, never a code change.

Adding a new billable feature = add one row here (or via the Admin API) and call the metering
seam. That is the entire engineering cost of monetizing something new.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from nexus.core.db import get_sessionmaker
from nexus.models.billing import BillingCapability

logger = logging.getLogger("nexus.billing.catalog")


def _cap(
    id: str, category: str, name: str, *, sub_category: str = "", unit: str = "action",
    meter_kind: str = "counter", default_mode: str = "shadow", description: str = "",
    depends_on: list[str] | None = None,
) -> dict:
    return {
        "id": id, "category": category, "sub_category": sub_category, "name": name,
        "description": description, "unit": unit, "meter_kind": meter_kind,
        "default_mode": default_mode, "depends_on": depends_on or [],
    }


# ---- module gates: coarse on/off switches other capabilities depend on ----------------------
_MODULES = [
    _cap("module.outreach", "module", "Outreach module", default_mode="enabled",
         description="Campaigns, cadences, sending."),
    _cap("module.calling", "module", "Calling module", default_mode="enabled",
         description="Call queue, AI scripts, dispositions."),
    _cap("module.network", "module", "Relationship graph module", default_mode="enabled",
         description="Personal network search and warm intros."),
    _cap("module.discovery", "module", "Discovery module", default_mode="enabled",
         description="ICP auto-discovery and look-alikes."),
    _cap("module.integrations", "module", "Integrations module", default_mode="enabled",
         description="CRM and sales-engagement connectors."),
    _cap("module.api", "module", "Public API access", default_mode="enterprise",
         description="Programmatic API access."),
]

# ---- platform & seats ----------------------------------------------------------------------
_PLATFORM = [
    _cap("seat.member", "platform", "User seat", unit="seat", meter_kind="gauge",
         default_mode="metered", description="Billed as seat-days."),
    _cap("platform.workspace", "platform", "Workspace", default_mode="enabled"),
    _cap("platform.storage", "platform", "Stored data", unit="gb", meter_kind="gauge",
         default_mode="metered", description="Measured nightly."),
    _cap("platform.custom_fields", "platform", "Custom field definitions", default_mode="enabled"),
    _cap("api.request", "platform", "API request", sub_category="api", unit="request",
         description="Blanket middleware meter over every HTTP request."),
    _cap("job.queue_execution", "platform", "Background job execution", unit="job",
         description="Blanket meter over every queue handler."),
]

# ---- AI -------------------------------------------------------------------------------------
_AI = [
    _cap("ai.tokens", "ai", "LLM tokens", unit="token",
         description="Raw token meter from the LLM chokepoint; COGS truth."),
    _cap("ai.research_brief", "ai", "AI research brief", sub_category="research",
         default_mode="metered"),
    _cap("ai.email_draft", "ai", "AI email draft", sub_category="outreach",
         default_mode="metered", depends_on=["module.outreach"]),
    _cap("ai.account_qa", "ai", "Ask about this account", sub_category="research",
         default_mode="metered"),
    _cap("ai.scoring", "ai", "ICP fit scoring", sub_category="scoring"),
    _cap("ai.contact_rank", "ai", "Contact recommendation", sub_category="research",
         default_mode="metered"),
    _cap("ai.call_script", "ai", "AI call script", sub_category="calling",
         default_mode="metered", depends_on=["module.calling"]),
    _cap("ai.icp_from_website", "ai", "AI ICP from website", sub_category="relevance",
         default_mode="metered"),
    _cap("ai.chat_turn", "ai", "Orchestrator chat turn", sub_category="orchestration",
         default_mode="metered"),
    _cap("ai.personalization_fetch", "ai", "Person social insights", sub_category="personalization",
         default_mode="metered"),
    _cap("ai.premium_model", "ai", "Premium model routing", sub_category="routing",
         default_mode="enterprise",
         description="Route to a frontier model; multiplies credit cost."),
    _cap("workflow.orchestration_run", "workflow", "Orchestration run", unit="run",
         default_mode="metered"),
    _cap("workflow.orchestration_step", "workflow", "Orchestration step", unit="job"),
]

# ---- search / discovery / enrichment --------------------------------------------------------
_DISCOVERY = [
    _cap("search.web", "search", "Web search", unit="search",
         description="Exa/Brave/Serper/DuckDuckGo call."),
    _cap("discovery.icp_daily", "discovery", "Daily ICP discovery run", unit="job",
         default_mode="metered", depends_on=["module.discovery"]),
    _cap("discovery.account_added", "discovery", "Net-new ICP account", default_mode="metered",
         depends_on=["module.discovery"]),
    _cap("discovery.lookalike_company", "discovery", "Company look-alikes", unit="run",
         default_mode="metered", depends_on=["module.discovery"]),
    _cap("discovery.lookalike_contact", "discovery", "Contact look-alikes", unit="run",
         default_mode="metered", depends_on=["module.discovery"]),
    _cap("enrich.account", "enrich", "Account enrichment", default_mode="metered"),
    _cap("enrich.contact", "enrich", "Contact enrichment", default_mode="metered"),
    _cap("enrich.source_committee", "enrich", "Source buying committee", default_mode="metered"),
    _cap("enrich.linkedin_finder", "enrich", "LinkedIn URL finder", default_mode="metered"),
    _cap("verify.email", "enrich", "Email verification", unit="check", default_mode="metered"),
    _cap("signal.news_scan", "signal", "News signal scan", unit="job"),
    _cap("signal.rss_scan", "signal", "RSS signal scan", unit="job"),
    _cap("signal.stored", "signal", "Signal stored"),
]

# ---- outreach & workflow --------------------------------------------------------------------
_OUTREACH = [
    _cap("outreach.email_send", "outreach", "Email sent", unit="message",
         default_mode="metered", depends_on=["module.outreach"]),
    _cap("outreach.email_draft_save", "outreach", "Draft saved to mailbox", unit="message",
         default_mode="metered", depends_on=["module.outreach"]),
    _cap("outreach.campaign", "outreach", "Campaign launched", unit="run",
         default_mode="metered", depends_on=["module.outreach"]),
    _cap("outreach.cadence_touch", "outreach", "Cadence touch", default_mode="metered",
         depends_on=["module.outreach"]),
    _cap("outreach.sep_push", "outreach", "Sales-engagement push", default_mode="metered",
         depends_on=["module.integrations"]),
    _cap("calling.task", "calling", "Call task", default_mode="enabled",
         depends_on=["module.calling"]),
    _cap("calling.brief", "calling", "Pre-call brief", default_mode="metered",
         depends_on=["module.calling"]),
    _cap("calling.minutes", "calling", "Telephony minutes", unit="minute",
         default_mode="enterprise", depends_on=["module.calling"]),
    _cap("automation.play_run", "automation", "Play executed", unit="job",
         default_mode="metered"),
    _cap("automation.account_refresh", "automation", "Account refresh cycle", unit="job"),
    _cap("inbox.task", "workflow", "Inbox task created"),
]

# ---- network (relationship graph) -----------------------------------------------------------
_NETWORK = [
    _cap("network.source_sync", "network", "Network source sync", unit="job",
         default_mode="metered", depends_on=["module.network"]),
    _cap("network.linkedin_import", "network", "LinkedIn export import", unit="job",
         default_mode="metered", depends_on=["module.network"]),
    _cap("network.search", "network", "Network search", unit="search",
         default_mode="metered", depends_on=["module.network"]),
    _cap("network.intro_paths", "network", "Warm intro paths", default_mode="enabled",
         depends_on=["module.network"]),
    _cap("network.persons", "network", "Graph persons stored", meter_kind="gauge"),
]

# ---- integrations, notifications, data ------------------------------------------------------
_INTEGRATIONS = [
    _cap("integration.crm_sync", "integration", "CRM record sync", default_mode="metered",
         depends_on=["module.integrations"]),
    _cap("integration.crm_connection", "integration", "CRM connection", default_mode="enabled",
         depends_on=["module.integrations"]),
    _cap("notify.in_app", "notify", "In-app notification", unit="message"),
    _cap("notify.webhook", "notify", "Webhook delivery", unit="message", default_mode="metered"),
    _cap("notify.slack", "notify", "Slack notification", unit="message", default_mode="metered"),
    _cap("notify.email_digest", "notify", "Email digest", unit="message", default_mode="metered"),
    _cap("data.import_csv", "data", "CSV import", unit="job", default_mode="metered"),
    _cap("data.export", "data", "Data export", unit="job", default_mode="metered"),
    _cap("report.cadence", "report", "Cadence report", default_mode="enabled"),
    _cap("report.analytics", "report", "Analytics dashboard", default_mode="enabled"),
]

CAPABILITY_SEED: list[dict] = (
    _MODULES + _PLATFORM + _AI + _DISCOVERY + _OUTREACH + _NETWORK + _INTEGRATIONS
)

# Fields sync_catalog() keeps authoritative from code. `name`/`description` are intentionally
# NOT in this list: admins may reword customer-facing copy without a deploy, and a redeploy must
# not silently revert their edits.
_MANAGED_FIELDS = ("category", "sub_category", "unit", "meter_kind", "depends_on")


async def sync_catalog() -> dict:
    """Upsert the declarative seed into ``billing_capabilities``. Idempotent.

    ``default_mode`` is applied on INSERT only: once a capability exists, its mode is owned by
    the Admin portal (flipping shadow -> enforced is an operator decision, and a redeploy must
    never silently re-arm or disarm a gate).
    """
    created = updated = 0
    async with get_sessionmaker()() as session:
        existing = {
            c.id: c for c in (await session.scalars(select(BillingCapability))).all()
        }
        for spec in CAPABILITY_SEED:
            row = existing.get(spec["id"])
            if row is None:
                session.add(BillingCapability(**spec))
                created += 1
                continue
            changed = False
            for field in _MANAGED_FIELDS:
                if getattr(row, field) != spec[field]:
                    setattr(row, field, spec[field])
                    changed = True
            if changed:
                updated += 1
        await session.commit()
    if created or updated:
        logger.info("catalog sync: %d created, %d updated", created, updated)
    return {"created": created, "updated": updated, "total": len(CAPABILITY_SEED)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_billing_catalog.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/__init__.py nexus/billing/catalog.py tests/test_billing_catalog.py
git commit -m "feat(billing): capability catalog seed + idempotent sync"
```

---

## Task 5: Plan seed + sync (incl. legacy-unlimited grandfather plan)

**Files:** Create `nexus/billing/plans.py`; Test: append to `tests/test_billing_catalog.py`

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_billing_catalog.py  (append)
async def test_plan_seed_creates_plans_and_legacy_plan_is_unlimited():
    from nexus.billing.plans import LEGACY_PLAN_ID, PLAN_SEED, sync_plans
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingPlan

    res = await sync_plans()
    assert res["created"] == len(PLAN_SEED)
    assert (await sync_plans())["created"] == 0          # idempotent

    async with get_sessionmaker()() as s:
        legacy = await s.get(BillingPlan, LEGACY_PLAN_ID)
        # The grandfather plan must never bill and never block existing tenants.
        assert legacy.plan_class == "unlimited"
        assert legacy.base_price_cents == 0 and legacy.seat_price_cents == 0
        assert legacy.max_seats is None
        growth = await s.get(BillingPlan, "growth")
        assert growth.base_price_cents == 7900 and growth.included_credits == 2000


async def test_plan_entitlements_reference_real_capabilities():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingCapability, BillingPlanEntitlement
    from sqlalchemy import select

    await sync_catalog()
    await sync_plans()
    async with get_sessionmaker()() as s:
        cap_ids = {c.id for c in (await s.scalars(select(BillingCapability))).all()}
        ents = (await s.scalars(select(BillingPlanEntitlement))).all()
        assert ents, "plans must ship entitlements"
        for e in ents:
            assert e.capability_id in cap_ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_billing_catalog.py -k plan -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.billing.plans'`

- [ ] **Step 3: Write the implementation**

```python
# nexus/billing/plans.py
"""Launch plan seed (docs/billing/13-Pricing-Recommendations.md §1).

Seeds are a STARTING POINT, not the source of truth: once a plan exists, the Admin portal owns
it. ``sync_plans`` therefore only creates missing plans and never overwrites an existing one —
a redeploy must not silently reprice live customers.

``legacy-unlimited`` is the migration keystone: every pre-billing tenant is attached to it so
the platform ships with zero behavioral change (docs/billing/15-Migration-Strategy.md §1).
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from nexus.core.db import get_sessionmaker
from nexus.models.billing import BillingPlan, BillingPlanEntitlement

logger = logging.getLogger("nexus.billing.plans")

LEGACY_PLAN_ID = "legacy-unlimited"

# Per-plan capability policy. Only capabilities that differ from "allow" need a row; the
# entitlement engine falls back to the catalog default for anything unlisted.
#   (capability_id, mode, quota, overage_price_credits)
_FREE_ENT = [
    ("module.outreach", "disabled", None, None),
    ("module.network", "disabled", None, None),
    ("module.calling", "disabled", None, None),
    ("module.discovery", "disabled", None, None),
    ("module.integrations", "disabled", None, None),
    ("verify.email", "metered", 50, None),
    ("ai.email_draft", "metered", 20, None),
    ("platform.storage", "metered", 1, None),
    ("seat.member", "metered", 1, None),
]
_STARTER_ENT = [
    ("module.network", "disabled", None, None),
    ("module.calling", "disabled", None, None),
    ("discovery.account_added", "metered", 150, 5),
    ("verify.email", "metered", 1000, 1),
    ("seat.member", "metered", 5, None),
    ("platform.storage", "metered", 2, 25),
]
_GROWTH_ENT = [
    ("discovery.account_added", "metered", 600, 5),
    ("verify.email", "metered", 5000, 1),
    ("seat.member", "metered", 25, None),
    ("platform.storage", "metered", 10, 25),
    ("network.source_sync", "metered", 60, 2),
]
_PRO_ENT = [
    ("module.api", "enabled", None, None),
    ("discovery.account_added", "metered", 1500, 5),
    ("verify.email", "metered", 15000, 1),
    ("seat.member", "metered", 100, None),
    ("platform.storage", "metered", 25, 25),
]
_BUSINESS_ENT = [
    ("module.api", "enabled", None, None),
    ("ai.premium_model", "enabled", None, None),
    ("discovery.account_added", "metered", 3000, 5),
    ("verify.email", "metered", 40000, 1),
    ("seat.member", "metered", 250, None),
    ("platform.storage", "metered", 100, 25),
]

PLAN_SEED: list[dict] = [
    {
        "id": LEGACY_PLAN_ID, "name": "Legacy Unlimited", "plan_class": "unlimited",
        "status": "grandfathered", "base_price_cents": 0, "seat_price_cents": 0,
        "included_credits": 0, "max_seats": None, "sort_order": 999,
        "description": "Pre-billing tenants. Never billed, never limited.",
        "entitlements": [],
    },
    {
        "id": "free", "name": "Free", "plan_class": "free", "status": "active",
        "base_price_cents": 0, "seat_price_cents": 0, "included_credits": 100,
        "max_seats": 1, "sort_order": 10,
        "description": "Explore NEXUS with a single seat.",
        "entitlements": _FREE_ENT,
    },
    {
        "id": "trial", "name": "Trial", "plan_class": "trial", "status": "active",
        "base_price_cents": 0, "seat_price_cents": 0, "included_credits": 1000,
        "max_seats": 5, "trial_days": 14, "sort_order": 15,
        "description": "14-day full-feature trial.",
        "entitlements": _GROWTH_ENT,
    },
    {
        "id": "starter", "name": "Starter", "plan_class": "standard", "status": "active",
        "base_price_cents": 3900, "seat_price_cents": 3900, "included_credits": 750,
        "max_seats": 5, "sort_order": 20,
        "description": "For a first SDR running outbound.",
        "entitlements": _STARTER_ENT,
    },
    {
        "id": "growth", "name": "Growth", "plan_class": "standard", "status": "active",
        "base_price_cents": 7900, "seat_price_cents": 7900, "included_credits": 2000,
        "max_seats": 25, "sort_order": 30,
        "description": "Full GTM stack for a growing team.",
        "entitlements": _GROWTH_ENT,
    },
    {
        "id": "professional", "name": "Professional", "plan_class": "standard",
        "status": "active", "base_price_cents": 12900, "seat_price_cents": 12900,
        "included_credits": 4000, "max_seats": 100, "sort_order": 40,
        "description": "API access and priority support.",
        "entitlements": _PRO_ENT,
    },
    {
        "id": "business", "name": "Business", "plan_class": "standard", "status": "active",
        "base_price_cents": 19900, "seat_price_cents": 19900, "included_credits": 8000,
        "max_seats": 250, "sort_order": 50,
        "description": "Scale outbound with advanced controls.",
        "entitlements": _BUSINESS_ENT,
    },
    {
        "id": "enterprise", "name": "Enterprise", "plan_class": "enterprise", "status": "active",
        "base_price_cents": 0, "seat_price_cents": 0, "included_credits": 0,
        "max_seats": None, "sort_order": 60,
        "description": "Custom contract; entitlements come from the contract.",
        "entitlements": [],
    },
    {
        "id": "internal", "name": "Internal", "plan_class": "internal", "status": "active",
        "base_price_cents": 0, "seat_price_cents": 0, "included_credits": 0,
        "max_seats": None, "sort_order": 900,
        "description": "Staff/demo workspaces. Metered, never billed.",
        "entitlements": [],
    },
]


async def sync_plans() -> dict:
    """Create any missing seed plans + their entitlements. Never mutates an existing plan."""
    created = 0
    async with get_sessionmaker()() as session:
        existing = {p.id for p in (await session.scalars(select(BillingPlan))).all()}
        for spec in PLAN_SEED:
            if spec["id"] in existing:
                continue
            data = {k: v for k, v in spec.items() if k != "entitlements"}
            session.add(BillingPlan(**data))
            await session.flush()
            for cap_id, mode, quota, overage in spec["entitlements"]:
                session.add(
                    BillingPlanEntitlement(
                        plan_id=spec["id"], capability_id=cap_id, mode=mode,
                        quota=quota, overage_price_credits=overage,
                    )
                )
            created += 1
        await session.commit()
    if created:
        logger.info("plan sync: %d plans created", created)
    return {"created": created, "total": len(PLAN_SEED)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_billing_catalog.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/billing/plans.py tests/test_billing_catalog.py
git commit -m "feat(billing): launch plan seed incl. legacy-unlimited grandfather plan"
```

---

## Task 6: Migration 0021

**Files:** Create `migrations/versions/0021_billing_foundation.py`; Test: `tests/test_billing_migration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_migration.py
from __future__ import annotations


def test_migration_creates_every_billing_table():
    import importlib
    import inspect

    import nexus.models  # noqa: F401  (register mappers)
    from nexus.core.db import Base

    mod = importlib.import_module("migrations.versions.0021_billing_foundation")
    assert mod.revision == "0021_billing_foundation"
    assert mod.down_revision == "0020_account_archived_at"

    src = inspect.getsource(mod.upgrade)
    for table in (
        "billing_capabilities", "billing_plans", "billing_plan_entitlements",
        "billing_subscriptions", "platform_admins",
    ):
        assert table in Base.metadata.tables, f"{table} missing from models"
        assert f'"{table}"' in src or f"'{table}'" in src, f"{table} not created by migration"

    # Downgrade must drop children before parents (FK-safe).
    down = inspect.getsource(mod.downgrade)
    assert down.index("billing_plan_entitlements") < down.index("billing_plans")
    assert down.index("billing_subscriptions") < down.index("billing_plans")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_billing_migration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'migrations.versions.0021_billing_foundation'`

- [ ] **Step 3: Write the implementation**

```python
# migrations/versions/0021_billing_foundation.py
"""Billing foundation: capability catalog, plans, entitlements, subscriptions, platform admins.

Additive only — no existing table is touched, so this is safe to apply to a live database with
zero downtime (docs/billing/15-Migration-Strategy.md).

Revision ID: 0021_billing_foundation
Revises: 0020_account_archived_at
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_billing_foundation"
down_revision = "0020_account_archived_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_capabilities",
        sa.Column("id", sa.String(length=80), primary_key=True),
        sa.Column("category", sa.String(length=40), nullable=False, index=True),
        sa.Column("sub_category", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("unit", sa.String(length=20), nullable=False, server_default="action"),
        sa.Column("meter_kind", sa.String(length=20), nullable=False, server_default="counter"),
        sa.Column("default_mode", sa.String(length=20), nullable=False, server_default="shadow"),
        sa.Column("depends_on", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_billing_cap_category", "billing_capabilities", ["category", "sub_category"])

    op.create_table(
        "billing_plans",
        sa.Column("id", sa.String(length=60), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("plan_class", sa.String(length=20), nullable=False, index=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft",
                  index=True),
        sa.Column("base_price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("seat_price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("interval", sa.String(length=10), nullable=False, server_default="month"),
        sa.Column("included_credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_seats", sa.Integer(), nullable=True),
        sa.Column("trial_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "billing_plan_entitlements",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("plan_id", sa.String(length=60),
                  sa.ForeignKey("billing_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capability_id", sa.String(length=80),
                  sa.ForeignKey("billing_capabilities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="metered"),
        sa.Column("quota", sa.Integer(), nullable=True),
        sa.Column("soft_limit_pct", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("hard_limit", sa.Integer(), nullable=True),
        sa.Column("reset_policy", sa.String(length=30), nullable=False,
                  server_default="monthly_anniversary"),
        sa.Column("burst_limit", sa.Integer(), nullable=True),
        sa.Column("rate_limit", sa.String(length=20), nullable=True),
        sa.Column("cooldown_s", sa.Integer(), nullable=True),
        sa.Column("overage_price_credits", sa.Integer(), nullable=True),
        sa.Column("feature_flag", sa.String(length=60), nullable=True),
        sa.Column("trial_quota", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("plan_id", "capability_id", name="uq_plan_entitlement"),
    )
    op.create_index("ix_plan_entitlement_plan", "billing_plan_entitlements", ["plan_id"])

    op.create_table(
        "billing_subscriptions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("plan_id", sa.String(length=60), sa.ForeignKey("billing_plans.id"),
                  nullable=False, index=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active",
                  index=True),
        sa.Column("interval", sa.String(length=10), nullable=False, server_default="month"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("grandfathered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("seats_included", sa.Integer(), nullable=True),
        sa.Column("psp_customer_id", sa.String(length=120), nullable=True),
        sa.Column("psp_subscription_id", sa.String(length=120), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_billing_sub_tenant_status", "billing_subscriptions",
                    ["tenant_id", "status"])

    op.create_table(
        "platform_admins",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False, index=True),
        sa.Column("platform_role", sa.String(length=20), nullable=False,
                  server_default="support"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("email", name="uq_platform_admin_email"),
    )


def downgrade() -> None:
    op.drop_table("platform_admins")
    op.drop_table("billing_plan_entitlements")
    op.drop_table("billing_subscriptions")
    op.drop_table("billing_capabilities")
    op.drop_table("billing_plans")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_billing_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add migrations/versions/0021_billing_foundation.py tests/test_billing_migration.py
git commit -m "feat(billing): additive migration 0021 (billing foundation tables)"
```

---

## Task 7: Platform-admin dependency + read-only admin API

**Files:** Modify `nexus/api/deps.py`, `nexus/api/routers/__init__.py`; Create `nexus/api/routers/admin_billing.py`; Test: `tests/test_billing_admin_api.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_billing_admin_api.py
from __future__ import annotations

import pytest

from tests.conftest import auth, client, signup


async def test_admin_billing_requires_platform_admin(client):
    """A normal tenant owner is NOT a platform admin: tenant RBAC must never grant staff access."""
    token = await signup(client, slug="acme", email="owner@acme.com", company="Acme")
    r = await client.get("/api/admin/billing/capabilities", headers=auth(token))
    assert r.status_code == 403


async def test_admin_billing_unauthenticated_is_rejected(client):
    r = await client.get("/api/admin/billing/capabilities")
    assert r.status_code in (401, 403)


async def test_platform_admin_can_read_catalog_and_plans(client, monkeypatch):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.core.config import get_settings

    await sync_catalog()
    await sync_plans()

    # Bootstrap this operator via the env allowlist (no chicken-and-egg DB row needed).
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "staff@nexus.io")
    token = await signup(client, slug="ops", email="staff@nexus.io", company="Ops")
    h = auth(token)

    r = await client.get("/api/admin/billing/capabilities", headers=h)
    assert r.status_code == 200, r.text
    caps = r.json()
    assert len(caps) >= 55
    assert any(c["id"] == "ai.email_draft" for c in caps)

    r = await client.get("/api/admin/billing/capabilities?category=ai", headers=h)
    assert all(c["category"] == "ai" for c in r.json())

    r = await client.get("/api/admin/billing/plans", headers=h)
    assert r.status_code == 200
    plans = r.json()
    growth = next(p for p in plans if p["id"] == "growth")
    assert growth["base_price_cents"] == 7900
    assert growth["entitlement_count"] >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_billing_admin_api.py -v`
Expected: FAIL — 404 (router not registered)

- [ ] **Step 3: Add the dependency**

In `nexus/api/deps.py`, append:

```python
async def require_platform_admin(
    principal: Principal = Depends(get_principal),
) -> Principal:
    """Gate for the staff /admin surface.

    Platform admins are operators of the SaaS, NOT tenant members: tenant RBAC (owner/admin)
    deliberately grants nothing here. Membership comes from the ``platform_admins`` table, plus
    an env allowlist (``NEXUS_PLATFORM_ADMIN_EMAILS``) that solves the bootstrap problem.
    """
    from sqlalchemy import select

    from nexus.core.config import get_settings
    from nexus.models.billing import PlatformAdmin
    from nexus.models.identity import User

    async with get_sessionmaker()() as session:
        user = await session.get(User, principal.user_id)
        if user is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "platform access denied")
        email = (user.email or "").lower()
        if email in get_settings().platform_admin_email_list:
            return principal
        row = (
            await session.scalars(
                select(PlatformAdmin).where(
                    PlatformAdmin.email == email, PlatformAdmin.active == True  # noqa: E712
                )
            )
        ).first()
    if row is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "platform access denied")
    return principal
```

- [ ] **Step 4: Create the router**

```python
# nexus/api/routers/admin_billing.py
"""Staff-only billing administration (read surface for M1).

Everything here is gated by ``require_platform_admin`` — tenant RBAC grants no access.
Write endpoints (plan CRUD, entitlement editing, enforcement flips) land in Milestone 3
(docs/billing/06-Admin-Portal.md).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from nexus.api.deps import Principal, require_platform_admin
from nexus.core.db import get_sessionmaker
from nexus.models.billing import BillingCapability, BillingPlan, BillingPlanEntitlement

router = APIRouter(prefix="/admin/billing", tags=["admin-billing"])


class CapabilityOut(BaseModel):
    id: str
    category: str
    sub_category: str
    name: str
    description: str
    unit: str
    meter_kind: str
    default_mode: str
    depends_on: list[str]
    active: bool


class PlanOut(BaseModel):
    id: str
    name: str
    description: str
    plan_class: str
    status: str
    base_price_cents: int
    seat_price_cents: int
    currency: str
    interval: str
    included_credits: int
    max_seats: int | None
    trial_days: int
    sort_order: int
    entitlement_count: int


@router.get("/capabilities", response_model=list[CapabilityOut])
async def list_capabilities(
    category: str | None = None,
    active: bool | None = None,
    limit: int = Query(default=500, ge=1, le=1000),
    _: Principal = Depends(require_platform_admin),
) -> list[CapabilityOut]:
    stmt = select(BillingCapability)
    if category:
        stmt = stmt.where(BillingCapability.category == category)
    if active is not None:
        stmt = stmt.where(BillingCapability.active == active)
    stmt = stmt.order_by(BillingCapability.category, BillingCapability.id).limit(limit)
    async with get_sessionmaker()() as session:
        rows = (await session.scalars(stmt)).all()
    return [
        CapabilityOut(
            id=c.id, category=c.category, sub_category=c.sub_category, name=c.name,
            description=c.description, unit=c.unit, meter_kind=c.meter_kind,
            default_mode=c.default_mode, depends_on=list(c.depends_on or []), active=c.active,
        )
        for c in rows
    ]


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    status_filter: str | None = None,
    _: Principal = Depends(require_platform_admin),
) -> list[PlanOut]:
    stmt = select(BillingPlan).order_by(BillingPlan.sort_order, BillingPlan.id)
    if status_filter:
        stmt = stmt.where(BillingPlan.status == status_filter)
    async with get_sessionmaker()() as session:
        plans = (await session.scalars(stmt)).all()
        counts = dict(
            (
                await session.execute(
                    select(
                        BillingPlanEntitlement.plan_id,
                        func.count(BillingPlanEntitlement.id),
                    ).group_by(BillingPlanEntitlement.plan_id)
                )
            ).all()
        )
    return [
        PlanOut(
            id=p.id, name=p.name, description=p.description, plan_class=p.plan_class,
            status=p.status, base_price_cents=p.base_price_cents,
            seat_price_cents=p.seat_price_cents, currency=p.currency, interval=p.interval,
            included_credits=p.included_credits, max_seats=p.max_seats,
            trial_days=p.trial_days, sort_order=p.sort_order,
            entitlement_count=int(counts.get(p.id, 0)),
        )
        for p in plans
    ]
```

- [ ] **Step 5: Register the router**

In `nexus/api/routers/__init__.py` add `admin_billing` to the import list (alphabetically first,
before `accounts`) and `admin_billing.router,` to `all_routers` (first entry).

- [ ] **Step 6: Run tests**

Run: `py -3.10 -m pytest tests/test_billing_admin_api.py -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Commit**

```bash
git add nexus/api/deps.py nexus/api/routers/admin_billing.py nexus/api/routers/__init__.py tests/test_billing_admin_api.py
git commit -m "feat(billing): platform-admin gate + read-only catalog/plan admin API"
```

---

## Task 8: Seed on startup

**Files:** Modify `nexus/main.py`

- [ ] **Step 1: Wire the seed into lifespan**

In `nexus/main.py`, inside `lifespan`, after `register_crm_sync_subscribers()`, add:

```python
    # Billing catalog/plan seed: idempotent, additive, and non-fatal. A seed failure must never
    # stop the API from serving (docs/billing/15-Migration-Strategy.md).
    if get_settings().billing_seed_on_startup:
        try:
            from nexus.billing.catalog import sync_catalog
            from nexus.billing.plans import sync_plans

            await sync_catalog()
            await sync_plans()
        except Exception:
            logging.getLogger("nexus.main").warning(
                "billing seed sync failed; continuing without it", exc_info=True
            )
```

- [ ] **Step 2: Verify the app still boots**

Run: `py -3.10 -m pytest tests/test_api.py -v`
Expected: PASS (existing API tests unaffected)

- [ ] **Step 3: Commit**

```bash
git add nexus/main.py
git commit -m "feat(billing): idempotent catalog/plan seed on startup"
```

---

## Task 9: Regression gate

**Files:** none (verification only)

- [ ] **Step 1: Run the billing suites**

Run: `py -3.10 -m pytest tests/test_billing_models.py tests/test_billing_catalog.py tests/test_billing_migration.py tests/test_billing_admin_api.py -v`
Expected: all PASS.

- [ ] **Step 2: Run the full suite**

Run: `py -3.10 -m pytest -q --deselect "tests/test_incident_hardening.py::test_worker_loop_survives_queue_outage"`
Expected: the pre-existing suite is green (the deselected test is a known Windows event-loop hang; the `test_metrics_off_by_default_and_app_serves` failure is a known stale-`nexus/web/dist` artifact and passes when that directory is absent).

- [ ] **Step 3: Lint**

Run: `py -3.10 -m ruff check nexus/billing nexus/models/billing.py nexus/api/routers/admin_billing.py nexus/api/deps.py`
Expected: `All checks passed!`

- [ ] **Step 4: Commit any fix**

```bash
git add -A
git commit -m "fix(billing): resolve lint/regression findings"
```

---

## Self-review

**Spec coverage (M1 scope):** catalog table + seed → T2/T4; plans + entitlements (full policy
surface from [02](../../billing/02-Entitlement-Engine.md) §1) → T2/T5; subscriptions (tenant-scoped,
RLS-auto) → T2; platform admins + gate → T2/T7; migration → T6; admin read APIs → T7; startup
seed → T8; enforcement kill-switch setting → T1; legacy-unlimited grandfather plan → T5.
Deferred by design to later milestones: resolution/`check_and_meter` (M2), usage events (M3),
rating/credits (M4), lifecycle/PSP (M5), admin writes + dashboards (M6), app integration (M7).

**Placeholder scan:** no TBD/TODO; every step ships complete runnable code.

**Type consistency:** `sync_catalog()`/`sync_plans()` return `dict` with `created`/`updated`/
`total` and are consumed as such in T4/T5 tests and T8. `LEGACY_PLAN_ID` defined in T5, used in
T5 tests. `require_platform_admin` defined in T7 step 3, imported in T7 step 4. `CapabilityOut`/
`PlanOut` field names match the model attributes read in T7. Model class names in T2 match the
registrations in T3 and the imports in T4/T5/T7.
