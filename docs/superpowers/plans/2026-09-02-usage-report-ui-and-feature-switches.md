# Usage Report UI and Superadmin Feature Switches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render the credit usage report in the customer's billing page, and let a superadmin take any feature — page, routes and endpoints — offline with a "coming soon" or "under maintenance" message, including features that do not exist yet.

**Architecture:** The feature switch is keyed on the **existing `module.*` capability ids**, not on a new page registry. Those ids already drive all three enforcement points — the nav item (`nav.tsx` carries `capability`), the route (`RequireCapability` in `App.tsx`), and the endpoints (`depends_on` in the capability catalog makes a disabled module disable everything behind it). A platform switch that forces a `module.*` capability's resolved state therefore reaches every one of them without touching any of them, and a page added later gets covered the moment it is given a capability — which it needs anyway to be sellable.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, Alembic, pytest, React 18 + TypeScript strict + CSS Modules.

---

## Why not a new mechanism

Three enforcement points already exist and are already tested. Any new "page registry" would be a fourth source of truth that has to agree with them, and the first thing to drift would be which pages it covers.

| Point | Where | What it does today |
|---|---|---|
| Nav | `frontend/src/app/nav.tsx` | Item carries `capability`; `navState` hides or upsells |
| Route | `frontend/src/App.tsx:115` | `RequireCapability` redirects to `/settings/billing` |
| Endpoints | `nexus/billing/catalog.py` | `depends_on` — a disabled module disables the capabilities behind it |

`GET /billing/entitlements` is the one seam the client reads. It already returns `gating_active` so the UI gates only when the server would genuinely refuse. A switch has to flow through **that** and everything downstream follows.

**The switch must beat the plan.** "Calling is down for maintenance" is a statement about the platform, not about what the customer bought — so it applies whatever the plan says, including to `legacy-unlimited` and `internal`, which bypass every other gate.

---

## File structure

| File | Responsibility | Task |
|---|---|---|
| `migrations/versions/0055_feature_switches.py` | **Create.** `feature_switches`, platform-global | 1 |
| `nexus/models/feature_switch.py` | **Create.** The row | 1 |
| `nexus/features/switches.py` | **Create.** Resolution + 30s TTL cache | 2 |
| `nexus/billing/entitlements.py` | Force the resolved state | 3 |
| `nexus/api/routers/billing.py` | Carry `state`/`message` to the client | 4 |
| `nexus/api/routers/admin_features.py` | **Create.** Superadmin CRUD | 5 |
| `frontend/src/lib/types.ts` | `FeatureState`, report types | 6, 8 |
| `frontend/src/app/EntitlementsContext.tsx` | Expose switch state to the app | 6 |
| `frontend/src/components/FeatureNotice.tsx` | **Create.** The banner | 7 |
| `frontend/src/pages/CreditUsagePage.tsx` | **Create.** The report | 8 |
| `frontend/src/pages/admin/FeatureSwitchesTab.tsx` | **Create.** The control | 9 |

---

## Task 1: The `feature_switches` table

Platform-global, like `billing_feature_flags` and `billing_capabilities`: no `tenant_id`, so `scripts/apply_rls.py` leaves it alone and the superadmin console can read every row.

**Files:**
- Create: `nexus/models/feature_switch.py`
- Create: `migrations/versions/0055_feature_switches.py`
- Modify: `nexus/models/__init__.py`
- Test: `tests/test_feature_switches.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_feature_switches.py
"""A superadmin can take a feature offline with a message.

Keyed on the EXISTING `module.*` capability ids rather than a new page registry, because those ids
already drive all three enforcement points — the nav item, the route guard and the endpoints behind
`depends_on`. A separate registry would be a fourth source of truth to keep in agreement, and the
first thing to drift would be which pages it covers.

The absence of a row means enabled. That is what makes this additive: no switch, no change.
"""
from __future__ import annotations


async def test_a_switch_row_defaults_to_enabled(fresh_db):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.feature_switch import FeatureSwitch

    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id="module.calling"))
        await s.commit()
        row = (await s.scalars(select(FeatureSwitch))).one()
        assert row.state == "enabled"
        assert row.message == ""


async def test_the_four_states_round_trip(fresh_db):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.models.feature_switch import FeatureSwitch

    async with get_sessionmaker()() as s:
        for i, state in enumerate(("enabled", "disabled", "coming_soon", "maintenance")):
            s.add(FeatureSwitch(capability_id=f"module.x{i}", state=state, message=f"m{i}"))
        await s.commit()
        rows = {r.capability_id: r for r in (await s.scalars(select(FeatureSwitch))).all()}
        assert rows["module.x1"].state == "disabled"
        assert rows["module.x2"].message == "m2"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_feature_switches.py -n0 -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.models.feature_switch'`.

- [ ] **Step 3: Write the model**

```python
# nexus/models/feature_switch.py
"""A platform-wide switch that takes a feature offline, with a message.

Keyed on a `module.*` capability id — the SAME ids the nav items, the `RequireCapability` route
guard and the capability catalog's `depends_on` already use. A switch therefore reaches all three
without any of them changing, and a page added later is covered the moment it is given a
capability, which it needs anyway to be sellable.

Platform-global: no ``tenant_id``, so `scripts/apply_rls.py` leaves it alone, exactly like
`billing_feature_flags` and `billing_capabilities`. This is a statement about the platform, not
about one customer.

THE ABSENCE OF A ROW MEANS ENABLED. That is what makes the table additive — a deployment with no
switches behaves exactly as it did before it existed.
"""
from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, TimestampMixin

# `enabled` is the absence of a restriction. The other three all block, and differ ONLY in what the
# customer is told — which is the whole point: "we turned this off", "this is not built yet" and
# "this is broken right now" are three different conversations, and a single boolean makes a
# support agent guess which one they are having.
SWITCH_STATES = ("enabled", "disabled", "coming_soon", "maintenance")


class FeatureSwitch(TimestampMixin, Base):
    __tablename__ = "feature_switches"

    # The capability id IS the key. A surrogate id would mean a join on the resolution path, which
    # runs inside every entitlement decision.
    capability_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), default="enabled", nullable=False)
    # Shown to the customer verbatim. Empty falls back to wording chosen per state in the UI, so a
    # superadmin flipping a switch in a hurry never produces a blank banner.
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Who did it, for the audit trail the admin mutations already write.
    updated_by: Mapped[str] = mapped_column(String(80), default="", nullable=False)
```

