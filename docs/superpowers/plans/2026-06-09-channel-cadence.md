# Channel & Cadence (Multi-touch Email Cadences) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make NEXUS the native owner of multi-touch email cadences over the existing Segment Campaign Engine: an enrolled campaign target receives several AI-composed touches spaced over days, stopping on reply / bounce / manual control / duration cap, with approve-once-then-auto-run plus an opt-in per-touch review gate.

**Architecture:** DB is the source of truth. Each `CadenceEnrollment` carries `next_touch_at` + a composite index `(status, next_touch_at)`. A periodic **advance tick** (`handle_advance_cadences`) claims a bounded batch of *due* enrollments and, per enrollment inside `tenant_session`, runs: stop-check → per-step AI compose (angle threaded into `research_compose`) → idempotent `CadenceTouch` insert → send-policy + grounded gate → send or park → advance. Time flows through an explicit injectable `now` parameter, so a multi-week cadence is exercised in tests with zero `sleep`. A NULL `Campaign.cadence_id` keeps the existing single-touch send path unchanged.

**Tech Stack:** Python 3.10 (`from __future__ import annotations`), async SQLAlchemy 2.0 (`Mapped`/`mapped_column`, `Index`, `UniqueConstraint`), Pydantic v2 + pydantic-settings (`NEXUS_` env prefix), FastAPI, Alembic, pytest (`asyncio_mode=auto`). Multi-tenant via `TenantSession` + Postgres RLS; RBAC reuses `Permission.manage_campaigns`. Offline: SQLite + stub LLM + in-memory queue, zero network.

**Conventions for every task:**
- Run the full test suite with `python -m pytest -q` from the repo root (`C:\Users\Amit Singh\Projects\nexus`).
- Commit only source/test/docs. NEVER `git add -A` / `git add .`. Stage named files. No `Co-Authored-By` trailer. Never amend; always a new commit. The "LF will be replaced by CRLF" git warning is harmless.
- Models become real tables in tests via `Base.metadata.create_all` (see `tests/conftest.py`), so registering a model in `nexus/models/__init__.py` is what makes its table exist for tests. The Alembic migration (Task 4) is verified separately.

---

### Task 1: Cadence configuration knobs

**Files:**
- Modify: `nexus/core/config.py`
- Test: `tests/test_cadence_engine.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_cadence_engine.py` with:

```python
"""Offline tests for the Channel & Cadence engine (sub-project C). Zero-network."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from nexus.core.config import get_settings
from tests.conftest import make_tenant, tenant_session


def test_cadence_config_defaults():
    s = get_settings()
    assert s.cadence_enabled is False
    assert s.cadence_tick_interval_s == 60
    assert s.cadence_batch_size == 100
    assert s.cadence_max_duration_days == 30
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cadence_engine.py::test_cadence_config_defaults -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'cadence_enabled'`

- [ ] **Step 3: Add the settings**

In `nexus/core/config.py`, immediately after the `crm_provider` line (currently line 73, inside the contact-sourcing block), add:

```python
    # Channel & Cadence (sub-project C): multi-touch email cadence engine. Disabled by
    # default (safe opt-in, like campaign_sourcing) so the advance tick is a no-op until a
    # deployment turns it on with one env line.
    cadence_enabled: bool = False             # master switch for the advance tick
    cadence_tick_interval_s: int = 60         # production due-scan cadence (seconds)
    cadence_batch_size: int = 100             # max enrollments claimed per tick per worker
    cadence_max_duration_days: int = 30       # duration-cap safety bound (mid-sequence stop)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cadence_engine.py::test_cadence_config_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/core/config.py tests/test_cadence_engine.py
git commit -m "feat(cadence): add cadence engine config knobs"
```

---

### Task 2: Cadence ORM models + constants

**Files:**
- Create: `nexus/models/cadence.py`
- Modify: `nexus/models/__init__.py`
- Test: `tests/test_cadence_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cadence_engine.py`:

```python
from nexus.models.cadence import (
    Cadence,
    CadenceStep,
    CadenceEnrollment,
    CadenceTouch,
    ENROLL_ACTIVE,
    ENROLL_TERMINAL,
    ENROLL_COMPLETED,
    ENROLL_STOPPED,
    STOP_REPLIED,
    TOUCH_SENT,
    TOUCH_AWAITING_APPROVAL,
)


async def test_cadence_models_and_uniques():
    tid = await make_tenant(slug="cad-models", name="Cad Models")
    async with tenant_session(tid) as ts:
        cad = Cadence(tenant_id=tid, name="3-touch")
        ts.add(cad)
        await ts.flush()
        assert cad.is_active is True

        ts.add(CadenceStep(tenant_id=tid, cadence_id=cad.id, step_index=0,
                           delay_days=0, angle="intro"))
        await ts.flush()
        # Duplicate (cadence_id, step_index) must violate the unique constraint.
        ts.add(CadenceStep(tenant_id=tid, cadence_id=cad.id, step_index=0,
                           delay_days=2, angle="dupe"))
        with pytest.raises(Exception):
            await ts.flush()


def test_enroll_terminal_set():
    assert ENROLL_TERMINAL == frozenset({ENROLL_COMPLETED, ENROLL_STOPPED})
    assert ENROLL_ACTIVE == "active"
    assert STOP_REPLIED == "replied"
    assert TOUCH_SENT == "sent"
    assert TOUCH_AWAITING_APPROVAL == "awaiting_approval"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cadence_engine.py::test_enroll_terminal_set -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.models.cadence'`

- [ ] **Step 3: Create the models**

Create `nexus/models/cadence.py`:

```python
"""Channel & Cadence: NEXUS-native multi-touch email cadences.

A :class:`Cadence` is a reusable, ordered list of :class:`CadenceStep` rows (each a delay +
an AI compose ``angle``). A :class:`CadenceEnrollment` puts one campaign target through a
cadence over time; the advance tick fires one :class:`CadenceTouch` per step. All tables are
tenant-scoped (RLS). Email-only in v1 (``channel`` is guarded to ``"email"`` in the service).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped

# Enrollment lifecycle.
ENROLL_ACTIVE = "active"
ENROLL_PAUSED = "paused"
ENROLL_COMPLETED = "completed"   # steps exhausted, natural finish
ENROLL_STOPPED = "stopped"       # halted early (see stop_reason)
ENROLL_TERMINAL = frozenset({ENROLL_COMPLETED, ENROLL_STOPPED})

# Why an enrollment stopped early.
STOP_REPLIED = "replied"
STOP_UNDELIVERABLE = "undeliverable"
STOP_MANUAL = "manual"
STOP_MAX_TOUCHES = "max_touches"   # duration cap exceeded

# Per-touch outcome.
TOUCH_SENT = "sent"
TOUCH_SKIPPED = "skipped"
TOUCH_FAILED = "failed"
TOUCH_AWAITING_APPROVAL = "awaiting_approval"


class Cadence(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "cadences"
    __table_args__ = (Index("ix_cadence_tenant", "tenant_id"),)

    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Soft-disable: off keeps the definition but blocks new enrollments.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class CadenceStep(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "cadence_steps"
    __table_args__ = (
        UniqueConstraint("cadence_id", "step_index", name="uq_cadence_step_index"),
    )

    cadence_id: Mapped[str] = mapped_column(ForeignKey("cadences.id"), index=True)
    step_index: Mapped[int] = mapped_column(Integer)          # 0-based, contiguous
    delay_days: Mapped[int] = mapped_column(Integer, default=0)  # wait before this step fires
    angle: Mapped[str] = mapped_column(Text, default="")     # per-touch compose angle
    channel: Mapped[str] = mapped_column(String(16), default="email")  # v1: email only


class CadenceEnrollment(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "cadence_enrollments"
    __table_args__ = (
        # The claim query's index: WHERE status=active AND next_touch_at <= now.
        Index("ix_enrollment_status_due", "status", "next_touch_at"),
        Index("ix_enrollment_campaign", "campaign_id"),
    )

    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    campaign_target_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaign_targets.id"), nullable=True
    )
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    cadence_id: Mapped[str] = mapped_column(ForeignKey("cadences.id"), index=True)
    current_step_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=ENROLL_ACTIVE, index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    next_touch_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CadenceTouch(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "cadence_touches"
    __table_args__ = (
        # Structural idempotency: a step is touched exactly once per enrollment.
        UniqueConstraint("enrollment_id", "step_index", name="uq_touch_enrollment_step"),
    )

    enrollment_id: Mapped[str] = mapped_column(
        ForeignKey("cadence_enrollments.id"), index=True
    )
    step_index: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    skip_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    draft: Mapped[dict] = mapped_column(JSON, default=dict)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Register the models**

In `nexus/models/__init__.py`, add the import after the `campaign` import line (currently line 8):

```python
from nexus.models.cadence import (
    Cadence,
    CadenceStep,
    CadenceEnrollment,
    CadenceTouch,
)
```

And add these entries to the `__all__` list (after `"CampaignTarget",`):

```python
    "Cadence",
    "CadenceStep",
    "CadenceEnrollment",
    "CadenceTouch",
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_cadence_engine.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add nexus/models/cadence.py nexus/models/__init__.py tests/test_cadence_engine.py
git commit -m "feat(cadence): add Cadence/Step/Enrollment/Touch models + constants"
```

---

### Task 3: Campaign cadence columns

**Files:**
- Modify: `nexus/models/campaign.py`
- Test: `tests/test_cadence_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cadence_engine.py`:

```python
from nexus.models.campaign import Campaign


async def test_campaign_cadence_columns_default():
    tid = await make_tenant(slug="cad-camp", name="Cad Camp")
    async with tenant_session(tid) as ts:
        c = Campaign(tenant_id=tid, name="cad", list_id="l1")
        ts.add(c)
        await ts.flush()
        assert c.cadence_id is None          # NULL = backward-compatible single-touch path
        assert c.review_each_touch is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cadence_engine.py::test_campaign_cadence_columns_default -v`
Expected: FAIL with `AttributeError: 'Campaign' object has no attribute 'cadence_id'` (or a Pydantic/SQLAlchemy unknown-attribute error)

- [ ] **Step 3: Add the columns**

In `nexus/models/campaign.py`, in the `Campaign` class, after the `send_risky` column (currently line 58), add:

```python
    # Channel & Cadence (sub-project C). NULL cadence_id = the existing single-touch send
    # path (fully backward-compatible). review_each_touch opts into a per-touch manual gate.
    cadence_id: Mapped[str | None] = mapped_column(
        ForeignKey("cadences.id"), nullable=True
    )
    review_each_touch: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
```

(`Boolean`, `ForeignKey`, `Mapped`, `mapped_column` are already imported in this file.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cadence_engine.py::test_campaign_cadence_columns_default -v`
Expected: PASS

- [ ] **Step 5: Run the existing campaign suite to confirm no regression**

Run: `python -m pytest tests/test_campaign_engine.py -q`
Expected: PASS (all existing tests green)

- [ ] **Step 6: Commit**

```bash
git add nexus/models/campaign.py tests/test_cadence_engine.py
git commit -m "feat(cadence): add Campaign.cadence_id + review_each_touch columns"
```

---

### Task 4: Alembic migration 0007

**Files:**
- Create: `migrations/versions/0007_cadence.py`

- [ ] **Step 1: Create the migration**

Create `migrations/versions/0007_cadence.py`:

```python
"""Channel & Cadence: cadence tables + campaign cadence columns.

Revision ID: 0007_cadence
Revises: 0006_contact_sourcing
Create Date: 2026-06-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_cadence"
down_revision = "0006_contact_sourcing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cadences",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.text("1")),
        sa.Column("created_by_user_id", sa.String(length=32),
                  sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_cadence_tenant", "cadences", ["tenant_id"])

    op.create_table(
        "cadence_steps",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("cadence_id", sa.String(length=32),
                  sa.ForeignKey("cadences.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("delay_days", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("angle", sa.Text(), nullable=False, server_default=""),
        sa.Column("channel", sa.String(length=16), nullable=False, server_default="email"),
        sa.UniqueConstraint("cadence_id", "step_index", name="uq_cadence_step_index"),
    )
    op.create_index("ix_cadence_steps_tenant_id", "cadence_steps", ["tenant_id"])
    op.create_index("ix_cadence_steps_cadence_id", "cadence_steps", ["cadence_id"])

    op.create_table(
        "cadence_enrollments",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("campaign_id", sa.String(length=32),
                  sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("campaign_target_id", sa.String(length=32),
                  sa.ForeignKey("campaign_targets.id"), nullable=True),
        sa.Column("account_id", sa.String(length=32),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("contact_id", sa.String(length=32),
                  sa.ForeignKey("contacts.id"), nullable=True),
        sa.Column("cadence_id", sa.String(length=32),
                  sa.ForeignKey("cadences.id"), nullable=False),
        sa.Column("current_step_index", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default="active"),
        sa.Column("stop_reason", sa.String(length=16), nullable=True),
        sa.Column("next_touch_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_cadence_enrollments_tenant_id", "cadence_enrollments", ["tenant_id"])
    op.create_index("ix_cadence_enrollments_account_id", "cadence_enrollments", ["account_id"])
    op.create_index("ix_cadence_enrollments_cadence_id", "cadence_enrollments", ["cadence_id"])
    op.create_index("ix_cadence_enrollments_status", "cadence_enrollments", ["status"])
    op.create_index("ix_enrollment_campaign", "cadence_enrollments", ["campaign_id"])
    op.create_index(
        "ix_enrollment_status_due", "cadence_enrollments", ["status", "next_touch_at"]
    )

    op.create_table(
        "cadence_touches",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("enrollment_id", sa.String(length=32),
                  sa.ForeignKey("cadence_enrollments.id"), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("skip_reason", sa.String(length=40), nullable=True),
        sa.Column("draft", sa.JSON(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.UniqueConstraint("enrollment_id", "step_index", name="uq_touch_enrollment_step"),
    )
    op.create_index("ix_cadence_touches_tenant_id", "cadence_touches", ["tenant_id"])
    op.create_index("ix_cadence_touches_enrollment_id", "cadence_touches", ["enrollment_id"])

    with op.batch_alter_table("campaigns") as batch:
        batch.add_column(
            sa.Column("cadence_id", sa.String(length=32), nullable=True)
        )
        batch.add_column(
            sa.Column("review_each_touch", sa.Boolean(), nullable=False,
                      server_default=sa.text("0"))
        )


def downgrade() -> None:
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_column("review_each_touch")
        batch.drop_column("cadence_id")
    op.drop_index("ix_cadence_touches_enrollment_id", table_name="cadence_touches")
    op.drop_index("ix_cadence_touches_tenant_id", table_name="cadence_touches")
    op.drop_table("cadence_touches")
    op.drop_index("ix_enrollment_status_due", table_name="cadence_enrollments")
    op.drop_index("ix_enrollment_campaign", table_name="cadence_enrollments")
    op.drop_index("ix_cadence_enrollments_status", table_name="cadence_enrollments")
    op.drop_index("ix_cadence_enrollments_cadence_id", table_name="cadence_enrollments")
    op.drop_index("ix_cadence_enrollments_account_id", table_name="cadence_enrollments")
    op.drop_index("ix_cadence_enrollments_tenant_id", table_name="cadence_enrollments")
    op.drop_table("cadence_enrollments")
    op.drop_index("ix_cadence_steps_cadence_id", table_name="cadence_steps")
    op.drop_index("ix_cadence_steps_tenant_id", table_name="cadence_steps")
    op.drop_table("cadence_steps")
    op.drop_index("ix_cadence_tenant", table_name="cadences")
    op.drop_table("cadences")
```

> **Note on the `campaigns.cadence_id` FK:** the ORM model (Task 3) declares a `ForeignKey("cadences.id")`, but this migration adds the column *without* the named FK constraint. That is deliberate and matches SQLite's limitation under `batch_alter_table` (adding a column with an inline FK to an existing table is awkward on SQLite). The application-level integrity is enforced by the service; the column type/nullability match the model. This mirrors how `0006` added `send_risky` as a plain column.

- [ ] **Step 2: Verify the migration applies cleanly on a throwaway DB**

Run (PowerShell, from repo root):

```powershell
$env:NEXUS_DATABASE_URL = "sqlite+aiosqlite:///./_mig_check.db"; alembic upgrade head
```

Expected: output ends with `Running upgrade 0006_contact_sourcing -> 0007_cadence`.

- [ ] **Step 3: Verify downgrade works, then clean up**

```powershell
alembic downgrade -1
Remove-Item ./_mig_check.db -ErrorAction SilentlyContinue
```

Expected: `Running downgrade 0007_cadence -> 0006_contact_sourcing` with no error.

- [ ] **Step 4: Commit**

```bash
git add migrations/versions/0007_cadence.py
git commit -m "feat(cadence): alembic 0007 cadence tables + campaign columns"
```

---

### Task 5: Thread the per-touch `angle` through compose

The cadence composes each touch by running the existing `research_compose` recipe with the
step's `angle` threaded into the messaging agent (same inputs-threading pattern sub-project B
used for `contact_id`). This task wires `angle` end-to-end; cadence tests in later tasks rely
on it.

**Files:**
- Modify: `nexus/orchestration/planner.py:86-100` (`_research_compose_plan`)
- Modify: `nexus/agents/messaging.py`
- Test: `tests/test_cadence_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cadence_engine.py`:

```python
from nexus.orchestration.planner import get_planner


def test_research_compose_threads_angle():
    plan = get_planner().plan(
        "research_compose", {"account_id": "a1", "angle": "case study follow-up"}
    )
    compose = next(s for s in plan if s["tool"] == "compose_message")
    assert compose["inputs"].get("angle") == "case study follow-up"


def test_research_compose_without_angle_unchanged():
    plan = get_planner().plan("research_compose", {"account_id": "a1"})
    compose = next(s for s in plan if s["tool"] == "compose_message")
    assert "angle" not in compose["inputs"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cadence_engine.py::test_research_compose_threads_angle -v`
Expected: FAIL with `AssertionError` (`angle` not in compose inputs)

- [ ] **Step 3: Thread angle in the planner**

In `nexus/orchestration/planner.py`, in `_research_compose_plan`, after the `contact_id`
block (currently lines 94-95), add the `angle` block so the function reads:

```python
    compose_inputs: dict = {}
    if goal_input.get("contact_id"):
        compose_inputs["contact_id"] = goal_input["contact_id"]
    # Channel & Cadence: thread the per-touch angle so each cadence step composes a
    # distinct message. Same inputs-threading seam B used for contact_id.
    if goal_input.get("angle"):
        compose_inputs["angle"] = goal_input["angle"]
    return [
        PlanStep(idx=0, tool="research", depends_on=[]),
        PlanStep(idx=1, tool="scoring", depends_on=[0]),
        PlanStep(idx=2, tool="compose_message", inputs=compose_inputs, depends_on=[1]),
    ]
```

- [ ] **Step 4: Consume angle in the messaging agent**

In `nexus/agents/messaging.py`, inside `MessagingAgent.run`, replace the construction of the
`user` message (currently lines 37-44) with:

```python
        angle = (ctx.inputs.get("angle") or "").strip()
        content = (
            f"Write a short, personalized cold email to "
            f"{contact.full_name if contact else 'the buyer'} at {ctx.account.name}. "
            f"Hook: {trigger}. Lead with value prop '{vp.get('name')}'."
        )
        if angle:
            # Per-touch cadence angle: shape this specific touch (e.g. a follow-up nudge,
            # a case-study share) so successive touches don't repeat the same message.
            content += f" Angle for this touch: {angle}."
        user = LLMMessage(role="user", content=content)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_cadence_engine.py -v`
Expected: PASS (the two new tests + earlier ones)

- [ ] **Step 6: Confirm no regression in agent/orchestration tests**

Run: `python -m pytest tests/test_campaign_engine.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add nexus/orchestration/planner.py nexus/agents/messaging.py tests/test_cadence_engine.py
git commit -m "feat(cadence): thread per-touch angle through research_compose"
```

---

### Task 6: CadenceService — create_cadence, list_steps, enroll

This task creates the cadence package and the first slice of the service: defining a cadence
and enrolling a campaign target. The advance/touch machinery comes in Task 7.

**Files:**
- Create: `nexus/cadences/__init__.py`
- Create: `nexus/cadences/service.py`
- Test: `tests/test_cadence_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cadence_engine.py`:

```python
from nexus.cadences.service import CadenceError, get_cadence_service
from nexus.models.account import Account
from nexus.models.campaign import Campaign, CampaignTarget, TARGET_DRAFTED


async def _make_cadence(ts, steps):
    return await get_cadence_service().create_cadence(
        ts, name="seq", description=None, steps=steps, created_by_user_id=None
    )


async def test_create_cadence_validates_and_orders():
    tid = await make_tenant(slug="cad-create", name="Cad Create")
    async with tenant_session(tid) as ts:
        cad = await _make_cadence(ts, [
            {"delay_days": 0, "angle": "intro"},
            {"delay_days": 3, "angle": "nudge"},
        ])
        steps = await get_cadence_service().list_steps(ts, cad.id)
        assert [s.step_index for s in steps] == [0, 1]
        assert steps[0].delay_days == 0 and steps[1].delay_days == 3

        with pytest.raises(CadenceError):
            await _make_cadence(ts, [])  # no steps
        with pytest.raises(CadenceError):
            await _make_cadence(ts, [{"delay_days": 0, "channel": "sms"}])  # v1 email-only
        with pytest.raises(CadenceError):
            await _make_cadence(ts, [{"delay_days": -1}])  # negative delay


async def test_enroll_sets_first_due():
    tid = await make_tenant(slug="cad-enroll", name="Cad Enroll")
    async with tenant_session(tid) as ts:
        cad = await _make_cadence(ts, [{"delay_days": 2, "angle": "intro"}])
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        camp = Campaign(tenant_id=tid, name="c", list_id="l1", cadence_id=cad.id)
        ts.add(camp)
        await ts.flush()
        target = CampaignTarget(tenant_id=tid, campaign_id=camp.id, account_id=acc.id,
                                status=TARGET_DRAFTED, draft={"contact_id": "ct1"})
        ts.add(target)
        await ts.flush()

        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        e = await get_cadence_service().enroll(ts, camp, target, now=t0)
        assert e.status == "active"
        assert e.current_step_index == 0
        assert e.account_id == acc.id
        assert e.contact_id == "ct1"
        assert e.started_at == t0
        assert e.next_touch_at == t0 + timedelta(days=2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cadence_engine.py::test_create_cadence_validates_and_orders -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.cadences'`

- [ ] **Step 3: Create the package init**

Create `nexus/cadences/__init__.py`:

```python
"""Channel & Cadence engine: multi-touch email cadences over the Campaign Engine."""
```

- [ ] **Step 4: Create the service (first slice)**

Create `nexus/cadences/service.py`:

```python
"""CadenceService: define cadences, enroll targets, and drive multi-touch sends over time.

The advance tick claims due enrollments and processes each inside ``tenant_session`` so every
compose/send read and write obeys RLS. Time is an explicit ``now`` parameter (injectable
clock): production passes wall-clock; tests pass a fake datetime and advance days via
``timedelta``, exercising a multi-week cadence with zero ``sleep``.

DRY reuse: per-touch compose reuses the ``research_compose`` recipe (with the step's ``angle``
threaded); the send reuses :class:`SendMessageTool` (grounded + verified hard gates) via the
run blackboard; the pre-send policy reuses :meth:`CampaignService._send_policy`; reply-stop and
``Outcome("sent")`` reuse :class:`OutcomeService`.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from nexus.agents.runtime import get_agent_runtime
from nexus.campaigns.service import CampaignService
from nexus.core.config import get_settings
from nexus.core.db import ensure_aware, utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.cadence import (
    Cadence,
    CadenceStep,
    CadenceEnrollment,
    CadenceTouch,
    ENROLL_ACTIVE,
    ENROLL_PAUSED,
    ENROLL_COMPLETED,
    ENROLL_STOPPED,
    ENROLL_TERMINAL,
    STOP_REPLIED,
    STOP_UNDELIVERABLE,
    STOP_MANUAL,
    STOP_MAX_TOUCHES,
    TOUCH_SENT,
    TOUCH_SKIPPED,
    TOUCH_FAILED,
    TOUCH_AWAITING_APPROVAL,
)
from nexus.models.campaign import (
    Campaign,
    SKIP_NO_CONTACT,
    SKIP_UNDELIVERABLE,
    SKIP_UNGROUNDED,
)
from nexus.models.orchestration import OrchestrationRun, RUN_COMPLETED
from nexus.models.outcome import Outcome
from nexus.orchestration.engine import get_orchestration_engine
from nexus.orchestration.tools import SendMessageTool, ToolContext, ToolError
from nexus.outcomes.service import get_outcome_service


class CadenceError(Exception):
    """Raised for an invalid cadence definition or control transition."""


class CadenceService:
    # ----- Definition ---------------------------------------------------------------
    async def create_cadence(
        self,
        ts: TenantSession,
        *,
        name: str,
        description: str | None,
        steps: list[dict],
        created_by_user_id: str | None,
    ) -> Cadence:
        """Create a cadence with ordered steps. Validates: >=1 step, channel=='email',
        delay_days >= 0. Step indices are assigned contiguously from 0 in list order."""
        if not steps:
            raise CadenceError("a cadence needs at least one step")
        cadence = Cadence(
            tenant_id=ts.tenant_id,
            name=name,
            description=description,
            created_by_user_id=created_by_user_id,
        )
        ts.add(cadence)
        await ts.flush()
        for i, spec in enumerate(steps):
            channel = spec.get("channel", "email")
            if channel != "email":
                raise CadenceError(f"v1 supports email only, got channel={channel!r}")
            delay = int(spec.get("delay_days", 0))
            if delay < 0:
                raise CadenceError("delay_days must be >= 0")
            ts.add(
                CadenceStep(
                    tenant_id=ts.tenant_id,
                    cadence_id=cadence.id,
                    step_index=i,
                    delay_days=delay,
                    angle=spec.get("angle", "") or "",
                    channel="email",
                )
            )
        await ts.flush()
        return cadence

    async def list_steps(self, ts: TenantSession, cadence_id: str) -> list[CadenceStep]:
        steps = await ts.list(CadenceStep, CadenceStep.cadence_id == cadence_id)
        return sorted(steps, key=lambda s: s.step_index)

    async def _step_at(
        self, ts: TenantSession, cadence_id: str, index: int
    ) -> CadenceStep | None:
        return await ts.first(
            CadenceStep,
            CadenceStep.cadence_id == cadence_id,
            CadenceStep.step_index == index,
        )

    # ----- Enrollment ---------------------------------------------------------------
    async def enroll(
        self,
        ts: TenantSession,
        campaign: Campaign,
        target,
        *,
        now: datetime,
    ) -> CadenceEnrollment:
        """Enroll one DRAFTED campaign target into the campaign's cadence. The first step
        becomes due at ``now + step0.delay_days``. contact_id is taken from the drafted
        snapshot so the cadence messages exactly the targeted contact."""
        step0 = await self._step_at(ts, campaign.cadence_id, 0)
        if step0 is None:
            raise CadenceError("cadence has no step 0")
        enrollment = CadenceEnrollment(
            tenant_id=ts.tenant_id,
            campaign_id=campaign.id,
            campaign_target_id=target.id,
            account_id=target.account_id,
            contact_id=(target.draft or {}).get("contact_id"),
            cadence_id=campaign.cadence_id,
            current_step_index=0,
            status=ENROLL_ACTIVE,
            next_touch_at=now + timedelta(days=step0.delay_days),
            started_at=now,
        )
        ts.add(enrollment)
        await ts.flush()
        return enrollment


_service = CadenceService()


def get_cadence_service() -> CadenceService:
    return _service
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_cadence_engine.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add nexus/cadences/__init__.py nexus/cadences/service.py tests/test_cadence_engine.py
git commit -m "feat(cadence): CadenceService create_cadence + enroll"
```

### Task 7: CadenceService — advance one touch (happy path)

This is the engine core: claim due enrollments, run the current step (compose → grounded gate → deliverability policy → send), record an `Outcome("sent")`, and advance to the next step at `now + delay_days`. The clock is an explicit `now` parameter so tests advance days with `timedelta` and never sleep. Stop conditions (reply / undeliverable / pause / duration cap) and `review_each_touch` parking are layered on in Tasks 8 and 9; this task is the deterministic happy path.

**Files:**
- Modify: `nexus/cadences/service.py` (add methods to `CadenceService`)
- Test: `tests/test_cadence_engine.py`

- [ ] **Step 1: Write the failing test**

Add these imports to the top of `tests/test_cadence_engine.py` (alongside the existing ones):

```python
from datetime import datetime, timedelta, timezone

from nexus.campaigns.service import get_campaign_service
from nexus.cadences.service import CadenceError, get_cadence_service
from nexus.models.account import Account, Contact
from nexus.models.cadence import (
    CadenceEnrollment,
    CadenceTouch,
    ENROLL_ACTIVE,
    ENROLL_COMPLETED,
    ENROLL_PAUSED,
    ENROLL_STOPPED,
    STOP_MANUAL,
    STOP_MAX_TOUCHES,
    STOP_REPLIED,
    STOP_UNDELIVERABLE,
    TOUCH_AWAITING_APPROVAL,
    TOUCH_SENT,
    TOUCH_SKIPPED,
)
from nexus.models.campaign import Campaign, CampaignTarget, TARGET_DRAFTED
from nexus.models.outcome import Outcome
```

Add the shared `ts` fixture, the `_enrollable` helper, and the happy-path test. The
`ts` fixture mirrors the one in `tests/test_campaign_engine.py`: a `TenantSession` bound
to a fresh tenant whose context commits on exit. (Tasks 8 and 10 reuse this same fixture.)

```python
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def ts():
    """A TenantSession bound to a fresh tenant. The context commits on exit."""
    tid = await make_tenant(slug="cad-svc", name="Cad Svc")
    async with tenant_session(tid) as session:
        yield session


async def _enrollable(ts, now, *, steps, review_each_touch=False, email="lead@acme.com"):
    """Build an account + contact, a cadence, a campaign wired to it, and one DRAFTED
    target; enroll the target. Returns (campaign, enrollment, account, contact). Pass a
    malformed ``email`` to drive the undeliverable path (the stub verifier rejects bad syntax)."""
    acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
    ts.add(acc)
    await ts.flush()
    contact = Contact(
        tenant_id=ts.tenant_id, account_id=acc.id, full_name="Lead", email=email
    )
    ts.add(contact)
    await ts.flush()
    cadence = await get_cadence_service().create_cadence(
        ts, name="multi-touch", description=None, steps=steps, created_by_user_id="u1"
    )
    campaign = Campaign(
        tenant_id=ts.tenant_id,
        name="Q3",
        list_id="l1",
        icp={"industries": ["SaaS"]},
        sequence="ai-orchestrated-outbound",
        cadence_id=cadence.id,
        review_each_touch=review_each_touch,
        created_by_user_id="u1",
    )
    ts.add(campaign)
    await ts.flush()
    target = CampaignTarget(
        tenant_id=ts.tenant_id,
        campaign_id=campaign.id,
        account_id=acc.id,
        status=TARGET_DRAFTED,
        draft={"contact_id": contact.id},
    )
    ts.add(target)
    await ts.flush()
    enrollment = await get_cadence_service().enroll(ts, campaign, target, now=now)
    return campaign, enrollment, acc, contact


async def test_cadence_happy_path_sends_each_touch(ts):
    svc = get_cadence_service()
    _, e, _, _ = await _enrollable(
        ts,
        NOW,
        steps=[
            {"delay_days": 0, "angle": "introduce the value prop"},
            {"delay_days": 3, "angle": "follow up with a case study"},
        ],
    )
    assert e.status == ENROLL_ACTIVE
    assert e.current_step_index == 0

    # Tick at t0: the first touch sends and the enrollment advances to step 1 (due in 3 days).
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 1
    await ts.refresh(e)
    assert e.current_step_index == 1
    assert e.status == ENROLL_ACTIVE
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    assert [t.status for t in touches] == [TOUCH_SENT]

    # Same instant: step 1 is not due yet — no work, no double send.
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 0

    # t0 + 3 days: the final touch sends and the enrollment completes.
    later = NOW + timedelta(days=3)
    assert await svc.advance_due_for_tenant(ts, now=later, limit=100) == 1
    await ts.refresh(e)
    assert e.status == ENROLL_COMPLETED
    assert e.completed_at is not None
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    assert sorted(t.step_index for t in touches) == [0, 1]
    assert all(t.status == TOUCH_SENT for t in touches)

    # Each send recorded an Outcome("sent") for manager attribution.
    outcomes = await ts.list(Outcome, Outcome.stage == "sent")
    assert len(outcomes) == 2


async def test_cadence_touch_is_idempotent_on_reclaim(ts):
    """Structural idempotency: a crash that sent a touch but never advanced must not double
    send. We simulate it — a TOUCH_SENT row exists for step 0 while the enrollment is still
    parked at step 0 and due. The next tick must NOT re-send or duplicate the touch; it just
    advances. This is the same guarantee the unique (enrollment_id, step_index) gives at the
    DB layer, asserted at the service layer."""
    svc = get_cadence_service()
    _, e, _, _ = await _enrollable(
        ts,
        NOW,
        steps=[
            {"delay_days": 0, "angle": "intro"},
            {"delay_days": 2, "angle": "bump"},
        ],
    )
    # Pre-existing sent touch for step 0, enrollment still at step 0 (the crash window).
    ts.add(CadenceTouch(
        tenant_id=ts.tenant_id, enrollment_id=e.id, step_index=0,
        status=TOUCH_SENT, sent_at=NOW,
    ))
    await ts.flush()

    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 1
    await ts.refresh(e)
    assert e.current_step_index == 1          # advanced past the already-sent step
    assert e.status == ENROLL_ACTIVE
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    assert [t.step_index for t in touches] == [0]   # no duplicate touch for step 0
    assert [t.status for t in touches] == [TOUCH_SENT]
    # No second send happened: still zero Outcome("sent") rows (the existing touch was
    # pre-inserted directly, bypassing _send, so the count proves no re-send).
    assert await ts.list(Outcome, Outcome.stage == "sent") == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_cadence_engine.py -k "happy_path or idempotent_on_reclaim" -v`