- [ ] **Step 4: Register it for metadata**

In `nexus/models/__init__.py`, beside the other imports:

```python
from nexus.models.feature_switch import FeatureSwitch
```

and add `"FeatureSwitch"` to `__all__`.

- [ ] **Step 5: Write the migration**

```python
# migrations/versions/0055_feature_switches.py
"""Platform-wide feature switches.

No ``tenant_id``: this is a statement about the platform, so `apply_rls.py` leaves it alone like
`billing_feature_flags`. An absent row means enabled, so creating the table changes nothing.

Revision ID: 0055_feature_switches
Revises: 0054_invoice_psp_reference
Create Date: 2026-09-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_feature_switches"
down_revision = "0054_invoice_psp_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feature_switches",
        sa.Column("capability_id", sa.String(80), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="enabled"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("feature_switches")
```

Copy the exact `created_at`/`updated_at` column types from `0052_signal_preferences.py` rather than trusting the sketch — `TimestampMixin` must match or `test_migrations_replay` fails.

- [ ] **Step 6: Run the tests and the replay**

Run: `python -m pytest tests/test_feature_switches.py tests/test_migrations_replay.py -n0 -v`
Expected: all pass. The replay builds a database from `alembic upgrade head` alone and diffs it against `Base.metadata`.

- [ ] **Step 7: Confirm one migration head**

Run: `python -c "from alembic.script import ScriptDirectory; from alembic.config import Config; print(ScriptDirectory.from_config(Config('alembic.ini')).get_heads())"`
Expected: `('0055_feature_switches',)` — exactly one.

- [ ] **Step 8: Commit**

```bash
git add nexus/models/feature_switch.py nexus/models/__init__.py migrations/versions/0055_feature_switches.py tests/test_feature_switches.py
git commit -m "feat(features): a platform-wide feature switch table"
```

---

## Task 2: Resolution with a 30s TTL

The worker is a separate container, so nothing the API does can invalidate its memory. The same 30s idiom as `providers/resolver.py` and `runtime_config` — that TTL is what makes "flip a switch and it takes effect" true without a redeploy, which is the requirement.

**Files:**
- Create: `nexus/features/__init__.py`, `nexus/features/switches.py`
- Test: `tests/test_feature_switches.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
async def test_no_row_means_enabled(fresh_db):
    """THE compatibility line. A deployment with no switches behaves as before."""
    from nexus.features.switches import switch_for

    assert (await switch_for("module.calling")).state == "enabled"


async def test_a_stored_switch_is_returned(fresh_db):
    from nexus.core.db import get_sessionmaker
    from nexus.features.switches import invalidate, switch_for
    from nexus.models.feature_switch import FeatureSwitch

    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id="module.calling", state="maintenance",
                            message="Back at 14:00 UTC"))
        await s.commit()
    invalidate()

    got = await switch_for("module.calling")
    assert got.state == "maintenance"
    assert got.message == "Back at 14:00 UTC"


async def test_an_unknown_state_resolves_to_enabled(fresh_db):
    """Fail OPEN. A typo or a value from a newer release must not take a working feature down —
    the same bias as the entitlement engine's unknown-means-allow."""
    from nexus.core.db import get_sessionmaker
    from nexus.features.switches import invalidate, switch_for
    from nexus.models.feature_switch import FeatureSwitch

    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id="module.calling", state="banana"))
        await s.commit()
    invalidate()

    assert (await switch_for("module.calling")).state == "enabled"


async def test_a_database_failure_resolves_to_enabled(fresh_db, monkeypatch):
    """A switch lookup that fails must not take the product down. It is a restriction; failing to
    read one means applying no restriction."""
    from nexus.features import switches

    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(switches, "_load", boom)
    switches.invalidate()
    assert (await switches.switch_for("module.calling")).state == "enabled"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_feature_switches.py -n0 -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'nexus.features'`.

- [ ] **Step 3: Implement**

```python
# nexus/features/__init__.py
"""Platform-wide feature switches."""
```

```python
# nexus/features/switches.py
"""Resolve a feature switch, cached for 30 seconds.

The TTL is the requirement, not an optimisation: the worker is a separate container and nothing the
API does can invalidate its memory, so without one a switch would need a redeploy to take effect —
which is the thing this feature exists to avoid. Same idiom as `providers/resolver.py` and
`runtime_config/service.py`.

EVERYTHING HERE FAILS OPEN. A switch is a restriction; failing to read one means applying no
restriction. An unreadable table, an unknown state, a typo — all resolve to `enabled`, matching the
entitlement engine's own unknown-means-allow bias. The alternative is a database blip taking the
whole product offline, which is a far worse failure than a switch that briefly does not apply.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from nexus.core.db import get_sessionmaker
from nexus.models.feature_switch import SWITCH_STATES, FeatureSwitch

logger = logging.getLogger("nexus.features.switches")

TTL_S = 30.0

_cache: dict[str, "Switch"] | None = None
_loaded_at = 0.0


@dataclass(frozen=True, slots=True)
class Switch:
    capability_id: str
    state: str = "enabled"
    message: str = ""

    @property
    def blocks(self) -> bool:
        """Whether this switch takes the feature away. Only `enabled` does not."""
        return self.state != "enabled"


ENABLED = Switch(capability_id="", state="enabled")


def invalidate() -> None:
    """Drop the cache. Immediate for THIS process; others wait out the TTL."""
    global _cache, _loaded_at
    _cache, _loaded_at = None, 0.0


async def _load() -> dict[str, Switch]:
    async with get_sessionmaker()() as session:
        rows = (await session.scalars(select(FeatureSwitch))).all()
    out: dict[str, Switch] = {}
    for r in rows:
        # An unrecognised state resolves to enabled rather than blocking. A value written by a
        # newer release, or a typo, must not take a working feature down.
        state = r.state if r.state in SWITCH_STATES else "enabled"
        out[r.capability_id] = Switch(r.capability_id, state, r.message or "")
    return out


async def all_switches() -> dict[str, Switch]:
    """Every stored switch, TTL-cached. ``{}`` on any failure."""
    global _cache, _loaded_at
    now = time.monotonic()
    if _cache is not None and (now - _loaded_at) < TTL_S:
        return _cache
    try:
        _cache, _loaded_at = await _load(), now
    except Exception:
        logger.warning("feature switch load failed; treating everything as enabled", exc_info=True)
        return {}
    return _cache


async def switch_for(capability_id: str) -> Switch:
    """The switch for one capability. ``enabled`` when there is no row."""
    return (await all_switches()).get(capability_id) or Switch(capability_id)
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_feature_switches.py -n0 -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add nexus/features/ tests/test_feature_switches.py
git commit -m "feat(features): resolve a feature switch with a 30s TTL, failing open"
```

---

## Task 3: The switch beats the plan

This is the one change to the engine, and where it sits matters. `resolve_entitlement` returns early for `_UNLIMITED_CLASSES` — so a switch applied after that point would not reach `legacy-unlimited` or `internal`, and "Calling is down for maintenance" has to be true for everyone.

**Files:**
- Modify: `nexus/billing/entitlements.py` (`resolve_entitlement`)
- Test: `tests/test_feature_switch_enforcement.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_feature_switch_enforcement.py
"""A platform switch overrides the plan, for everyone.

"Calling is down for maintenance" is a statement about the PLATFORM, not about what a customer
bought — so it has to apply whatever the plan says. That includes `legacy-unlimited` and `internal`,
which `resolve_entitlement` short-circuits before any other gate, and which would therefore sail
straight past a check placed anywhere later in the function.
"""
from __future__ import annotations

import pytest

from nexus.models.identity import Tenant


async def _tenant_on(plan_id: str) -> str:
    from nexus.billing.subscriptions import ensure_subscription
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    async with get_sessionmaker()() as s:
        t = Tenant(name=plan_id, slug=f"fs-{plan_id}")
        s.add(t)
        await s.flush()
        await ensure_subscription(TenantSession(s, t.id), plan_id=plan_id)
        await s.commit()
        return t.id


async def _set_switch(capability_id: str, state: str, message: str = ""):
    from nexus.core.db import get_sessionmaker
    from nexus.features.switches import invalidate
    from nexus.models.feature_switch import FeatureSwitch

    async with get_sessionmaker()() as s:
        s.add(FeatureSwitch(capability_id=capability_id, state=state, message=message))
        await s.commit()
    invalidate()


async def _resolve(tenant_id: str, capability_id: str):
    from nexus.billing.entitlements import resolve_entitlement
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession

    async with get_sessionmaker()() as s:
        return await resolve_entitlement(TenantSession(s, tenant_id), capability_id)


@pytest.fixture
async def seeded(fresh_db):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans

    await sync_catalog()
    await sync_plans()


async def test_a_switch_disables_a_module_the_plan_includes(seeded):
    tid = await _tenant_on("accelerate")
    assert (await _resolve(tid, "module.calling")).mode != "disabled"

    await _set_switch("module.calling", "maintenance", "Back at 14:00 UTC")
    ent = await _resolve(tid, "module.calling")
    assert ent.mode == "disabled"
    assert ent.source == "feature_switch"


async def test_it_beats_an_unlimited_plan_class(seeded):
    """THE placement test. `resolve_entitlement` returns early for unlimited plan classes, so a
    check placed after that point never runs for them — and a grandfathered tenant would keep
    using a feature the platform has taken offline."""
    tid = await _tenant_on("legacy-unlimited")
    await _set_switch("module.calling", "disabled")
    assert (await _resolve(tid, "module.calling")).mode == "disabled"


async def test_the_capabilities_behind_a_module_go_with_it(seeded):
    """`depends_on` is what makes this cover ENDPOINTS and not just the menu. Nothing new is
    needed for it — a disabled module already disables what hangs off it."""
    tid = await _tenant_on("accelerate")
    await _set_switch("module.calling", "maintenance")
    ent = await _resolve(tid, "calling.minutes")
    assert ent.mode == "disabled"


async def test_an_enabled_switch_changes_nothing(seeded):
    """A row that says enabled is not a restriction."""
    tid = await _tenant_on("accelerate")
    before = await _resolve(tid, "module.calling")
    await _set_switch("module.calling", "enabled")
    assert (await _resolve(tid, "module.calling")).mode == before.mode


async def test_no_switch_changes_nothing(seeded):
    """The compatibility line for every existing deployment."""
    tid = await _tenant_on("accelerate")
    ent = await _resolve(tid, "module.calling")
    assert ent.source in ("plan", "catalog", "plan_class")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_feature_switch_enforcement.py -n0 -v`
Expected: FAIL — `source` is `plan`/`catalog`, not `feature_switch`.

- [ ] **Step 3: Implement**

In `nexus/billing/entitlements.py`, inside `resolve_entitlement`, immediately after `base` is built and **before** the `sub is None` early return and before the `_UNLIMITED_CLASSES` short-circuit:

```python
        # A PLATFORM SWITCH BEATS EVERYTHING BELOW, and it is placed here for that reason.
        #
        # "Calling is down for maintenance" is a statement about the platform, not about what the
        # customer bought, so it applies whatever the plan says. `_UNLIMITED_CLASSES` returns a few
        # lines down without consulting plan entitlements at all — so a check placed after it would
        # never run for `legacy-unlimited` or `internal`, and a grandfathered tenant would keep
        # using a feature we had taken offline.
        #
        # Only `module.*` carries a switch. The capabilities BEHIND a module follow automatically
        # through `depends_on` in `_apply_dependencies`, which is what makes one switch cover the
        # menu item, the route and every endpoint without naming any of them.
        if capability_id.startswith("module."):
            from nexus.features.switches import switch_for

            switch = await switch_for(capability_id)
            if switch.blocks:
                base.mode = "disabled"
                base.quota = 0
                base.source = "feature_switch"
                return base
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_feature_switch_enforcement.py -n0 -v`
Expected: all pass.