Expected: FAIL with `AttributeError: 'CadenceService' object has no attribute 'advance_due_for_tenant'`

- [ ] **Step 3: Add the advance/touch methods to `CadenceService`**

Insert these methods into the `CadenceService` class in `nexus/cadences/service.py`, after the `enroll` method and before the module-level `_service = CadenceService()`:

```python
    # ----- Advance tick -------------------------------------------------------------
    async def advance_due_for_tenant(
        self, ts: TenantSession, *, now: datetime, limit: int
    ) -> int:
        """Claim and process every active enrollment whose next touch is due at ``now``.

        Returns the number of enrollments claimed. Each step flushes; the caller's
        ``tenant_session`` owns the commit (the worker context commits on exit)."""
        due = await self._claim_due(ts, now=now, limit=limit)
        for enrollment in due:
            await self._process_enrollment(ts, enrollment, now=now)
        return len(due)

    async def _claim_due(
        self, ts: TenantSession, *, now: datetime, limit: int
    ) -> list[CadenceEnrollment]:
        """Select active, due enrollments oldest-first. On Postgres, lock the claimed rows
        with ``FOR UPDATE SKIP LOCKED`` so concurrent workers never grab the same row;
        SQLite (tests) ignores row locks, so the clause is applied only on Postgres."""
        stmt = (
            ts.select(
                CadenceEnrollment,
                CadenceEnrollment.status == ENROLL_ACTIVE,
                CadenceEnrollment.next_touch_at <= now,
            )
            .order_by(CadenceEnrollment.next_touch_at)
            .limit(limit)
        )
        if get_settings().is_postgres:
            stmt = stmt.with_for_update(skip_locked=True)
        return list((await ts.session.scalars(stmt)).all())

    async def _process_enrollment(
        self, ts: TenantSession, e: CadenceEnrollment, *, now: datetime
    ) -> None:
        """Run one step and apply the resulting action. Isolation: a step that raises is
        recorded as a FAILED touch and the enrollment advances past it, so one bad step
        never wedges the enrollment (or blocks the rest of the batch)."""
        try:
            action = await self._run_touch(ts, e, now=now)
        except Exception as exc:  # noqa: BLE001 — isolation boundary
            await self._record_failed_touch(ts, e, exc)
            await self._advance(ts, e, now=now)
            return
        kind = action[0]
        if kind == "advance":
            await self._advance(ts, e, now=now)
        elif kind == "complete":
            await self._complete(ts, e, now=now)
        elif kind == "stop":
            await self._stop(ts, e, action[1], now=now)
        elif kind == "park":
            # Hold for human review: leave the AWAITING_APPROVAL touch and drop the
            # enrollment out of the due set until approve/reject resumes it (Task 9).
            e.status = ENROLL_PAUSED
            await ts.flush()

    async def _run_touch(self, ts: TenantSession, e: CadenceEnrollment, *, now: datetime):
        """Run the enrollment's current step exactly once. Returns an action tuple:
        ``("advance",)`` | ``("complete",)`` | ``("stop", reason)`` | ``("park",)``."""
        campaign = await ts.get(Campaign, e.campaign_id)
        if campaign is None:
            return ("complete",)
        step = await self._step_at(ts, e.cadence_id, e.current_step_index)
        if step is None:
            return ("complete",)

        # Structural idempotency: one touch per (enrollment, step). A re-claimed enrollment
        # whose step already sent simply advances rather than sending twice.
        existing = await ts.first(
            CadenceTouch,
            CadenceTouch.enrollment_id == e.id,
            CadenceTouch.step_index == e.current_step_index,
        )
        if existing is not None and existing.status == TOUCH_SENT:
            return ("advance",)

        run, draft = await self._compose(ts, e, step, now=now)
        touch = existing or self._new_touch(ts, e, run)

        # Grounded-send gate: never send a draft that wasn't grounded in retrieved facts.
        if not draft.get("grounded"):
            touch.status = TOUCH_SKIPPED
            touch.skip_reason = SKIP_UNGROUNDED
            await ts.flush()
            return ("advance",)

        # Pre-send deliverability policy (reuses the campaign engine's rules verbatim).
        skip = CampaignService._send_policy(draft, campaign)
        if skip is not None:
            touch.status = TOUCH_SKIPPED
            touch.skip_reason = skip
            await ts.flush()
            return ("advance",)

        # review_each_touch: park awaiting human approval instead of auto-sending (Task 9).
        if campaign.review_each_touch:
            touch.status = TOUCH_AWAITING_APPROVAL
            touch.draft = dict(draft)
            await ts.flush()
            return ("park",)

        _, undeliverable = await self._send(ts, e, campaign, run, draft, touch, now=now)
        if undeliverable:
            return ("stop", STOP_UNDELIVERABLE)
        return ("advance",)

    async def _compose(
        self, ts: TenantSession, e: CadenceEnrollment, step: CadenceStep, *, now: datetime
    ) -> tuple[OrchestrationRun, dict]:
        """Run a fresh ``research_compose`` for this touch, threading the step's angle so each
        touch reads differently. Returns the run plus its drafted message snapshot."""
        engine = get_orchestration_engine()
        runtime = get_agent_runtime()
        goal_input: dict = {"account_id": e.account_id}
        if e.contact_id:
            goal_input["contact_id"] = e.contact_id
        if step.angle:
            goal_input["angle"] = step.angle
        run = await engine.create_run(
            ts, "research_compose", goal_input, account_id=e.account_id
        )
        await engine.execute_run(ts, run, runtime=runtime)
        draft = dict((run.blackboard or {}).get("draft") or {})
        return run, draft

    def _new_touch(
        self, ts: TenantSession, e: CadenceEnrollment, run: OrchestrationRun
    ) -> CadenceTouch:
        """Stage a touch row for the current step. The caller sets the terminal status
        (sent / skipped / awaiting_approval) before the next flush."""
        touch = CadenceTouch(
            tenant_id=ts.tenant_id,
            enrollment_id=e.id,
            step_index=e.current_step_index,
            run_id=run.id,
            status=TOUCH_SENT,  # overwritten by the caller before flush
            draft={},
        )
        ts.add(touch)
        return touch

    async def _send(
        self,
        ts: TenantSession,
        e: CadenceEnrollment,
        campaign: Campaign,
        run: OrchestrationRun,
        draft: dict,
        touch: CadenceTouch,
        *,
        now: datetime,
    ) -> tuple[bool, bool]:
        """Replay the draft through ``SendMessageTool`` so the universal hard gates fire,
        then record an ``Outcome("sent")``. Returns ``(sent_ok, undeliverable)``; an
        undeliverable address is the only send failure that stops the enrollment."""
        run.blackboard = dict(run.blackboard or {})
        run.blackboard["draft"] = dict(draft)
        tc = ToolContext(
            ts=ts,
            runtime=get_agent_runtime(),
            run=run,
            inputs={"sequence": campaign.sequence},
        )
        try:
            await SendMessageTool().run(tc)
        except ToolError as exc:
            msg = str(exc).lower()
            touch.status = TOUCH_SKIPPED
            if "undeliverable" in msg or "invalid" in msg:
                touch.skip_reason = SKIP_UNDELIVERABLE
                touch.error = str(exc)
                await ts.flush()
                return (False, True)
            touch.skip_reason = SKIP_UNGROUNDED if "ungrounded" in msg else SKIP_NO_CONTACT
            touch.error = str(exc)
            await ts.flush()
            return (False, False)

        touch.status = TOUCH_SENT
        touch.sent_at = now
        await ts.flush()
        await get_outcome_service().record(
            ts,
            stage="sent",
            account_id=e.account_id,
            contact_id=e.contact_id,
            meta={
                "cadence_id": e.cadence_id,
                "enrollment_id": e.id,
                "campaign_id": e.campaign_id,
                "step_index": touch.step_index,
            },
        )
        return (True, False)

    async def _record_failed_touch(
        self, ts: TenantSession, e: CadenceEnrollment, exc: Exception
    ) -> None:
        """Persist a FAILED touch for the current step (guarded by the unique
        (enrollment, step) constraint via a pre-check)."""
        touch = await ts.first(
            CadenceTouch,
            CadenceTouch.enrollment_id == e.id,
            CadenceTouch.step_index == e.current_step_index,
        )
        if touch is None:
            touch = CadenceTouch(
                tenant_id=ts.tenant_id,
                enrollment_id=e.id,
                step_index=e.current_step_index,
                status=TOUCH_FAILED,
                draft={},
            )
            ts.add(touch)
        touch.status = TOUCH_FAILED
        touch.error = f"{type(exc).__name__}: {exc}"
        await ts.flush()

    # ----- Step transitions ---------------------------------------------------------
    async def _advance(self, ts: TenantSession, e: CadenceEnrollment, *, now: datetime) -> None:
        """Move to the next step (due at ``now + delay_days``) or complete if none remains."""
        next_index = e.current_step_index + 1
        nxt = await self._step_at(ts, e.cadence_id, next_index)
        if nxt is None:
            await self._complete(ts, e, now=now)
            return
        e.current_step_index = next_index
        e.next_touch_at = now + timedelta(days=nxt.delay_days)
        e.status = ENROLL_ACTIVE
        await ts.flush()

    async def _complete(self, ts: TenantSession, e: CadenceEnrollment, *, now: datetime) -> None:
        e.status = ENROLL_COMPLETED
        e.completed_at = now
        await ts.flush()

    async def _stop(
        self, ts: TenantSession, e: CadenceEnrollment, reason: str, *, now: datetime
    ) -> None:
        e.status = ENROLL_STOPPED
        e.stop_reason = reason
        e.completed_at = now
        await ts.flush()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_cadence_engine.py -k "happy_path or idempotent_on_reclaim" -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/cadences/service.py tests/test_cadence_engine.py
git commit -m "feat(cadence): advance tick — compose, gate, send, advance one touch"
```

### Task 8: Stop conditions + pause/resume/stop controls

Layer the four stop conditions onto `_run_touch` (reply, undeliverable, duration cap) and add the manual controls (`pause`, `resume`, `stop`) the API will call. A replied/undeliverable/over-cap enrollment terminates instead of touching the prospect again.

**Files:**
- Modify: `nexus/cadences/service.py` (extend `_run_touch`, add stop detectors + controls)
- Test: `tests/test_cadence_engine.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_cadence_engine.py`:

```python
async def test_cadence_stops_on_reply(ts):
    svc = get_cadence_service()
    _, e, acc, contact = await _enrollable(ts, NOW, steps=[{"delay_days": 0, "angle": "intro"}])
    # The prospect replied (a positive outcome). The next tick must stop, not send.
    await get_outcome_service().record(
        ts, stage="replied", account_id=acc.id, contact_id=contact.id
    )
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 1
    await ts.refresh(e)
    assert e.status == ENROLL_STOPPED
    assert e.stop_reason == STOP_REPLIED
    assert await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id) == []


async def test_cadence_stops_on_undeliverable(ts):
    svc = get_cadence_service()
    # A malformed address verifies as invalid -> the send policy holds it as undeliverable,
    # and because every future touch hits the same dead address, the enrollment stops.
    _, e, _, _ = await _enrollable(
        ts, NOW, steps=[{"delay_days": 0, "angle": "intro"}, {"delay_days": 2, "angle": "bump"}],
        email="deadinbox",
    )
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 1
    await ts.refresh(e)
    assert e.status == ENROLL_STOPPED
    assert e.stop_reason == STOP_UNDELIVERABLE
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    assert [t.status for t in touches] == [TOUCH_SKIPPED]


async def test_cadence_pause_and_resume(ts):
    svc = get_cadence_service()
    _, e, _, _ = await _enrollable(ts, NOW, steps=[{"delay_days": 0, "angle": "intro"}])
    await svc.pause(ts, e)
    assert e.status == ENROLL_PAUSED
    # Paused enrollments are not claimed.
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 0

    later = NOW + timedelta(days=1)
    await svc.resume(ts, e, now=later)
    assert e.status == ENROLL_ACTIVE
    assert await svc.advance_due_for_tenant(ts, now=later, limit=100) == 1
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    assert [t.status for t in touches] == [TOUCH_SENT]


async def test_cadence_stops_on_duration_cap(ts):
    svc = get_cadence_service()
    # Default cap is 30 days; a tick past it stops the enrollment before any further send.
    _, e, _, _ = await _enrollable(ts, NOW, steps=[{"delay_days": 0, "angle": "intro"}])
    past_cap = NOW + timedelta(days=31)
    assert await svc.advance_due_for_tenant(ts, now=past_cap, limit=100) == 1
    await ts.refresh(e)
    assert e.status == ENROLL_STOPPED
    assert e.stop_reason == STOP_MAX_TOUCHES
    assert await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id) == []


async def test_manual_stop_is_terminal(ts):
    svc = get_cadence_service()
    _, e, _, _ = await _enrollable(ts, NOW, steps=[{"delay_days": 0, "angle": "intro"}])
    await svc.stop(ts, e)
    assert e.status == ENROLL_STOPPED
    assert e.stop_reason == STOP_MANUAL
    # A stopped enrollment is never claimed again.
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_cadence_engine.py -k "stops_on or pause_and_resume or manual_stop" -v`
Expected: FAIL — `test_cadence_stops_on_reply` asserts `ENROLL_STOPPED` but the happy path sends (the reply check does not exist yet); `pause`/`resume`/`stop` raise `AttributeError`.

- [ ] **Step 3: Add the stop pre-checks to `_run_touch`**

In `nexus/cadences/service.py`, edit `_run_touch`. Replace this block:

```python
        step = await self._step_at(ts, e.cadence_id, e.current_step_index)
        if step is None:
            return ("complete",)

        # Structural idempotency: one touch per (enrollment, step). A re-claimed enrollment
        # whose step already sent simply advances rather than sending twice.
```

with:

```python
        step = await self._step_at(ts, e.cadence_id, e.current_step_index)
        if step is None:
            return ("complete",)

        # Stop conditions, checked before composing so a terminated enrollment never wastes
        # a compose or touches the prospect again.
        if await self._has_reply(ts, e):
            return ("stop", STOP_REPLIED)
        if self._exceeded_duration(e, now):
            return ("stop", STOP_MAX_TOUCHES)

        # Structural idempotency: one touch per (enrollment, step). A re-claimed enrollment
        # whose step already sent simply advances rather than sending twice.
```

- [ ] **Step 4: Make an undeliverable address stop the enrollment**

In `_run_touch`, replace the send-policy block:

```python
        # Pre-send deliverability policy (reuses the campaign engine's rules verbatim).
        skip = CampaignService._send_policy(draft, campaign)
        if skip is not None:
            touch.status = TOUCH_SKIPPED
            touch.skip_reason = skip
            await ts.flush()
            return ("advance",)
```

with:

```python
        # Pre-send deliverability policy (reuses the campaign engine's rules verbatim).
        skip = CampaignService._send_policy(draft, campaign)
        if skip is not None:
            touch.status = TOUCH_SKIPPED
            touch.skip_reason = skip
            await ts.flush()
            # A dead address dooms every future touch to the same contact — stop here.
            if skip == SKIP_UNDELIVERABLE:
                return ("stop", STOP_UNDELIVERABLE)
            return ("advance",)
```

- [ ] **Step 5: Add the stop detectors and manual controls**

Insert these methods into `CadenceService`, after `_stop` (added in Task 7):