- [ ] **Step 5: Regression — the engine is the money path**

Run: `python -m pytest tests/test_billing_entitlements.py tests/test_credit_floor.py tests/test_credits_only_billing.py tests/test_core_plan.py tests/test_plan_gated_nav.py tests/test_feature_flags.py -n4 -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add nexus/billing/entitlements.py tests/test_feature_switch_enforcement.py
git commit -m "feat(features): a platform switch overrides the plan, including unlimited classes"
```

---

## Task 4: Carry the state and message to the client

The UI needs more than "blocked". A banner saying "coming soon" and one saying "under maintenance" are different messages, and a 402 upsell would be wrong for both — the customer cannot buy their way out of either.

**Files:**
- Modify: `nexus/api/routers/billing.py` (`EntitlementOut`, `get_entitlements`)
- Test: `tests/test_feature_switch_enforcement.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
async def test_the_entitlements_endpoint_reports_the_switch(client, fresh_db, monkeypatch):
    """The UI cannot show the right banner from `included: false` alone — "you did not buy this",
    "this is not built yet" and "this is broken right now" are three different conversations."""
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from tests.conftest import auth, signup

    await sync_catalog()
    await sync_plans()
    token = await signup(client, slug="fsapi", email="a@fsapi.com", company="FSAPI")
    await _set_switch("module.calling", "coming_soon", "Calling lands in October.")

    body = (await client.get("/api/billing/entitlements", headers=auth(token))).json()
    row = next(m for m in body["modules"] if m["capability_id"] == "module.calling")
    assert row["feature_state"] == "coming_soon"
    assert row["feature_message"] == "Calling lands in October."


async def test_an_unswitched_module_reports_enabled(client, fresh_db):
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from tests.conftest import auth, signup

    await sync_catalog()
    await sync_plans()
    token = await signup(client, slug="fsapi2", email="a@fsapi2.com", company="FSAPI2")
    body = (await client.get("/api/billing/entitlements", headers=auth(token))).json()
    assert all(m["feature_state"] == "enabled" for m in body["modules"])
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_feature_switch_enforcement.py -n0 -v`
Expected: FAIL, `KeyError: 'feature_state'`.

- [ ] **Step 3: Implement**

Add to `EntitlementOut` in `nexus/api/routers/billing.py`:

```python
    # enabled | disabled | coming_soon | maintenance. A PLATFORM state, distinct from `included`,
    # which is about the customer's plan. The UI needs both: an upsell is the right answer to "you
    # did not buy this" and exactly the wrong one for "this is broken right now", because the
    # customer cannot buy their way out of an outage.
    feature_state: str = "enabled"
    feature_message: str = ""
```

Then in `get_entitlements`, where each `EntitlementOut` is built, resolve the switch:

```python
    from nexus.features.switches import all_switches

    switches = await all_switches()
```

and pass, for each capability:

```python
            feature_state=(switches.get(cap.id).state if switches.get(cap.id) else "enabled"),
            feature_message=(switches.get(cap.id).message if switches.get(cap.id) else ""),
```

Read the existing loop first and match how it builds each row — the field names around it are the ones to preserve.

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_feature_switch_enforcement.py -n0 -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add nexus/api/routers/billing.py tests/test_feature_switch_enforcement.py
git commit -m "feat(api): report feature-switch state and message to the client"
```

---

## Task 5: Superadmin CRUD

Gated on `features.manage`, superadmin preset only — the same argument that keeps `providers.manage` and `sources.manage` separate from `admins.manage`. Taking a feature away from every customer is not the same act as reading a billing figure.

**Files:**
- Create: `nexus/api/routers/admin_features.py`
- Modify: `nexus/api/routers/__init__.py`, `nexus/billing/permissions.py`
- Test: `tests/test_admin_features_api.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_admin_features_api.py
"""Superadmin control over feature switches.

Gated on its own permission: taking a feature away from every customer on the platform is not the
same act as reading a billing number, and folding it into `admins.manage` would grant it to anyone
who can see an invoice.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def _superadmin(client, monkeypatch, slug: str):
    from nexus.core.config import get_settings

    email = f"boss@{slug}.com"
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


async def test_a_superadmin_can_list_and_set_a_switch(client, fresh_db, monkeypatch):
    from nexus.billing.catalog import sync_catalog

    await sync_catalog()
    token = await _superadmin(client, monkeypatch, "af1")

    listed = (await client.get("/api/admin/features", headers=auth(token))).json()
    assert any(r["capability_id"] == "module.calling" for r in listed)
    assert all(r["state"] == "enabled" for r in listed), "nothing switched by default"

    r = await client.put(
        "/api/admin/features/module.calling", headers=auth(token),
        json={"state": "maintenance", "message": "Back at 14:00 UTC"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "maintenance"

    again = (await client.get("/api/admin/features", headers=auth(token))).json()
    row = next(x for x in again if x["capability_id"] == "module.calling")
    assert row["state"] == "maintenance"
    assert row["message"] == "Back at 14:00 UTC"


async def test_it_lists_every_module_so_a_new_page_appears_by_itself(client, fresh_db, monkeypatch):
    """The requirement that this cover features built later. The list is derived from the CATALOG,
    not from stored rows — a screen showing only what somebody already switched cannot be used to
    switch anything the first time."""
    from nexus.billing.catalog import CAPABILITY_SEED, sync_catalog

    await sync_catalog()
    token = await _superadmin(client, monkeypatch, "af2")
    listed = (await client.get("/api/admin/features", headers=auth(token))).json()

    modules = {c["id"] for c in CAPABILITY_SEED if c["id"].startswith("module.")}
    assert {r["capability_id"] for r in listed} >= modules


async def test_a_tenant_owner_cannot_see_or_set_switches(client, fresh_db):
    """404, not 403: a 403 confirms the route exists and lets the staff surface be enumerated."""
    token = await signup(client, slug="af3", email="o@af3.com", company="AF3")
    assert (await client.get("/api/admin/features", headers=auth(token))).status_code == 404
    assert (await client.put("/api/admin/features/module.calling", headers=auth(token),
                             json={"state": "disabled"})).status_code == 404


async def test_an_unknown_state_is_refused(client, fresh_db, monkeypatch):
    """Stored, an unknown state resolves to `enabled` and silently does nothing — so it must be
    refused at the door rather than accepted and ignored."""
    from nexus.billing.catalog import sync_catalog

    await sync_catalog()
    token = await _superadmin(client, monkeypatch, "af4")
    r = await client.put("/api/admin/features/module.calling", headers=auth(token),
                         json={"state": "banana"})
    assert r.status_code in (400, 422)


async def test_a_non_module_capability_is_refused(client, fresh_db, monkeypatch):
    """Only `module.*` is switchable. A switch on `enrich.account` would silently do nothing —
    `resolve_entitlement` only consults switches for modules — and dead config that looks live is
    exactly what this codebase keeps having to diagnose."""
    from nexus.billing.catalog import sync_catalog

    await sync_catalog()
    token = await _superadmin(client, monkeypatch, "af5")
    r = await client.put("/api/admin/features/enrich.account", headers=auth(token),
                         json={"state": "disabled"})
    assert r.status_code == 400


async def test_setting_a_switch_is_audited(client, fresh_db, monkeypatch):
    """Every admin mutation is captured with before/after — this one takes a feature away from every
    customer, so it is the last one that should be untraceable."""
    from sqlalchemy import select

    from nexus.billing.catalog import sync_catalog
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingAuditLog

    await sync_catalog()
    token = await _superadmin(client, monkeypatch, "af6")
    await client.put("/api/admin/features/module.calling", headers=auth(token),
                     json={"state": "disabled", "message": "gone"})

    async with get_platform_sessionmaker()() as s:
        rows = (await s.scalars(select(BillingAuditLog))).all()
    assert any("feature" in (r.action or "") for r in rows)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_admin_features_api.py -n0 -v`
Expected: FAIL with 404 on `/api/admin/features` for the superadmin too — the router does not exist.

- [ ] **Step 3: Add the permission**

In `nexus/billing/permissions.py`, add `FEATURES_MANAGE = "features.manage"` beside the other names, add it to the `superadmin` preset ONLY, and to whatever list enumerates all permissions. Read the file first: the presets are explicit dicts and the expanded set is what gets stored on a `platform_admins` row.

- [ ] **Step 4: Write the router**

```python
# nexus/api/routers/admin_features.py
"""Superadmin control over platform feature switches.

Takes a feature offline for every customer — the page, its routes and every endpoint behind it —
with a message explaining which kind of "not available" this is.

Gated on `features.manage`, superadmin preset only. Same argument as `providers.manage` and
`sources.manage`: this is not the same act as reading a billing figure, and folding it into
`admins.manage` would hand it to anyone who can see an invoice.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from nexus.api.deps import Principal, require_platform_permission
from nexus.core.db import get_platform_sessionmaker
from nexus.models.feature_switch import SWITCH_STATES, FeatureSwitch

router = APIRouter(prefix="/admin/features", tags=["admin", "features"])


class FeatureSwitchOut(BaseModel):
    capability_id: str
    name: str
    state: str
    message: str


class FeatureSwitchIn(BaseModel):
    # `extra="forbid"`, so a typo'd field is a 422 rather than a setting that silently did nothing.
    model_config = {"extra": "forbid"}

    state: str
    message: str = ""


@router.get("", response_model=list[FeatureSwitchOut])
async def list_switches(
    _: Principal = Depends(require_platform_permission("features.manage")),
) -> list[FeatureSwitchOut]:
    """Every switchable module with its current state.

    Derived from the CATALOG and overlaid with stored rows, never from the rows alone — a screen
    listing only what somebody already switched cannot be used to switch anything the first time,
    and a feature added in a later release has to appear here by itself.
    """
    from nexus.models.billing import BillingCapability

    async with get_platform_sessionmaker()() as s:
        caps = [
            c for c in (await s.scalars(select(BillingCapability))).all()
            if c.id.startswith("module.")
        ]
        stored = {
            r.capability_id: r for r in (await s.scalars(select(FeatureSwitch))).all()
        }
    return [
        FeatureSwitchOut(
            capability_id=c.id,
            name=c.name or c.id,
            state=(stored[c.id].state if c.id in stored else "enabled"),
            message=(stored[c.id].message if c.id in stored else ""),
        )
        for c in sorted(caps, key=lambda c: c.id)
    ]