```python
    # ----- Stop detection -----------------------------------------------------------
    async def _has_reply(self, ts: TenantSession, e: CadenceEnrollment) -> bool:
        """True if a positive outcome (replied/meeting/won) landed for this enrollment's
        contact since it started. ``Outcome("sent")`` rows the cadence itself writes are
        not positive, so they never self-trigger a stop."""
        if e.contact_id is None:
            return False
        outcomes = await ts.list(
            Outcome,
            Outcome.contact_id == e.contact_id,
            Outcome.stage.in_(("replied", "meeting", "won")),
        )
        started = ensure_aware(e.started_at)
        for o in outcomes:
            if started is None or ensure_aware(o.created_at) >= started:
                return True
        return False

    def _exceeded_duration(self, e: CadenceEnrollment, now: datetime) -> bool:
        """True once the enrollment has been running longer than the configured cap."""
        started = ensure_aware(e.started_at)
        if started is None:
            return False
        cap_days = get_settings().cadence_max_duration_days
        return (now - started) > timedelta(days=cap_days)

    # ----- Manual controls ----------------------------------------------------------
    async def pause(self, ts: TenantSession, e: CadenceEnrollment) -> CadenceEnrollment:
        """Pause an active enrollment so the tick stops claiming it."""
        if e.status == ENROLL_ACTIVE:
            e.status = ENROLL_PAUSED
            await ts.flush()
        return e

    async def resume(
        self, ts: TenantSession, e: CadenceEnrollment, *, now: datetime
    ) -> CadenceEnrollment:
        """Resume a paused enrollment, making its current step due immediately."""
        if e.status == ENROLL_PAUSED:
            e.status = ENROLL_ACTIVE
            e.next_touch_at = now
            await ts.flush()
        return e

    async def stop(self, ts: TenantSession, e: CadenceEnrollment) -> CadenceEnrollment:
        """Manually and terminally stop an enrollment."""
        if e.status not in ENROLL_TERMINAL:
            e.status = ENROLL_STOPPED
            e.stop_reason = STOP_MANUAL
            e.completed_at = utcnow()
            await ts.flush()
        return e
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_cadence_engine.py -v`
Expected: PASS (all cadence tests, including Task 7's happy path)

- [ ] **Step 7: Commit**

```bash
git add nexus/cadences/service.py tests/test_cadence_engine.py
git commit -m "feat(cadence): stop on reply/undeliverable/duration + pause/resume/stop"
```

### Task 9: review_each_touch — approve / reject a parked touch

When a campaign opts into `review_each_touch`, each touch parks at an `AWAITING_APPROVAL` touch row instead of auto-sending (Task 7 already returns `("park",)`). This task adds the two operations a reviewer takes: approve (optionally with an edited body) → send + advance; reject → skip + advance, or reject-and-stop.

**Files:**
- Modify: `nexus/cadences/service.py` (add `approve_touch`, `reject_touch`)
- Test: `tests/test_cadence_engine.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_cadence_engine.py`:

```python
async def test_review_each_touch_parks_then_approves(ts):
    svc = get_cadence_service()
    _, e, _, _ = await _enrollable(
        ts, NOW, steps=[{"delay_days": 0, "angle": "intro"}], review_each_touch=True
    )
    # The tick parks the touch for review instead of sending.
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 1
    await ts.refresh(e)
    assert e.status == ENROLL_PAUSED
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    assert [t.status for t in touches] == [TOUCH_AWAITING_APPROVAL]
    assert await ts.list(Outcome, Outcome.stage == "sent") == []

    # Approving sends the touch; with a single step the enrollment then completes.
    await svc.approve_touch(ts, e, 0, now=NOW)
    await ts.refresh(e)
    assert e.status == ENROLL_COMPLETED
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    assert [t.status for t in touches] == [TOUCH_SENT]
    assert len(await ts.list(Outcome, Outcome.stage == "sent")) == 1


async def test_review_reject_advances_to_next_step(ts):
    svc = get_cadence_service()
    _, e, _, _ = await _enrollable(
        ts,
        NOW,
        steps=[{"delay_days": 0, "angle": "intro"}, {"delay_days": 2, "angle": "bump"}],
        review_each_touch=True,
    )
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 1
    # Reject step 0 (no stop): the touch is skipped and the enrollment advances to step 1.
    await svc.reject_touch(ts, e, 0, now=NOW)
    await ts.refresh(e)
    assert e.status == ENROLL_ACTIVE
    assert e.current_step_index == 1
    touch0 = await ts.first(
        CadenceTouch, CadenceTouch.enrollment_id == e.id, CadenceTouch.step_index == 0
    )
    assert touch0.status == TOUCH_SKIPPED
    assert touch0.skip_reason == "rejected"


async def test_review_reject_with_stop_is_terminal(ts):
    svc = get_cadence_service()
    _, e, _, _ = await _enrollable(
        ts,
        NOW,
        steps=[{"delay_days": 0, "angle": "intro"}, {"delay_days": 2, "angle": "bump"}],
        review_each_touch=True,
    )
    assert await svc.advance_due_for_tenant(ts, now=NOW, limit=100) == 1
    await svc.reject_touch(ts, e, 0, now=NOW, stop=True)
    await ts.refresh(e)
    assert e.status == ENROLL_STOPPED
    assert e.stop_reason == STOP_MANUAL
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_cadence_engine.py -k review -v`
Expected: FAIL — `AttributeError: 'CadenceService' object has no attribute 'approve_touch'`

- [ ] **Step 3: Add `approve_touch` and `reject_touch`**

Insert these methods into `CadenceService`, after the manual controls (`stop`) added in Task 8:

```python
    # ----- Per-touch review ---------------------------------------------------------
    async def _awaiting_touch(
        self, ts: TenantSession, e: CadenceEnrollment, step_index: int
    ) -> CadenceTouch:
        touch = await ts.first(
            CadenceTouch,
            CadenceTouch.enrollment_id == e.id,
            CadenceTouch.step_index == step_index,
        )
        if touch is None or touch.status != TOUCH_AWAITING_APPROVAL:
            raise CadenceError("no touch awaiting approval at that step")
        return touch

    async def approve_touch(
        self,
        ts: TenantSession,
        e: CadenceEnrollment,
        step_index: int,
        *,
        now: datetime,
        edited_body: str | None = None,
    ) -> CadenceEnrollment:
        """Approve a parked touch: send it (optionally with an edited body), then advance.
        An undeliverable address surfaced at send time stops the enrollment instead."""
        touch = await self._awaiting_touch(ts, e, step_index)
        campaign = await ts.get(Campaign, e.campaign_id)
        run = await ts.get(OrchestrationRun, touch.run_id) if touch.run_id else None
        if campaign is None or run is None:
            raise CadenceError("approval lost its campaign or compose run")
        draft = dict(touch.draft or {})
        if edited_body is not None:
            draft["body"] = edited_body
        _, undeliverable = await self._send(ts, e, campaign, run, draft, touch, now=now)
        if undeliverable:
            await self._stop(ts, e, STOP_UNDELIVERABLE, now=now)
        else:
            await self._advance(ts, e, now=now)
        return e

    async def reject_touch(
        self,
        ts: TenantSession,
        e: CadenceEnrollment,
        step_index: int,
        *,
        now: datetime,
        stop: bool = False,
    ) -> CadenceEnrollment:
        """Reject a parked touch. By default the touch is skipped and the enrollment moves
        to the next step; ``stop=True`` terminates the whole enrollment."""
        touch = await self._awaiting_touch(ts, e, step_index)
        touch.status = TOUCH_SKIPPED
        touch.skip_reason = "rejected"
        await ts.flush()
        if stop:
            await self._stop(ts, e, STOP_MANUAL, now=now)
        else:
            await self._advance(ts, e, now=now)
        return e
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_cadence_engine.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/cadences/service.py tests/test_cadence_engine.py
git commit -m "feat(cadence): review_each_touch approve/reject controls"
```

### Task 10: Worker handler — the periodic advance tick

The cadence engine is driven by a periodic job. The handler is the only place the *real* clock enters: it scans globally (a raw, tenant-agnostic session) for tenants that have a due enrollment, then does the work per-tenant inside a `tenant_session`. Gated by `cadence_enabled` so it is inert until switched on.

**Files:**
- Modify: `nexus/workers/tasks.py` (add handler + enqueue + register)
- Test: `tests/test_cadence_engine.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_cadence_engine.py`:

```python
async def test_handle_advance_cadences_disabled_is_noop(monkeypatch):
    from nexus.workers.tasks import handle_advance_cadences

    monkeypatch.setattr(get_settings(), "cadence_enabled", False)
    assert await handle_advance_cadences({}) == {"skipped": "cadence_disabled"}


async def test_handle_advance_cadences_processes_due(ts, monkeypatch):
    from nexus.core.db import utcnow
    from nexus.workers.tasks import handle_advance_cadences

    monkeypatch.setattr(get_settings(), "cadence_enabled", True)
    # Enroll at the real clock so the wall-clock tick inside the handler finds it due
    # (and well inside the duration cap).
    now0 = utcnow()
    _, e, _, _ = await _enrollable(ts, now0, steps=[{"delay_days": 0, "angle": "intro"}])
    await ts.commit()  # the handler scans in its own sessions

    result = await handle_advance_cadences({})
    assert result["tenants"] >= 1
    assert result["processed"] >= 1

    # Confirm the touch sent, reading through a fresh tenant-bound session.
    async with tenant_session(ts.tenant_id) as ts2:
        touches = await ts2.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
        assert [t.status for t in touches] == [TOUCH_SENT]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_cadence_engine.py -k handle_advance -v`
Expected: FAIL — `ImportError: cannot import name 'handle_advance_cadences' from 'nexus.workers.tasks'`

- [ ] **Step 3: Add the handler, enqueue helper, and registration**

In `nexus/workers/tasks.py`, add this handler after `handle_run_campaign` (before the `HANDLERS` dict):

```python
async def handle_advance_cadences(payload: dict) -> dict:
    """Periodic cadence driver. Scans globally for tenants with a due enrollment, then
    advances each tenant's due enrollments inside its own tenant-bound session.

    The scan uses a raw, tenant-agnostic session (it only reads tenant ids, never ORM
    rows), keeping the per-tenant isolation guarantee for the actual work. Inert unless
    ``cadence_enabled`` is set."""
    from datetime import datetime, timezone

    from sqlalchemy import distinct, select

    from nexus.core.config import get_settings
    from nexus.cadences.service import get_cadence_service
    from nexus.models.cadence import CadenceEnrollment, ENROLL_ACTIVE

    settings = get_settings()
    if not settings.cadence_enabled:
        return {"skipped": "cadence_disabled"}

    now = datetime.now(timezone.utc)
    batch = settings.cadence_batch_size

    async with get_sessionmaker()() as session:
        rows = await session.scalars(
            select(distinct(CadenceEnrollment.tenant_id)).where(
                CadenceEnrollment.status == ENROLL_ACTIVE,
                CadenceEnrollment.next_touch_at <= now,
            )
        )
        tenant_ids = list(rows.all())

    processed = 0
    for tid in tenant_ids:
        async with tenant_session(tid) as ts:
            processed += await get_cadence_service().advance_due_for_tenant(
                ts, now=now, limit=batch
            )
    return {"tenants": len(tenant_ids), "processed": processed}
```

Register it in the `HANDLERS` dict:

```python
HANDLERS: dict[str, Handler] = {
    "process_account": handle_process_account,
    "run_orchestration": handle_run_orchestration,
    "run_campaign": handle_run_campaign,
    "advance_cadences": handle_advance_cadences,
}
```

Add the enqueue helper after `enqueue_run_campaign`:

```python
async def enqueue_advance_cadences(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="advance_cadences", payload={}))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_cadence_engine.py -k handle_advance -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/workers/tasks.py tests/test_cadence_engine.py
git commit -m "feat(cadence): periodic advance_cadences worker handler"
```

### Task 11: Pydantic wire schemas

The API contracts for cadences, enrollments, touches, and the cadence report. Mirrors the style of `nexus/campaigns/schemas.py` (explicit `from_model` classmethods, no ORM mode magic).

**Files:**
- Create: `nexus/cadences/schemas.py`
- Test: `tests/test_cadence_engine.py`

- [ ] **Step 1: Write the failing test**

Add this test to `tests/test_cadence_engine.py`:

```python
def test_cadence_in_step_defaults():
    from nexus.cadences.schemas import CadenceIn

    c = CadenceIn(
        name="3-touch",
        steps=[{"delay_days": 0}, {"delay_days": 3, "angle": "case study"}],
    )
    assert c.steps[0].channel == "email"
    assert c.steps[0].angle == ""
    assert c.steps[0].delay_days == 0
    assert c.steps[1].angle == "case study"
    # Wire model rejects an empty cadence (the service enforces the same rule).
    with pytest.raises(Exception):
        CadenceIn(name="empty", steps=[])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_cadence_engine.py::test_cadence_in_step_defaults -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.cadences.schemas'`

- [ ] **Step 3: Create the schemas**

Create `nexus/cadences/schemas.py`:

```python
"""Pydantic wire contracts for the cadence engine API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from nexus.models.cadence import (
    Cadence,
    CadenceEnrollment,
    CadenceStep,
    CadenceTouch,
)


class CadenceStepIn(BaseModel):
    delay_days: int = Field(default=0, ge=0)
    angle: str = Field(default="", max_length=2000)
    channel: str = Field(default="email", max_length=16)


class CadenceIn(BaseModel):
    name: str = Field(..., max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    steps: list[CadenceStepIn] = Field(..., min_length=1)


class CadenceStepOut(BaseModel):
    step_index: int
    delay_days: int
    angle: str
    channel: str

    @classmethod
    def from_model(cls, s: CadenceStep) -> "CadenceStepOut":
        return cls(
            step_index=s.step_index,
            delay_days=s.delay_days,
            angle=s.angle or "",
            channel=s.channel,
        )


class CadenceOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    is_active: bool
    created_at: datetime
    steps: list[CadenceStepOut] = Field(default_factory=list)

    @classmethod
    def from_models(cls, c: Cadence, steps: list[CadenceStep]) -> "CadenceOut":
        return cls(
            id=c.id,
            name=c.name,
            description=c.description,
            is_active=c.is_active,
            created_at=c.created_at,
            steps=[CadenceStepOut.from_model(s) for s in steps],
        )


class CadenceEnrollmentOut(BaseModel):
    id: str
    campaign_id: str
    account_id: str
    contact_id: str | None = None
    cadence_id: str
    current_step_index: int
    status: str
    stop_reason: str | None = None
    next_touch_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @classmethod
    def from_model(cls, e: CadenceEnrollment) -> "CadenceEnrollmentOut":
        return cls(
            id=e.id,
            campaign_id=e.campaign_id,
            account_id=e.account_id,
            contact_id=e.contact_id,
            cadence_id=e.cadence_id,
            current_step_index=e.current_step_index,
            status=e.status,
            stop_reason=e.stop_reason,
            next_touch_at=e.next_touch_at,
            started_at=e.started_at,
            completed_at=e.completed_at,
        )


class CadenceTouchOut(BaseModel):
    id: str
    enrollment_id: str
    step_index: int
    status: str
    skip_reason: str | None = None
    run_id: str | None = None
    sent_at: datetime | None = None
    error: str | None = None

    @classmethod
    def from_model(cls, t: CadenceTouch) -> "CadenceTouchOut":
        return cls(
            id=t.id,
            enrollment_id=t.enrollment_id,
            step_index=t.step_index,
            status=t.status,
            skip_reason=t.skip_reason,
            run_id=t.run_id,
            sent_at=t.sent_at,
            error=t.error,
        )


class EnrollmentDetailOut(CadenceEnrollmentOut):
    touches: list[CadenceTouchOut] = Field(default_factory=list)

    @classmethod
    def from_models(
        cls, e: CadenceEnrollment, touches: list[CadenceTouch]
    ) -> "EnrollmentDetailOut":
        base = CadenceEnrollmentOut.from_model(e)
        return cls(
            **base.model_dump(),
            touches=[CadenceTouchOut.from_model(t) for t in touches],
        )


class CadenceReportOut(BaseModel):
    """Per-campaign cadence rollup for the manager dashboard."""

    campaign_id: str
    cadence_id: str | None = None
    total_enrollments: int = 0
    by_status: dict = Field(default_factory=dict)
    touches_sent: int = 0
    touches_skipped: int = 0
    stops: dict = Field(default_factory=dict)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_cadence_engine.py::test_cadence_in_step_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/cadences/schemas.py tests/test_cadence_engine.py
git commit -m "feat(cadence): pydantic wire schemas"
```

### Task 12: API router + campaign wiring + cadence report

The control surface. Wire the campaign approve path to enroll into a cadence (instead of a one-shot send) when `cadence_id` is set, add the cadence report rollup, and expose the cadence CRUD + enrollment control endpoints. All gated by `manage_campaigns` (reusing the campaign permission).

**Files:**
- Modify: `nexus/campaigns/service.py` (`create` signature, `approve_and_send` branch, `_enroll_drafted`)
- Modify: `nexus/cadences/service.py` (add `cadence_report`)
- Modify: `nexus/campaigns/schemas.py` (`CampaignIn` + `CampaignOut` cadence fields)
- Modify: `nexus/api/routers/campaigns.py` (pass cadence fields through `create`)
- Create: `nexus/api/routers/cadences.py`
- Modify: `nexus/api/routers/__init__.py` (register)
- Test: `tests/test_cadence_engine.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_cadence_engine.py`. Extend the campaign import line at the top to include `CAMP_AWAITING_APPROVAL` and `CAMP_SENDING`:

```python
from nexus.models.campaign import (
    Campaign,
    CampaignTarget,
    CAMP_AWAITING_APPROVAL,
    CAMP_SENDING,
    TARGET_APPROVED,
    TARGET_DRAFTED,
)
```

Add the import for `signup`, `auth` (already imported by other tests via conftest — add if missing):

```python
from tests.conftest import auth, signup
```

Tests:

```python
async def test_campaign_with_cadence_enrolls_on_approve(ts):
    from nexus.models.workflow import ListItem, ProspectList

    camp_svc = get_campaign_service()
    cad_svc = get_cadence_service()
    cadence = await cad_svc.create_cadence(
        ts,
        name="2-touch",
        description=None,
        steps=[{"delay_days": 0, "angle": "a"}, {"delay_days": 3, "angle": "b"}],
        created_by_user_id="u1",
    )
    plist = ProspectList(tenant_id=ts.tenant_id, name="seg", filter={})
    ts.add(plist)
    await ts.flush()
    acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
    ts.add(acc)
    await ts.flush()
    ts.add(Contact(tenant_id=ts.tenant_id, account_id=acc.id, full_name="Lead", email="lead@acme.com"))
    ts.add(ListItem(tenant_id=ts.tenant_id, list_id=plist.id, account_id=acc.id))
    await ts.flush()

    campaign = await camp_svc.create(
        ts,
        name="Q3",
        list_id=plist.id,
        icp={"industries": ["SaaS"]},
        sequence="ai-orchestrated-outbound",
        created_by_user_id="u1",
        cadence_id=cadence.id,
    )
    await camp_svc.run_draft_phase(ts, campaign)
    assert campaign.status == CAMP_AWAITING_APPROVAL
    await camp_svc.approve_and_send(ts, campaign, decided_by="u1")

    # Cadence path: approval ENROLLS rather than sending once. The campaign stays SENDING and
    # the periodic tick drives the touches from here.
    assert campaign.status == CAMP_SENDING
    enrollments = await ts.list(CadenceEnrollment, CadenceEnrollment.campaign_id == campaign.id)
    assert len(enrollments) == 1
    assert enrollments[0].status == ENROLL_ACTIVE
    targets = await camp_svc.list_targets(ts, campaign.id)
    assert all(t.status == TARGET_APPROVED for t in targets)

    report = await cad_svc.cadence_report(ts, campaign.id)
    assert report["total_enrollments"] == 1
    assert report["by_status"].get(ENROLL_ACTIVE) == 1
    assert report["cadence_id"] == cadence.id


async def test_cadences_crud_http(client):
    token = await signup(client, slug="cad", email="mgr@cad.com", company="Cad")
    r = await client.post(
        "/cadences",
        headers=auth(token),
        json={
            "name": "3-touch",
            "description": "outbound",
            "steps": [{"delay_days": 0, "angle": "intro"}, {"delay_days": 3, "angle": "bump"}],
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert [s["step_index"] for s in r.json()["steps"]] == [0, 1]

    r = await client.get("/cadences", headers=auth(token))
    assert r.status_code == 200
    assert any(c["id"] == cid for c in r.json())

    r = await client.get(f"/cadences/{cid}", headers=auth(token))
    assert r.status_code == 200
    assert r.json()["steps"][1]["angle"] == "bump"

    r = await client.patch(f"/cadences/{cid}", headers=auth(token), json={"is_active": False})
    assert r.status_code == 200
    assert r.json()["is_active"] is False

    # An empty cadence is rejected by the wire contract (422).
    r = await client.post(
        "/cadences", headers=auth(token), json={"name": "empty", "steps": []}
    )
    assert r.status_code == 422
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_cadence_engine.py -k "enrolls_on_approve or cadences_crud_http" -v`
Expected: FAIL — `create()` rejects the `cadence_id` kwarg / `POST /cadences` returns 404 (route not registered).

- [ ] **Step 3: Extend `CampaignService.create` and the approve path**

In `nexus/campaigns/service.py`, add the `utcnow` import:

```python
from nexus.core.db import utcnow
```

Replace the `create` signature and the `Campaign(...)` construction:

```python
    async def create(
        self,
        ts: TenantSession,
        *,
        name: str,
        list_id: str,
        icp: dict,
        sequence: str,
        created_by_user_id: str | None,
        send_risky: bool = False,
        cadence_id: str | None = None,
        review_each_touch: bool = False,
    ) -> Campaign:
        """Create the campaign and one PENDING target per account in the List.

        When ``cadence_id`` is set, approval enrolls each drafted target into that cadence
        (multi-touch) instead of sending once; ``review_each_touch`` parks every touch for
        human review before it sends."""
        campaign = Campaign(
            tenant_id=ts.tenant_id,
            name=name,
            list_id=list_id,
            icp=icp or {},
            sequence=sequence or "ai-orchestrated-outbound",
            send_risky=send_risky,
            cadence_id=cadence_id,
            review_each_touch=review_each_touch,
            created_by_user_id=created_by_user_id,
        )
```

Replace `approve_and_send`:

```python
    async def approve_and_send(
        self, ts: TenantSession, campaign: Campaign, *, decided_by: str | None
    ) -> Campaign:
        """Campaign-level approval: one human decision. A cadence campaign enrolls its
        drafted targets (the tick drives the touches); a plain campaign sends once."""
        if campaign.status != CAMP_AWAITING_APPROVAL:
            raise CampaignError(
                f"campaign must be awaiting_approval to approve, is '{campaign.status}'"
            )
        campaign.status = CAMP_APPROVED
        await ts.flush()
        if campaign.cadence_id:
            return await self._enroll_drafted(ts, campaign, now=utcnow())
        return await self.run_send_phase(ts, campaign)
```

Add `_enroll_drafted` (place it right after `run_send_phase`):

```python
    async def _enroll_drafted(
        self, ts: TenantSession, campaign: Campaign, *, now
    ) -> Campaign:
        """Cadence path: enroll every DRAFTED target into the campaign's cadence and leave
        the campaign in SENDING. The periodic ``advance_cadences`` tick takes over from here.

        Lazy import of the cadence service avoids an import cycle: ``cadences.service``
        imports ``CampaignService`` at module load, so the reverse edge must stay lazy."""
        from nexus.cadences.service import get_cadence_service

        cad_svc = get_cadence_service()
        campaign.status = CAMP_SENDING
        await ts.flush()
        for target in await self.list_targets(ts, campaign.id):
            if target.status != TARGET_DRAFTED:
                continue
            await cad_svc.enroll(ts, campaign, target, now=now)
            target.status = TARGET_APPROVED
        campaign.report = await self._build_report(ts, campaign)
        await ts.flush()
        return campaign
```

- [ ] **Step 4: Add `cadence_report` to `CadenceService`**

Insert into `CadenceService` (after `reject_touch`), in `nexus/cadences/service.py`:

```python
    # ----- Reporting ----------------------------------------------------------------
    async def cadence_report(self, ts: TenantSession, campaign_id: str) -> dict:
        """Roll up a campaign's enrollments + touches for the manager dashboard."""
        campaign = await ts.get(Campaign, campaign_id)
        enrollments = await ts.list(
            CadenceEnrollment, CadenceEnrollment.campaign_id == campaign_id
        )
        by_status: dict[str, int] = {}
        stops: dict[str, int] = {}
        for e in enrollments:
            by_status[e.status] = by_status.get(e.status, 0) + 1
            if e.stop_reason:
                stops[e.stop_reason] = stops.get(e.stop_reason, 0) + 1
        sent = skipped = 0
        ids = [e.id for e in enrollments]
        if ids:
            touches = await ts.list(
                CadenceTouch, CadenceTouch.enrollment_id.in_(ids)
            )
            for t in touches:
                if t.status == TOUCH_SENT:
                    sent += 1
                elif t.status == TOUCH_SKIPPED:
                    skipped += 1
        return {
            "campaign_id": campaign_id,
            "cadence_id": campaign.cadence_id if campaign else None,
            "total_enrollments": len(enrollments),
            "by_status": by_status,
            "touches_sent": sent,
            "touches_skipped": skipped,
            "stops": stops,
        }
```

- [ ] **Step 5: Surface cadence fields on the campaign schemas**

In `nexus/campaigns/schemas.py`, add to `CampaignIn`:

```python
    cadence_id: str | None = None
    review_each_touch: bool = False
```

Add to `CampaignOut` (both the field declarations and `from_model`):

```python
class CampaignOut(BaseModel):
    id: str
    name: str
    list_id: str
    status: str
    sequence: str
    icp: dict = Field(default_factory=dict)
    report: dict = Field(default_factory=dict)
    send_risky: bool = False
    cadence_id: str | None = None
    review_each_touch: bool = False
    created_at: datetime

    @classmethod
    def from_model(cls, c: Campaign) -> "CampaignOut":
        return cls(
            id=c.id,
            name=c.name,
            list_id=c.list_id,
            status=c.status,
            sequence=c.sequence,
            icp=c.icp or {},
            report=c.report or {},
            send_risky=c.send_risky,
            cadence_id=c.cadence_id,
            review_each_touch=c.review_each_touch,
            created_at=c.created_at,
        )
```

- [ ] **Step 6: Pass the cadence fields through the campaign create endpoint**

In `nexus/api/routers/campaigns.py`, update the `svc.create(...)` call inside `create_campaign`:

```python
    campaign = await svc.create(
        ts,
        name=body.name,
        list_id=body.list_id,
        icp=body.icp,
        sequence=body.sequence,
        created_by_user_id=principal.user_id,
        send_risky=body.send_risky,
        cadence_id=body.cadence_id,
        review_each_touch=body.review_each_touch,
    )
```

If `body.cadence_id` is set, validate it exists (right after the `ProspectList` lookup in `create_campaign`):

```python
    if body.cadence_id is not None:
        from nexus.models.cadence import Cadence

        if await ts.get(Cadence, body.cadence_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Cadence not found")
```

- [ ] **Step 7: Create the cadences router**

Create `nexus/api/routers/cadences.py`:

```python
"""Cadence endpoints: define multi-touch cadences, inspect a campaign's enrollments and
touches, and control individual enrollments (pause/resume/stop, approve/reject a touch).

The router carries no prefix — cadence routes live under ``/cadences`` while enrollment and
report routes hang off ``/campaigns/{id}`` and ``/enrollments/{id}`` — so paths are written
in full. Every endpoint is gated by ``manage_campaigns`` (the same permission as campaigns)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.cadences.schemas import (
    CadenceEnrollmentOut,
    CadenceIn,
    CadenceOut,
    CadenceReportOut,
    EnrollmentDetailOut,
)
from nexus.cadences.service import CadenceError, get_cadence_service
from nexus.core.db import utcnow
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.cadence import Cadence, CadenceEnrollment, CadenceTouch

router = APIRouter(tags=["cadences"])


class _CadencePatchIn(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None


class _ApproveTouchIn(BaseModel):
    edited_body: str | None = None


class _RejectTouchIn(BaseModel):
    stop: bool = False


async def _get_cadence(ts: TenantSession, cadence_id: str) -> Cadence:
    c = await ts.get(Cadence, cadence_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Cadence not found")
    return c


async def _get_enrollment(ts: TenantSession, enrollment_id: str) -> CadenceEnrollment:
    e = await ts.get(CadenceEnrollment, enrollment_id)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Enrollment not found")
    return e


# ----- Cadence definitions ----------------------------------------------------------
@router.post("/cadences", response_model=CadenceOut, status_code=status.HTTP_201_CREATED)
async def create_cadence(
    body: CadenceIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceOut:
    svc = get_cadence_service()
    try:
        cadence = await svc.create_cadence(
            ts,
            name=body.name,
            description=body.description,
            steps=[s.model_dump() for s in body.steps],
            created_by_user_id=principal.user_id,
        )
    except CadenceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    steps = await svc.list_steps(ts, cadence.id)
    return CadenceOut.from_models(cadence, steps)


@router.get("/cadences", response_model=list[CadenceOut])
async def list_cadences(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> list[CadenceOut]:
    svc = get_cadence_service()
    stmt = ts.select(Cadence).order_by(Cadence.created_at.desc()).limit(100)
    cadences = list((await ts.session.scalars(stmt)).all())
    out: list[CadenceOut] = []
    for c in cadences:
        out.append(CadenceOut.from_models(c, await svc.list_steps(ts, c.id)))
    return out


@router.get("/cadences/{cadence_id}", response_model=CadenceOut)
async def get_cadence(
    cadence_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceOut:
    cadence = await _get_cadence(ts, cadence_id)
    steps = await get_cadence_service().list_steps(ts, cadence.id)
    return CadenceOut.from_models(cadence, steps)


@router.patch("/cadences/{cadence_id}", response_model=CadenceOut)
async def update_cadence(
    cadence_id: str,
    body: _CadencePatchIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceOut:
    cadence = await _get_cadence(ts, cadence_id)
    if body.name is not None:
        cadence.name = body.name
    if body.description is not None:
        cadence.description = body.description
    if body.is_active is not None:
        cadence.is_active = body.is_active
    await ts.flush()
    steps = await get_cadence_service().list_steps(ts, cadence.id)
    return CadenceOut.from_models(cadence, steps)


@router.delete("/cadences/{cadence_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_cadence(
    cadence_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> None:
    # Soft delete: existing enrollments may still reference this cadence, so deactivate
    # rather than orphan them. Idempotent.
    cadence = await _get_cadence(ts, cadence_id)
    cadence.is_active = False
    await ts.flush()


# ----- Enrollments + report ---------------------------------------------------------
@router.get("/campaigns/{campaign_id}/enrollments", response_model=list[CadenceEnrollmentOut])
async def list_enrollments(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> list[CadenceEnrollmentOut]:
    rows = await ts.list(CadenceEnrollment, CadenceEnrollment.campaign_id == campaign_id)
    return [CadenceEnrollmentOut.from_model(e) for e in rows]


@router.get("/campaigns/{campaign_id}/cadence-report", response_model=CadenceReportOut)
async def campaign_cadence_report(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceReportOut:
    report = await get_cadence_service().cadence_report(ts, campaign_id)
    return CadenceReportOut(**report)


@router.get("/enrollments/{enrollment_id}", response_model=EnrollmentDetailOut)
async def get_enrollment(
    enrollment_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> EnrollmentDetailOut:
    e = await _get_enrollment(ts, enrollment_id)
    touches = await ts.list(CadenceTouch, CadenceTouch.enrollment_id == e.id)
    touches.sort(key=lambda t: t.step_index)
    return EnrollmentDetailOut.from_models(e, touches)


@router.post("/enrollments/{enrollment_id}/pause", response_model=CadenceEnrollmentOut)
async def pause_enrollment(
    enrollment_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceEnrollmentOut:
    e = await _get_enrollment(ts, enrollment_id)
    await get_cadence_service().pause(ts, e)
    return CadenceEnrollmentOut.from_model(e)


@router.post("/enrollments/{enrollment_id}/resume", response_model=CadenceEnrollmentOut)
async def resume_enrollment(
    enrollment_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceEnrollmentOut:
    e = await _get_enrollment(ts, enrollment_id)
    await get_cadence_service().resume(ts, e, now=utcnow())
    return CadenceEnrollmentOut.from_model(e)


@router.post("/enrollments/{enrollment_id}/stop", response_model=CadenceEnrollmentOut)
async def stop_enrollment(
    enrollment_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceEnrollmentOut:
    e = await _get_enrollment(ts, enrollment_id)
    await get_cadence_service().stop(ts, e)
    return CadenceEnrollmentOut.from_model(e)


@router.post(
    "/enrollments/{enrollment_id}/touches/{step_index}/approve",
    response_model=CadenceEnrollmentOut,
)
async def approve_touch(
    enrollment_id: str,
    step_index: int,
    body: _ApproveTouchIn | None = None,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceEnrollmentOut:
    e = await _get_enrollment(ts, enrollment_id)
    edited_body = body.edited_body if body else None
    try:
        await get_cadence_service().approve_touch(
            ts, e, step_index, now=utcnow(), edited_body=edited_body
        )
    except CadenceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return CadenceEnrollmentOut.from_model(e)


@router.post(
    "/enrollments/{enrollment_id}/touches/{step_index}/reject",
    response_model=CadenceEnrollmentOut,
)
async def reject_touch(
    enrollment_id: str,
    step_index: int,
    body: _RejectTouchIn | None = None,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CadenceEnrollmentOut:
    e = await _get_enrollment(ts, enrollment_id)
    stop = body.stop if body else False
    try:
        await get_cadence_service().reject_touch(
            ts, e, step_index, now=utcnow(), stop=stop
        )
    except CadenceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return CadenceEnrollmentOut.from_model(e)
```

- [ ] **Step 8: Register the router**

In `nexus/api/routers/__init__.py`, add `cadences` to the import block and `cadences.router` to `all_routers` (place it right after `campaigns.router`):

```python
from nexus.api.routers import (
    accounts,
    agents,
    alerts,
    auth,
    cadences,
    campaigns,
    chat,
    custom_fields,
    integrations,
    orchestration,
    outcomes,
    relevance,
    signals,
    workflow,
    workspace,
)

all_routers = [
    auth.router,
    relevance.router,
    accounts.router,
    agents.router,
    workflow.router,
    campaigns.router,
    cadences.router,
    alerts.router,
    integrations.router,
    workspace.router,
    signals.router,
    orchestration.router,
    chat.router,
    custom_fields.router,
    outcomes.router,
]
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `python -m pytest tests/test_cadence_engine.py -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add nexus/campaigns/service.py nexus/cadences/service.py nexus/campaigns/schemas.py nexus/api/routers/campaigns.py nexus/api/routers/cadences.py nexus/api/routers/__init__.py tests/test_cadence_engine.py
git commit -m "feat(cadence): API router, campaign enroll-on-approve, cadence report"
```

### Task 13: Multi-tenant isolation, backward-compat, full-suite green

This final task proves two contracts the whole sub-project rests on: (1) the advance tick is
**tenant-isolated** — advancing tenant A's due enrollments never touches tenant B's; and (2) the
cadence engine is **purely additive** — a campaign with a NULL `cadence_id` still runs the
original single-shot send path and creates zero enrollments. Then it runs the entire suite to
prove nothing regressed.

**Files:**
- Test: `tests/test_cadence_engine.py`

- [ ] **Step 1: Write the failing tests**

Add these tests to `tests/test_cadence_engine.py`:

```python
async def test_advance_is_tenant_isolated():
    """Advancing one tenant's due enrollments must never reach into another tenant's.

    Two tenants each have one identical, immediately-due enrollment. We advance ONLY tenant
    A inside its own session; tenant B's enrollment must remain untouched (still active, zero
    touches). This is the per-tenant guarantee that ``handle_advance_cadences`` relies on when
    it loops tenant-by-tenant."""
    svc = get_cadence_service()
    tid_a = await make_tenant(slug="cad-iso-a", name="Iso A")
    tid_b = await make_tenant(slug="cad-iso-b", name="Iso B")

    async with tenant_session(tid_a) as ts_a:
        _, e_a, _, _ = await _enrollable(ts_a, NOW, steps=[{"delay_days": 0, "angle": "intro"}])
    async with tenant_session(tid_b) as ts_b:
        _, e_b, _, _ = await _enrollable(ts_b, NOW, steps=[{"delay_days": 0, "angle": "intro"}])

    # Advance only tenant A.
    async with tenant_session(tid_a) as ts_a:
        assert await svc.advance_due_for_tenant(ts_a, now=NOW, limit=100) == 1

    # Tenant A: the single touch sent and the enrollment completed.
    async with tenant_session(tid_a) as ts_a:
        ea = await ts_a.get(CadenceEnrollment, e_a.id)
        assert ea.status == ENROLL_COMPLETED
        touches_a = await ts_a.list(CadenceTouch, CadenceTouch.enrollment_id == e_a.id)
        assert [t.status for t in touches_a] == [TOUCH_SENT]

    # Tenant B: completely untouched — still active, no touches.
    async with tenant_session(tid_b) as ts_b:
        eb = await ts_b.get(CadenceEnrollment, e_b.id)
        assert eb.status == ENROLL_ACTIVE
        assert await ts_b.list(CadenceTouch, CadenceTouch.enrollment_id == e_b.id) == []


async def test_null_cadence_campaign_uses_single_send_path(ts):
    """Backward-compat: a campaign with no ``cadence_id`` still runs the original single-shot
    send path and creates ZERO enrollments. The cadence engine is purely additive — the
    pre-existing campaign behavior (and ``tests/test_campaign_engine.py``) is unaffected."""
    from nexus.models.campaign import CAMP_COMPLETED, TARGET_SENT
    from nexus.models.workflow import ListItem, ProspectList

    plist = ProspectList(tenant_id=ts.tenant_id, name="seg", filter={})
    ts.add(plist)
    await ts.flush()
    acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
    ts.add(acc)
    await ts.flush()
    ts.add(Contact(
        tenant_id=ts.tenant_id, account_id=acc.id, full_name="Lead", email="lead@acme.com"
    ))
    ts.add(ListItem(tenant_id=ts.tenant_id, list_id=plist.id, account_id=acc.id))
    await ts.flush()

    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="single", list_id=plist.id, icp={"industries": ["SaaS"]},
        sequence="ai-orchestrated-outbound", created_by_user_id="u1",
    )
    assert campaign.cadence_id is None  # default — no cadence wired
    await svc.run_draft_phase(ts, campaign)
    await svc.approve_and_send(ts, campaign, decided_by="u1")

    assert campaign.status == CAMP_COMPLETED
    targets = await svc.list_targets(ts, campaign.id)
    assert all(t.status == TARGET_SENT for t in targets if t.draft)
    # The additive cadence engine stayed out of the way: no enrollments were created.
    enrollments = await ts.list(
        CadenceEnrollment, CadenceEnrollment.campaign_id == campaign.id
    )
    assert enrollments == []