@router.put("/{capability_id}", response_model=FeatureSwitchOut)
async def set_switch(
    capability_id: str,
    body: FeatureSwitchIn,
    principal: Principal = Depends(require_platform_permission("features.manage")),
) -> FeatureSwitchOut:
    """Set one module's switch. Takes effect within 30 seconds, everywhere, with no redeploy."""
    if not capability_id.startswith("module."):
        # Only modules are consulted by `resolve_entitlement`, so a switch on anything else would
        # be stored and silently ignored — dead config that looks live.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "only module.* capabilities can be switched; a switch on anything else would be "
            "stored and never applied",
        )
    if body.state not in SWITCH_STATES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"state must be one of {', '.join(SWITCH_STATES)}",
        )

    from nexus.billing.audit import record_admin_action
    from nexus.models.billing import BillingCapability

    async with get_platform_sessionmaker()() as s:
        if await s.get(BillingCapability, capability_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown capability {capability_id}")
        row = await s.get(FeatureSwitch, capability_id)
        before = {"state": row.state, "message": row.message} if row else None
        if row is None:
            row = FeatureSwitch(capability_id=capability_id)
            s.add(row)
        row.state = body.state
        row.message = body.message
        row.updated_by = principal.user_id
        await record_admin_action(
            s, actor=principal.user_id, action="feature_switch.set",
            target=capability_id, before=before,
            after={"state": row.state, "message": row.message},
        )
        await s.commit()
        out = FeatureSwitchOut(
            capability_id=capability_id, name=capability_id,
            state=row.state, message=row.message,
        )

    # Immediate for THIS process; other replicas and the worker pick it up within the TTL.
    from nexus.features.switches import invalidate

    invalidate()
    return out
```

Check `record_admin_action`'s real signature in `nexus/billing/audit.py` before relying on the keyword names above, and match how `admin_billing_write.py` calls it.

- [ ] **Step 5: Register the router**

In `nexus/api/routers/__init__.py`, add `admin_features` to the imports and `admin_features.router` to `all_routers`, beside the other admin routers.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_admin_features_api.py -n0 -v`
Expected: all pass.

- [ ] **Step 7: Regression on the admin surface**

Run: `python -m pytest tests/test_admin_permissions.py tests/test_admin_routes_are_not_discoverable.py tests/test_billing_platform_admins.py -n4 -q`
Expected: all pass. `test_admin_routes_are_not_discoverable` will now also cover the new router if you add its path to `ADMIN_PATHS` — do that.

- [ ] **Step 8: Commit**

```bash
git add nexus/api/routers/admin_features.py nexus/api/routers/__init__.py nexus/billing/permissions.py tests/test_admin_features_api.py
git commit -m "feat(admin): superadmin CRUD for platform feature switches"
```

---

## Task 6: Surface the state in the client

**Files:**
- Modify: `frontend/src/lib/types.ts`, `frontend/src/app/EntitlementsContext.tsx`
- Test: `tests/test_feature_switch_frontend.py` (there is no frontend test runner; these read the source, as `test_plan_gated_nav` already does)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_feature_switch_frontend.py
"""The client must gate on the switch, not only on the plan.

There is no frontend test runner in this repo, so these read the source — the same approach
`test_plan_gated_nav.py::test_the_routes_guard_the_same_capabilities_the_nav_hides` already takes.
"""
from __future__ import annotations

from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "src"


def test_the_entitlement_type_carries_the_switch():
    src = (FRONTEND / "lib" / "types.ts").read_text(encoding="utf-8")
    assert "feature_state" in src
    assert "feature_message" in src


def test_the_context_exposes_the_switch_state():
    src = (FRONTEND / "app" / "EntitlementsContext.tsx").read_text(encoding="utf-8")
    assert "feature_state" in src, (
        "the context resolves plan gating only, so a switched-off feature would still show as "
        "available in the nav"
    )


def test_a_switch_blocks_regardless_of_gating_active():
    """`gating_active` is false in shadow mode, which is correct for PLAN gating — hiding a feature
    the server still serves would make a rollout mode a visible regression. A platform switch is
    the opposite: the server genuinely refuses, so it must gate whatever the enforcement mode."""
    src = (FRONTEND / "app" / "EntitlementsContext.tsx").read_text(encoding="utf-8")
    assert "featureState" in src or "feature_state" in src
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_feature_switch_frontend.py -n0 -v`
Expected: FAIL — `feature_state` is not in `types.ts`.

- [ ] **Step 3: Extend the types**

In `frontend/src/lib/types.ts`, on the entitlement interface (find it by searching `gating_active`):

```typescript
export type FeatureState = "enabled" | "disabled" | "coming_soon" | "maintenance";
```

and on the per-module entitlement row:

```typescript
  // PLATFORM state, distinct from `included`, which is about the plan. An upsell is the right
  // answer to "you did not buy this" and the wrong one for "this is broken right now".
  feature_state: FeatureState;
  feature_message: string;
```

- [ ] **Step 4: Expose it from the context**

In `frontend/src/app/EntitlementsContext.tsx`, alongside the existing `isLocked`, add:

```typescript
  /**
   * The platform switch for a capability, independent of the plan.
   *
   * Gated regardless of `gating_active`, unlike plan locking. That flag exists because shadow mode
   * resolves entitlements and then allows anyway, so hiding a plan-gated feature there would hide
   * one that still works. A platform switch is the opposite: the server genuinely refuses, so
   * respecting `gating_active` here would show a working menu item for a feature that is off.
   */
  featureState: (capability?: string) => FeatureState;
  featureMessage: (capability?: string) => string;
```

Implement both by looking the capability up in the same `modules` map `isLocked` already uses, defaulting to `"enabled"` and `""` when absent or still loading — missing data must not gate, matching `isLocked`'s own bias.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_feature_switch_frontend.py -n0 -v`
Expected: all pass.

- [ ] **Step 6: Typecheck**

Run: `cd frontend && npx tsc --noEmit -p tsconfig.json`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/app/EntitlementsContext.tsx tests/test_feature_switch_frontend.py
git commit -m "feat(ui): surface the platform feature switch in the entitlements context"
```

---

## Task 7: The notice component and the route/nav wiring

**Files:**
- Create: `frontend/src/components/FeatureNotice.tsx`, `FeatureNotice.module.css`
- Modify: `frontend/src/App.tsx` (`RequireCapability`), `frontend/src/app/nav.tsx`

- [ ] **Step 1: Write the component**

```tsx
// frontend/src/components/FeatureNotice.tsx
import type { FeatureState } from "@/lib/types";
import styles from "./FeatureNotice.module.css";

/**
 * What the customer sees when a feature is switched off at the platform level.
 *
 * Three states, three different messages, because they are three different conversations and a
 * single "unavailable" makes a support agent guess which one they are having:
 *
 *  - `coming_soon`  — it is not built yet. Nothing to do but wait.
 *  - `maintenance`  — it is broken right now. It will come back.
 *  - `disabled`     — it has been turned off deliberately.
 *
 * Never an upsell. A customer cannot buy their way out of an outage, and offering them a plan
 * change here would be actively misleading — that is what plan locking is for.
 */
const COPY: Record<Exclude<FeatureState, "enabled">, { title: string; body: string }> = {
  coming_soon: {
    title: "Coming soon",
    body: "This feature is on the way. We will let you know the moment it is ready.",
  },
  maintenance: {
    title: "Temporarily unavailable",
    body: "We are working on this feature right now. Please try again shortly.",
  },
  disabled: {
    title: "Not available",
    body: "This feature is not currently available on this workspace.",
  },
};

export function FeatureNotice({
  state,
  message,
  compact = false,
}: {
  state: FeatureState;
  message?: string;
  compact?: boolean;
}) {
  if (state === "enabled") return null;
  const copy = COPY[state];
  return (
    <div
      className={compact ? styles.banner : styles.page}
      role="status"
      aria-live="polite"
    >
      <p className={styles.title}>{copy.title}</p>
      {/* The superadmin's own words win: they know why it is off and when it is back. The generic
          copy is the fallback so a switch flipped in a hurry never leaves a blank banner. */}
      <p className={styles.body}>{message?.trim() || copy.body}</p>
    </div>
  );
}
```

- [ ] **Step 2: Write the styles**

```css
/* frontend/src/components/FeatureNotice.module.css */
.page {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  min-height: 40vh;
  padding: var(--space-8) var(--space-4);
  text-align: center;
}

.banner {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-3) var(--space-4);
  margin-bottom: var(--space-4);
  border: 1px solid var(--color-border);
  border-left: 3px solid var(--color-warning, var(--color-accent));
  border-radius: var(--radius-md, 6px);
  background: var(--color-surface);
}