```

- [ ] **Step 2: Run the new tests to verify they pass**

These two tests exercise behavior already implemented in Tasks 7–12 (tenant-scoped advance
and the unchanged NULL-`cadence_id` branch in `approve_and_send`), so they should pass as
written — they are regression locks, not new behavior.

Run: `python -m pytest tests/test_cadence_engine.py -k "tenant_isolated or single_send_path" -v`
Expected: PASS

If either fails, the bug is in earlier-task code (not the test): a failing isolation test
means `advance_due_for_tenant` is not filtering by the session's tenant; a failing
backward-compat test means `approve_and_send`'s `cadence_id` branch (Task 12) is enrolling
when it should fall through to `run_send_phase`. Fix the implementing task's code, then re-run.

- [ ] **Step 3: Run the full cadence suite**

Run: `python -m pytest tests/test_cadence_engine.py -v`
Expected: PASS (every cadence test from Tasks 1–13).

- [ ] **Step 4: Run the full project suite to prove no regression**

Run: `python -m pytest -q`
Expected: PASS — the entire suite is green, including `tests/test_campaign_engine.py` (proving
the single-send path is unchanged) and `tests/test_campaign_sourcing.py`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_cadence_engine.py
git commit -m "test(cadence): multi-tenant isolation + NULL-cadence backward-compat locks"
```

---

## Self-Review Checklist (for the implementing agent)

Before declaring the plan done, confirm:
- Every `CadenceService` method referenced in a test (`create_cadence`, `list_steps`, `enroll`,
  `advance_due_for_tenant`, `pause`, `resume`, `stop`, `approve_touch`, `reject_touch`,
  `cadence_report`) is implemented by some task.
- The `ts` fixture is defined once (Task 7) and reused by Tasks 8, 10, 13.
- Model constants used in tests (`ENROLL_*`, `TOUCH_*`, `STOP_*`) all exist in
  `nexus/models/cadence.py` (Task 2/3).
- A NULL `Campaign.cadence_id` never enters the cadence path (Task 12 branch + Task 13 lock).