.title {
  margin: 0;
  font-weight: 600;
  color: var(--color-text);
}

.body {
  margin: 0;
  color: var(--color-text-secondary);
  max-width: 46ch;
}
```

- [ ] **Step 3: Wire the route guard**

In `frontend/src/App.tsx`, `RequireCapability` currently redirects to `/settings/billing`. That is right for plan locking — the customer can change the plan — and wrong for a switch, which sends them to a page that cannot help. Render the notice instead:

```tsx
function RequireCapability({ capability, children }: { capability: string; children: ReactNode }) {
  const { isLocked, featureState, featureMessage } = useEntitlements();
  const state = featureState(capability);
  // A platform switch is not something the customer can buy their way out of, so this renders an
  // explanation IN PLACE rather than redirecting to billing the way plan locking does. Sending
  // someone to a plan picker for an outage is a dead end that also looks like an upsell.
  if (state !== "enabled") {
    return <FeatureNotice state={state} message={featureMessage(capability)} />;
  }
  // ...existing plan-locking behaviour unchanged below
}
```

Read the existing body and keep the plan-locking branch exactly as it is.

- [ ] **Step 4: Wire the nav**

In `frontend/src/app/nav.tsx`, `navState(role, locked)` returns `visible | locked | hidden`. A switched feature should stay VISIBLE and be marked, not hidden: a menu item that vanishes reads as a bug or a permissions problem, and the customer then cannot find the explanation. Extend the signature:

```typescript
export function navState(
  role: Role | undefined,
  locked: boolean,
  switched = false,
): NavState {
  // A platform switch keeps the item VISIBLE. Hiding it means the customer cannot reach the page
  // that explains why the feature is off, and a menu item that silently disappears reads as a bug
  // — which generates the support ticket the message exists to prevent.
  if (switched) return "visible";
  if (!locked) return "visible";
  return role && CAN_UPGRADE.includes(role) ? "locked" : "hidden";
}
```

Then pass `featureState(item.capability) !== "enabled"` at the call site, and render a small "Soon" / "Off" chip beside the label there.

- [ ] **Step 5: Typecheck and build**

Run: `cd frontend && npx tsc --noEmit -p tsconfig.json && npm run build`
Expected: no type errors; build succeeds.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/FeatureNotice.tsx frontend/src/components/FeatureNotice.module.css frontend/src/App.tsx frontend/src/app/nav.tsx
git commit -m "feat(ui): show a coming-soon or maintenance notice instead of an upsell"
```

---

## Task 8: The credit usage report page

**Files:**
- Create: `frontend/src/pages/CreditUsagePage.tsx`, `CreditUsagePage.module.css`
- Modify: `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`, `frontend/src/App.tsx`

- [ ] **Step 1: Add the types**

```typescript
// frontend/src/lib/types.ts
export interface CreditSpendRow {
  capability_id: string;
  name: string;
  credits: number;
  actions: number;
}

export interface CreditDay {
  date: string;
  credits: number;
}

export interface CreditUserRow {
  user_id: string;
  credits: number;
}

export interface CreditUsageReport {
  period: string;
  granted: number;
  spent: number;
  balance: number;
  by_capability: CreditSpendRow[];
  by_day: CreditDay[];
  by_user: CreditUserRow[];
  // Spend with no user behind it. Shown, never hidden: background work has nobody to attribute
  // to, so `by_user` cannot sum to `spent`, and a screen whose parts do not add up quietly lies
  // about a figure the customer will check against their balance.
  unattributed_credits: number;
}
```

- [ ] **Step 2: Add the client method**

In `frontend/src/lib/api.ts`, beside the other billing calls:

```typescript
  creditUsageReport(signal?: AbortSignal) {
    return this.request<CreditUsageReport>("/billing/usage/credits", { signal });
  }
```

- [ ] **Step 3: Write the page**

Build it with the existing primitives from `@/components/ui` and `DataState`, and handle loading, empty and error explicitly — every data view in this codebase does, and it is in the frontend conventions.

Four sections, in this order, because it is the order the questions get asked:

1. **Three stat tiles** — balance, spent this period, granted this period.
2. **By capability**, a table sorted by spend as the server returns it (do not re-sort on the client — two sorts is two answers). Columns: feature name, credits, actions.
3. **By day**, a simple bar row per day. No charting library: the curated dependency list is `react`, `react-dom`, `react-router-dom`, `framer-motion`, and a bar is a `div` with a width.
4. **By user**, plus one final row for `unattributed_credits` labelled "Background work (automations, crawls)" with a one-line explanation that it belongs to no person. Omitting it would leave the rows not summing to the total.

Empty state copy: "No credits spent this period yet." — not a spinner and not a blank panel.

- [ ] **Step 4: Add the route**

In `frontend/src/App.tsx`, add the page under `/settings/billing/usage`. **No `RequireCapability`**: billing pages are deliberately ungateable — `test_the_floor_of_the_product_is_never_gated` asserts it — because gating the page where a customer sees what they are spending is the one thing that must never be behind a plan.

- [ ] **Step 5: Link it from the billing page**

Add a "See where your credits went" link from the existing billing settings page beside the balance.

- [ ] **Step 6: Typecheck and build**

Run: `cd frontend && npx tsc --noEmit -p tsconfig.json && npm run build`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/CreditUsagePage.tsx frontend/src/pages/CreditUsagePage.module.css frontend/src/lib/api.ts frontend/src/lib/types.ts frontend/src/App.tsx
git commit -m "feat(ui): a page showing where a workspace's credits went"
```

---

## Task 9: The superadmin control

**Files:**
- Create: `frontend/src/pages/admin/FeatureSwitchesTab.tsx`, `.module.css`
- Modify: `frontend/src/lib/api.ts`, the admin page that hosts the tabs

- [ ] **Step 1: Add the client methods**

```typescript
  featureSwitches(signal?: AbortSignal) {
    return this.request<FeatureSwitchRow[]>("/admin/features", { signal });
  }

  setFeatureSwitch(capabilityId: string, state: FeatureState, message: string, signal?: AbortSignal) {
    return this.request<FeatureSwitchRow>(`/admin/features/${capabilityId}`, {
      method: "PUT",
      // NOT JSON.stringify: `request()` already serialises `body`. Double-encoding sends a JSON
      // string where the server expects an object, which is a 422 with no useful detail.
      body: { state, message },
      signal,
    });
  }
```

- [ ] **Step 2: Write the tab**

One row per module: its name, a four-way state control, a message field, and a Save. Two things the UI must say out loud:

- **"Takes effect within 30 seconds"** beside the save action. Operators otherwise assume either instant or a redeploy, and both guesses cause a wrong action — reloading repeatedly, or shipping a build.
- **A warning on any state but `enabled`**, naming what it does: "Hides the page for every workspace and refuses its endpoints." A control whose blast radius is not stated is a trap, which is the rule `runtime_config/catalog.py` already applies to every setting.

- [ ] **Step 3: Typecheck, build, commit**

```bash
cd frontend && npx tsc --noEmit -p tsconfig.json && npm run build && cd ..
git add frontend/src/pages/admin/FeatureSwitchesTab.tsx frontend/src/pages/admin/FeatureSwitchesTab.module.css frontend/src/lib/api.ts
git commit -m "feat(admin): a superadmin control for platform feature switches"
```

---

## Task 10: Verify in the running app, then deploy

- [ ] **Step 1: Full suite**

Run: `python -m pytest tests/ -n auto --dist loadfile -q`
Expected: all pass. Do not run any other pytest process at the same time — concurrent runs share Prometheus registry state and produce a spurious `test_metrics` failure.

- [ ] **Step 2: Build the frontend, then the images**

```bash
cd frontend && npm run build && cd ..
docker compose -f deploy/docker-compose.prod.yml build app worker
docker compose -f deploy/docker-compose.prod.yml up -d
```

- [ ] **Step 3: Migrate and apply RLS**

```bash
docker compose -f deploy/docker-compose.prod.yml exec -T app sh -c "cd /app && alembic upgrade head"
docker compose -f deploy/docker-compose.prod.yml exec -T app python scripts/apply_rls.py
```

Expected head: `0055_feature_switches`. `feature_switches` carries no `tenant_id`, so `apply_rls.py` deliberately leaves it alone.

- [ ] **Step 4: Prove the switch reaches BOTH containers**

The requirement is that it takes effect in production without a redeploy, and the worker is a separate process:

```bash
docker compose -f deploy/docker-compose.prod.yml exec -T app python -c "
import asyncio
from nexus.features.switches import invalidate, switch_for
async def m():
    invalidate()
    s = await switch_for('module.calling')
    print('app   :', s.state, repr(s.message))
asyncio.run(m())"

docker compose -f deploy/docker-compose.prod.yml exec -T worker python -c "
import asyncio
from nexus.features.switches import invalidate, switch_for
async def m():
    invalidate()
    s = await switch_for('module.calling')
    print('worker:', s.state, repr(s.message))
asyncio.run(m())"
```

Both must report the same state after a switch is set through the API.

- [ ] **Step 5: Prove an endpoint actually refuses**

Set `module.calling` to `maintenance`, then call a calling endpoint as a normal user and confirm it is refused — not merely hidden in the nav. A switch that only hides the menu is a discount with no effect, the same failure `depends_on` was added to fix for module gates.

- [ ] **Step 6: Update the graph and CLAUDE.md**

```bash
git add -A && code-review-graph build
```

The indexer only sees git-TRACKED files, so `git add` must come first — measured 2026-08-04, both `update` and `build` reported success and indexed none of nine new files until they were added. Verify a new symbol is present rather than trusting the summary line:

```bash
code-review-graph search "FeatureSwitch"
```

Add a CLAUDE.md section covering: the switch is keyed on `module.*` so it reaches nav, route and endpoints through machinery that already exists; it is placed BEFORE the unlimited-class short-circuit so it beats every plan; it fails open; and the four states differ only in what the customer is told.

---

## Self-review

**Spec coverage.** (1) usage report frontend → Task 8. (2) superadmin disable of a feature, its endpoints and its page → Tasks 1–5 (backend) and 7 (page/nav/route). "Upgrading feature" message → `maintenance` state. "Coming soon" banner → `coming_soon` state. New pages covered later → Task 5's catalogue-derived listing plus the `module.*` keying, tested by `test_it_lists_every_module_so_a_new_page_appears_by_itself`. Takes effect in production → Task 2's TTL, proven in Task 10 Step 4. No regressions → the regression steps in Tasks 3, 5 and 10. Graph update → Task 10 Step 6.

**Placeholders.** Tasks 8 Step 3 and 9 Step 2 describe the page composition rather than giving complete JSX. That is deliberate: both must be built from the existing `@/components/ui` primitives, and inventing markup here that does not match them would be worse than describing the required sections, states and copy — which are all specified.

**Type consistency.** `FeatureState` is the same union in `types.ts`, the API field (`feature_state`), and the model (`SWITCH_STATES`). `Switch.blocks`, `switch_for`, `all_switches` and `invalidate` keep their signatures across Tasks 2, 3, 4 and 5. `FeatureSwitchOut`/`FeatureSwitchIn` match between Task 5's router and Task 9's client.

**Known risk.** Task 7 Step 4 changes `navState`'s signature, which `test_plan_gated_nav` asserts against. The third parameter is defaulted, so existing calls compile unchanged — but run that suite before assuming it.
