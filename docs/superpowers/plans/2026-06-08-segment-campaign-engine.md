# Segment Campaign Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run discover→research→outreach across a saved segment (List) with a single campaign-level approval: autonomously draft a grounded email per account, let a human approve once, then send each draft through the existing send gates.

**Architecture:** A `Campaign` aggregate owns one `CampaignTarget` per account in the segment's List. A `CampaignService` drives two phases by reusing the existing orchestration engine: the **draft phase** creates+executes a new `research_compose` run (research→scoring→compose, no send, no per-run gate) per target and snapshots the resulting draft onto the target; the **send phase** (after one campaign-level approval) replays each target's draft through `SendMessageTool` verbatim, so the two hard gates (grounded-send, verified-send) live in exactly one place. Un-actionable accounts are classified, skipped, and rolled into a campaign report. A worker handler wraps the same service methods for off-request execution (the seam sub-project D will schedule).

**Tech Stack:** Python 3.11, FastAPI, async SQLAlchemy 2.0, Pydantic v2, Alembic, pytest (`asyncio_mode=auto`). Offline path: SQLite + stub LLM + in-memory queue, zero network.

---

## File Structure

**New files:**
- `nexus/models/campaign.py` — `Campaign` + `CampaignTarget` ORM models and their status/skip-reason constants. Owns the campaign data shape only.
- `nexus/campaigns/__init__.py` — package marker.
- `nexus/campaigns/service.py` — `CampaignService`: create, draft phase, approve, send phase, report roll-up, cancel. The one place campaign business logic lives.
- `nexus/campaigns/schemas.py` — Pydantic wire contracts (`CampaignIn`, `CampaignOut`, `CampaignTargetOut`, `CampaignPreviewOut`, `CampaignReportOut`).
- `nexus/api/routers/campaigns.py` — HTTP + SSE surface; thin, delegates to the service.
- `migrations/versions/0005_campaigns.py` — Alembic migration creating both tables (prod parity).
- `tests/test_campaign_engine.py` — the full offline test suite for this sub-project.

**Modified files:**
- `nexus/core/rbac.py` — add `manage_campaigns` permission (manager+).
- `nexus/orchestration/planner.py` — add `research_compose` recipe.
- `nexus/models/__init__.py` — register the two new models so `Base.metadata.create_all` picks them up.
- `nexus/api/routers/__init__.py` — register `campaigns.router`.
- `nexus/workers/tasks.py` — add `run_campaign` handler + `enqueue_run_campaign` helper.
- `nexus/core/config.py` — add `campaign_preview_sample: int = 3` setting.

**Out of scope (separate follow-on plan):** the React Campaigns UI. This plan is backend + tests only.

---

## Established patterns to follow (verified against the codebase)

- **TenantSession API:** `ts.tenant_id`, `ts.add(obj)`, `await ts.flush()`, `await ts.get(Model, id)`, `await ts.first(Model, *where)`, `await ts.list(Model, *where, limit=...)`, `ts.select(Model, *where)`, `await ts.session.scalars(stmt)`. Never bypass it.
- **Model base:** `class Foo(IdMixin, TimestampMixin, TenantScoped, Base)` — gives `id` (str pk), `created_at`/`updated_at`, `tenant_id`. See `nexus/models/workflow.py`.
- **Engine reuse:** `engine = get_orchestration_engine()`; `run = await engine.create_run(ts, goal, goal_input, created_by=..., account_id=...)`; `await engine.execute_run(ts, run)`. A run with no approval step drives straight to `RUN_COMPLETED`. The draft is left at `run.blackboard["draft"]` by `ComposeMessageTool`.
- **Send gate reuse:** `SendMessageTool().run(ToolContext(ts, runtime, run, inputs))` reads `tc.blackboard["draft"]`, raises `ToolError` on ungrounded/undeliverable. `runtime = get_agent_runtime()`.
- **Router gating:** `principal: Principal = Depends(require(Permission.manage_campaigns))`, `ts: TenantSession = Depends(get_tenant_session)`.
- **SSE:** mirror `nexus/api/routers/orchestration.py::_event_stream` + `stream_events` (poll inside `tenant_session`, `StreamingResponse(media_type="text/event-stream")`, auth via `get_principal` + `has_permission` because EventSource can't send headers).
- **Migration:** copy the structure of `migrations/versions/0004_outcomes.py`; `down_revision = "0004_outcomes"`.

---

## Task 1: Campaign + CampaignTarget models

**Files:**
- Create: `nexus/models/campaign.py`
- Modify: `nexus/models/__init__.py`
- Test: `tests/test_campaign_engine.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_campaign_engine.py`:

```python
"""Offline tests for the Segment Campaign Engine (sub-project A)."""
from __future__ import annotations

import pytest

from nexus.models.campaign import (
    Campaign,
    CampaignTarget,
    CAMP_DRAFT_PENDING,
    TARGET_PENDING,
)


def test_campaign_defaults():
    c = Campaign(
        tenant_id="t1",
        name="Q3 expansion",
        list_id="list1",
        icp={"industries": ["SaaS"]},
        sequence="ai-orchestrated-outbound",
        created_by_user_id="u1",
    )
    assert c.status == CAMP_DRAFT_PENDING
    assert c.report == {} or c.report is None


def test_campaign_target_defaults():
    t = CampaignTarget(tenant_id="t1", campaign_id="c1", account_id="a1")
    assert t.status == TARGET_PENDING
    assert t.skip_reason is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_campaign_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.models.campaign'`

- [ ] **Step 3: Write the models**

Create `nexus/models/campaign.py`:

```python
"""Segment Campaign Engine: a Campaign aggregates per-account outreach over a saved List.

A :class:`Campaign` targets a ProspectList; the service creates one :class:`CampaignTarget`
per account, drives an autonomous draft phase (research→score→compose, no send), parks the
whole campaign at a single human approval, then runs a send phase per approved target. All
tables are tenant-scoped — a campaign never reads or writes across tenant boundaries.
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin
from nexus.core.tenancy import TenantScoped

# Campaign lifecycle.
CAMP_DRAFT_PENDING = "draft_pending"        # created, targets not yet enumerated
CAMP_DRAFTING = "drafting"                  # draft phase running
CAMP_AWAITING_APPROVAL = "awaiting_approval"  # all targets resolved; waiting on human
CAMP_APPROVED = "approved"                  # human approved; send phase queued
CAMP_SENDING = "sending"                    # send phase running
CAMP_COMPLETED = "completed"                # all targets terminal
CAMP_CANCELLED = "cancelled"
CAMP_FAILED = "failed"
CAMP_TERMINAL = frozenset({CAMP_COMPLETED, CAMP_CANCELLED, CAMP_FAILED})

# CampaignTarget lifecycle.
TARGET_PENDING = "pending"
TARGET_DRAFTING = "drafting"
TARGET_DRAFTED = "drafted"
TARGET_SKIPPED = "skipped"        # un-actionable (see skip_reason); terminal
TARGET_APPROVED = "approved"
TARGET_SENT = "sent"              # terminal
TARGET_FAILED = "failed"          # unexpected error (see error); terminal
TARGET_TERMINAL = frozenset({TARGET_SKIPPED, TARGET_SENT, TARGET_FAILED})

# Skip reasons — the fixed contract sub-project B (Contact Sourcing) consumes.
SKIP_NO_CONTACT = "no_deliverable_contact"
SKIP_UNGROUNDED = "ungrounded_draft"
SKIP_UNDELIVERABLE = "undeliverable_address"
SKIP_RESEARCH_FAILED = "research_failed"


class Campaign(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "campaigns"
    __table_args__ = (Index("ix_campaign_tenant_status", "tenant_id", "status"),)

    name: Mapped[str] = mapped_column(String(200))
    list_id: Mapped[str] = mapped_column(ForeignKey("prospect_lists.id"), index=True)
    icp: Mapped[dict] = mapped_column(JSON, default=dict)
    sequence: Mapped[str] = mapped_column(String(120), default="ai-orchestrated-outbound")
    status: Mapped[str] = mapped_column(String(24), default=CAMP_DRAFT_PENDING, index=True)
    # Rolled-up counts: {"total","drafted","skipped","sent","failed","skips":{reason:count}}.
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class CampaignTarget(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "campaign_targets"
    __table_args__ = (Index("ix_camptarget_campaign_status", "campaign_id", "status"),)

    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    # The research_compose run that produced this target's draft (plain ref, nullable).
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=TARGET_PENDING, index=True)
    skip_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Snapshot copied off the run blackboard so approval UI + report survive the run.
    draft: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Register the models for create_all**

In `nexus/models/__init__.py`, add the import after the `outcome` import line:

```python
from nexus.models.campaign import Campaign, CampaignTarget
```

And add `"Campaign"` and `"CampaignTarget"` to the `__all__` list (after `"Outcome"`).

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_campaign_engine.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add nexus/models/campaign.py nexus/models/__init__.py tests/test_campaign_engine.py
git commit -m "feat(campaigns): add Campaign + CampaignTarget models"
```

---

## Task 2: Alembic migration 0005 (prod parity)

**Files:**
- Create: `migrations/versions/0005_campaigns.py`

`init_db()` uses `create_all` for local/test (picks the models up automatically), but production runs migrations — so the two tables need an explicit migration mirroring the models.

- [ ] **Step 1: Write the migration**

Create `migrations/versions/0005_campaigns.py`:

```python
"""Segment Campaign Engine: campaigns + campaign_targets tables.

Revision ID: 0005_campaigns
Revises: 0004_outcomes
Create Date: 2026-06-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_campaigns"
down_revision = "0004_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("list_id", sa.String(length=32),
                  sa.ForeignKey("prospect_lists.id"), nullable=False),
        sa.Column("icp", sa.JSON(), nullable=True),
        sa.Column("sequence", sa.String(length=120), nullable=False,
                  server_default="ai-orchestrated-outbound"),
        sa.Column("status", sa.String(length=24), nullable=False,
                  server_default="draft_pending"),
        sa.Column("report", sa.JSON(), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=32),
                  sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_campaigns_tenant_id", "campaigns", ["tenant_id"])
    op.create_index("ix_campaigns_list_id", "campaigns", ["list_id"])
    op.create_index("ix_campaign_tenant_status", "campaigns", ["tenant_id", "status"])

    op.create_table(
        "campaign_targets",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("campaign_id", sa.String(length=32),
                  sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("account_id", sa.String(length=32),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False,
                  server_default="pending"),
        sa.Column("skip_reason", sa.String(length=40), nullable=True),
        sa.Column("draft", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_campaign_targets_tenant_id", "campaign_targets", ["tenant_id"])
    op.create_index("ix_campaign_targets_campaign_id", "campaign_targets", ["campaign_id"])
    op.create_index("ix_campaign_targets_account_id", "campaign_targets", ["account_id"])
    op.create_index(
        "ix_camptarget_campaign_status", "campaign_targets", ["campaign_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_camptarget_campaign_status", table_name="campaign_targets")
    op.drop_index("ix_campaign_targets_account_id", table_name="campaign_targets")
    op.drop_index("ix_campaign_targets_campaign_id", table_name="campaign_targets")
    op.drop_index("ix_campaign_targets_tenant_id", table_name="campaign_targets")
    op.drop_table("campaign_targets")
    op.drop_index("ix_campaign_tenant_status", table_name="campaigns")
    op.drop_index("ix_campaigns_list_id", table_name="campaigns")
    op.drop_index("ix_campaigns_tenant_id", table_name="campaigns")
    op.drop_table("campaigns")
```

- [ ] **Step 2: Verify the migration imports cleanly**

Run: `python -c "import importlib.util, pathlib; spec = importlib.util.spec_from_file_location('m', 'migrations/versions/0005_campaigns.py'); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); print(mod.revision, '<-', mod.down_revision)"`
Expected: `0005_campaigns <- 0004_outcomes`

- [ ] **Step 3: Commit**

```bash
git add migrations/versions/0005_campaigns.py
git commit -m "feat(campaigns): add alembic migration for campaign tables"
```

---

## Task 3: `manage_campaigns` RBAC permission

**Files:**
- Modify: `nexus/core/rbac.py`
- Test: `tests/test_campaign_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_campaign_engine.py`:

```python
from nexus.core.rbac import Permission, Role, has_permission


def test_manage_campaigns_permission_is_manager_plus():
    assert has_permission(Role.manager, Permission.manage_campaigns) is True
    assert has_permission(Role.admin, Permission.manage_campaigns) is True
    assert has_permission(Role.owner, Permission.manage_campaigns) is True
    assert has_permission(Role.rep, Permission.manage_campaigns) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_campaign_engine.py::test_manage_campaigns_permission_is_manager_plus -v`
Expected: FAIL with `AttributeError: manage_campaigns` (no such enum member)

- [ ] **Step 3: Add the permission**

In `nexus/core/rbac.py`, add to the `Permission` enum after `approve_outreach`:

```python
    manage_campaigns = "manage_campaigns"      # manager+ (launches segment campaigns)
```

And add to the `_MIN_ROLE` dict after the `approve_outreach` entry:

```python
    Permission.manage_campaigns: Role.manager,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_campaign_engine.py::test_manage_campaigns_permission_is_manager_plus -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/core/rbac.py tests/test_campaign_engine.py
git commit -m "feat(campaigns): add manage_campaigns permission (manager+)"
```

---

## Task 4: `research_compose` planner recipe

**Files:**
- Modify: `nexus/orchestration/planner.py`
- Test: `tests/test_campaign_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_campaign_engine.py`:

```python
from nexus.orchestration.planner import available_goals, get_planner


def test_research_compose_recipe_has_no_send_step():
    assert "research_compose" in available_goals()
    plan = get_planner().plan("research_compose", {"account_id": "a1"})
    tools = [s["tool"] for s in plan]
    assert tools == ["research", "scoring", "compose_message"]
    # No step requires approval — the draft phase is fully autonomous.
    assert all(s["requires_approval"] is False for s in plan)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_campaign_engine.py::test_research_compose_recipe_has_no_send_step -v`
Expected: FAIL — `PlanError: Unknown goal 'research_compose'`

- [ ] **Step 3: Add the recipe**

In `nexus/orchestration/planner.py`, add this function after `_research_only_plan`:

```python
def _research_compose_plan(goal_input: dict) -> list[PlanStep]:
    """Draft-phase goal for the Segment Campaign Engine: research, score, and compose a
    grounded draft — but NO send and NO approval gate. The send happens later, once, in
    the campaign send phase, so the outbound gates stay in exactly one place."""
    return [
        PlanStep(idx=0, tool="research", depends_on=[]),
        PlanStep(idx=1, tool="scoring", depends_on=[0]),
        PlanStep(idx=2, tool="compose_message", depends_on=[1]),
    ]
```

And add it to `_RECIPES`:

```python
_RECIPES = {
    "research_account": _research_account_plan,
    "research_only": _research_only_plan,
    "research_compose": _research_compose_plan,
    "discover": _discover_plan,
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_campaign_engine.py::test_research_compose_recipe_has_no_send_step -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/orchestration/planner.py tests/test_campaign_engine.py
git commit -m "feat(campaigns): add research_compose draft-phase recipe"
```

---

## Task 5: Config setting for preview sample size

**Files:**
- Modify: `nexus/core/config.py`
- Test: `tests/test_campaign_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_campaign_engine.py`:

```python
from nexus.core.config import get_settings


def test_campaign_preview_sample_default():
    assert get_settings().campaign_preview_sample == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_campaign_engine.py::test_campaign_preview_sample_default -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'campaign_preview_sample'`

- [ ] **Step 3: Add the setting**

In `nexus/core/config.py`, add after the `discovery_max_candidates` line in the orchestration block:

```python
    campaign_preview_sample: int = 3          # drafted targets shown at the approval gate
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_campaign_engine.py::test_campaign_preview_sample_default -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/core/config.py tests/test_campaign_engine.py
git commit -m "feat(campaigns): add campaign_preview_sample setting"
```

---

## Task 6: CampaignService — create + draft phase

**Files:**
- Create: `nexus/campaigns/__init__.py`, `nexus/campaigns/service.py`
- Test: `tests/test_campaign_engine.py`

This is the heart of the draft phase: create the campaign, enumerate one target per account in the List, run `research_compose` per target, snapshot the draft, classify un-actionable targets, and roll up the report.

- [ ] **Step 1: Write the failing test**

The harness (`tests/conftest.py`, already verified) provides **no `ts` fixture** — tests obtain a `TenantSession` via `make_tenant()` + the `tenant_session(tid)` context manager (from `nexus.workers.tasks`, which commits on exit), and HTTP tests use the `client` fixture + `signup`/`auth` helpers. So this plan **defines local `ts`/`other_ts` fixtures at the top of the test file**. Add this block at the TOP of `tests/test_campaign_engine.py` (below the existing imports from Tasks 1–5):

```python
import pytest
import pytest_asyncio

from tests.conftest import make_tenant, signup, auth
from nexus.workers.tasks import tenant_session
from nexus.models.account import Account, Contact
from nexus.models.workflow import ListItem, ProspectList
from nexus.campaigns.service import get_campaign_service
from nexus.models.campaign import (
    CAMP_AWAITING_APPROVAL,
    TARGET_DRAFTED,
    TARGET_SKIPPED,
    SKIP_NO_CONTACT,
)


@pytest_asyncio.fixture
async def ts():
    """A TenantSession bound to a fresh tenant. The tenant_session context commits on exit."""
    tid = await make_tenant(slug="camp-a", name="Camp A")
    async with tenant_session(tid) as session:
        yield session


@pytest_asyncio.fixture
async def other_ts():
    """A second TenantSession bound to a different tenant (for isolation tests)."""
    tid = await make_tenant(slug="camp-b", name="Camp B")
    async with tenant_session(tid) as session:
        yield session


async def _make_list_with_accounts(ts, specs: list[dict]) -> str:
    """specs: [{"name","email"|None}]. Returns the list_id."""
    plist = ProspectList(tenant_id=ts.tenant_id, name="seg", filter={})
    ts.add(plist)
    await ts.flush()
    for spec in specs:
        acc = Account(tenant_id=ts.tenant_id, name=spec["name"], domain=spec["name"].lower() + ".com")
        ts.add(acc)
        await ts.flush()
        if spec.get("email"):
            ts.add(Contact(tenant_id=ts.tenant_id, account_id=acc.id, email=spec["email"], name="Lead"))
        ts.add(ListItem(tenant_id=ts.tenant_id, list_id=plist.id, account_id=acc.id))
    await ts.flush()
    return plist.id


@pytest.mark.asyncio
async def test_draft_phase_drafts_and_reports_skips(ts):
    list_id = await _make_list_with_accounts(
        ts,
        [
            {"name": "Acme", "email": "lead@acme.com"},
            {"name": "NoContactCo", "email": None},
        ],
    )
    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="Q3", list_id=list_id, icp={"industries": ["SaaS"]},
        sequence="ai-orchestrated-outbound", created_by_user_id="u1",
    )
    await svc.run_draft_phase(ts, campaign)

    assert campaign.status == CAMP_AWAITING_APPROVAL
    targets = await svc.list_targets(ts, campaign.id)
    by_account = {t.account_id: t for t in targets}
    statuses = sorted(t.status for t in targets)
    assert statuses == [TARGET_DRAFTED, TARGET_SKIPPED]
    skipped = next(t for t in targets if t.status == TARGET_SKIPPED)
    assert skipped.skip_reason == SKIP_NO_CONTACT
    # Report rolls up the counts.
    assert campaign.report["total"] == 2
    assert campaign.report["drafted"] == 1
    assert campaign.report["skipped"] == 1
    assert campaign.report["skips"][SKIP_NO_CONTACT] == 1


def test_classify_skip_reasons():
    """Pure unit test of the skip-reason classifier (no DB, no async).

    Priority order is ungrounded → no-contact → undeliverable; a fully grounded,
    deliverable draft returns None (it is sendable)."""
    from nexus.campaigns.service import CampaignService
    from nexus.models.campaign import (
        SKIP_NO_CONTACT,
        SKIP_UNDELIVERABLE,
        SKIP_UNGROUNDED,
    )
    from nexus.verification import STATUS_INVALID

    classify = CampaignService._classify
    # Ungrounded wins even if a contact/email is present.
    assert classify({"grounded": False, "contact_id": "c1", "email_status": "unknown"}) == SKIP_UNGROUNDED
    # Grounded but no contact, or no email to verify (email_status is None).
    assert classify({"grounded": True, "contact_id": None, "email_status": None}) == SKIP_NO_CONTACT
    assert classify({"grounded": True, "contact_id": "c1", "email_status": None}) == SKIP_NO_CONTACT
    # Grounded, has contact + a verified-invalid address.
    assert classify({"grounded": True, "contact_id": "c1", "email_status": STATUS_INVALID}) == SKIP_UNDELIVERABLE
    # Grounded, deliverable (unknown/valid) → sendable.
    assert classify({"grounded": True, "contact_id": "c1", "email_status": "unknown"}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_campaign_engine.py::test_draft_phase_drafts_and_reports_skips tests/test_campaign_engine.py::test_classify_skip_reasons -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.campaigns'`

- [ ] **Step 3: Create the package marker**

Create `nexus/campaigns/__init__.py`:

```python
"""Segment Campaign Engine: drive discover→research→outreach across a saved List."""
```

- [ ] **Step 4: Write the service (create + draft phase + report + skip classification)**

Create `nexus/campaigns/service.py`:

```python
"""CampaignService: the business logic for the Segment Campaign Engine.

Two phases, both reusing the orchestration engine:
- ``run_draft_phase``: per target, create+execute a ``research_compose`` run (no send),
  snapshot the draft, and classify un-actionable targets into the skip report.
- ``run_send_phase`` (Task 7): per approved target, replay the draft through
  ``SendMessageTool`` so the outbound hard gates fire exactly as they do for a single run.

Targets are processed independently with try/except isolation: one account's failure never
blocks the rest. The service mutates rows in the passed-in ``TenantSession`` and flushes;
the caller (API or worker) owns the commit.
"""
from __future__ import annotations

from nexus.agents.runtime import get_agent_runtime
from nexus.core.tenancy import TenantSession
from nexus.models.account import Contact
from nexus.models.campaign import (
    Campaign,
    CampaignTarget,
    CAMP_AWAITING_APPROVAL,
    CAMP_DRAFTING,
    CAMP_COMPLETED,
    CAMP_FAILED,
    TARGET_DRAFTED,
    TARGET_DRAFTING,
    TARGET_FAILED,
    TARGET_PENDING,
    TARGET_SENT,
    TARGET_SKIPPED,
    SKIP_NO_CONTACT,
    SKIP_RESEARCH_FAILED,
    SKIP_UNDELIVERABLE,
    SKIP_UNGROUNDED,
)
from nexus.models.orchestration import RUN_COMPLETED
from nexus.models.workflow import ListItem
from nexus.orchestration.engine import get_orchestration_engine
from nexus.verification import STATUS_INVALID


class CampaignService:
    async def create(
        self,
        ts: TenantSession,
        *,
        name: str,
        list_id: str,
        icp: dict,
        sequence: str,
        created_by_user_id: str | None,
    ) -> Campaign:
        """Create the campaign and one PENDING target per account in the List."""
        campaign = Campaign(
            tenant_id=ts.tenant_id,
            name=name,
            list_id=list_id,
            icp=icp or {},
            sequence=sequence or "ai-orchestrated-outbound",
            created_by_user_id=created_by_user_id,
        )
        ts.add(campaign)
        await ts.flush()

        items = await ts.list(ListItem, ListItem.list_id == list_id)
        # Dedupe accounts (a List could in principle carry an account twice).
        seen: set[str] = set()
        for item in items:
            if item.account_id in seen:
                continue
            seen.add(item.account_id)
            ts.add(
                CampaignTarget(
                    tenant_id=ts.tenant_id,
                    campaign_id=campaign.id,
                    account_id=item.account_id,
                    status=TARGET_PENDING,
                )
            )
        await ts.flush()
        return campaign

    async def list_targets(self, ts: TenantSession, campaign_id: str) -> list[CampaignTarget]:
        return await ts.list(CampaignTarget, CampaignTarget.campaign_id == campaign_id)

    async def run_draft_phase(self, ts: TenantSession, campaign: Campaign) -> Campaign:
        """Draft a grounded email for every PENDING target, then park for approval."""
        campaign.status = CAMP_DRAFTING
        await ts.flush()

        targets = await self.list_targets(ts, campaign.id)
        engine = get_orchestration_engine()
        runtime = get_agent_runtime()
        for target in targets:
            if target.status != TARGET_PENDING:
                continue
            await self._draft_one(ts, campaign, target, engine=engine, runtime=runtime)

        campaign.report = await self._build_report(ts, campaign)
        campaign.status = CAMP_AWAITING_APPROVAL
        await ts.flush()
        return campaign

    async def _draft_one(self, ts, campaign, target, *, engine, runtime) -> None:
        target.status = TARGET_DRAFTING
        await ts.flush()
        try:
            run = await engine.create_run(
                ts,
                "research_compose",
                {"account_id": target.account_id},
                account_id=target.account_id,
            )
            await engine.execute_run(ts, run, runtime=runtime)
            target.run_id = run.id

            if run.status != RUN_COMPLETED:
                target.status = TARGET_SKIPPED
                target.skip_reason = SKIP_RESEARCH_FAILED
                target.error = run.error
                await ts.flush()
                return

            draft = dict((run.blackboard or {}).get("draft") or {})
            target.draft = draft
            reason = self._classify(draft)
            if reason is None:
                target.status = TARGET_DRAFTED
            else:
                target.status = TARGET_SKIPPED
                target.skip_reason = reason
            await ts.flush()
        except Exception as exc:  # isolation: one bad target never blocks the rest
            target.status = TARGET_FAILED
            target.error = f"{type(exc).__name__}: {exc}"
            await ts.flush()

    @staticmethod
    def _classify(draft: dict) -> str | None:
        """Return the primary skip reason for a draft, or None if it is sendable.

        Priority (most fundamental blocker first): ungrounded → no contact → undeliverable.
        ``ComposeMessageTool`` writes ``grounded`` (bool), ``contact_id`` (str|None), and
        ``email_status`` (str|None — None means no email was present to verify)."""
        if not draft.get("grounded"):
            return SKIP_UNGROUNDED
        if not draft.get("contact_id") or draft.get("email_status") is None:
            return SKIP_NO_CONTACT
        if draft.get("email_status") == STATUS_INVALID:
            return SKIP_UNDELIVERABLE
        return None

    async def _build_report(self, ts: TenantSession, campaign: Campaign) -> dict:
        targets = await self.list_targets(ts, campaign.id)
        skips: dict[str, int] = {}
        drafted = sent = skipped = failed = 0
        for t in targets:
            if t.status == TARGET_DRAFTED:
                drafted += 1
            elif t.status == TARGET_SENT:
                sent += 1
            elif t.status == TARGET_SKIPPED:
                skipped += 1
                if t.skip_reason:
                    skips[t.skip_reason] = skips.get(t.skip_reason, 0) + 1
            elif t.status == TARGET_FAILED:
                failed += 1
        return {
            "total": len(targets),
            "drafted": drafted,
            "sent": sent,
            "skipped": skipped,
            "failed": failed,
            "skips": skips,
        }


_service = CampaignService()


def get_campaign_service() -> CampaignService:
    return _service
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_campaign_engine.py::test_draft_phase_drafts_and_reports_skips tests/test_campaign_engine.py::test_classify_skip_reasons -v`
Expected: PASS (2 passed). If the `ts` fixture name differs, adjust the test's fixture argument to match `tests/conftest.py` (do NOT change the service).

- [ ] **Step 6: Verify `STATUS_INVALID` import path**

Run: `python -c "from nexus.verification import STATUS_INVALID; print(STATUS_INVALID)"`
Expected: prints the invalid-status constant (e.g. `invalid`). This is the same import `nexus/orchestration/tools.py` uses, so it must resolve.

- [ ] **Step 7: Commit**

```bash
git add nexus/campaigns/__init__.py nexus/campaigns/service.py tests/test_campaign_engine.py
git commit -m "feat(campaigns): draft phase — research_compose per target + skip report"
```

---

## Task 7: CampaignService — approve + send phase

**Files:**
- Modify: `nexus/campaigns/service.py`
- Test: `tests/test_campaign_engine.py`

The send phase replays each DRAFTED target's draft through `SendMessageTool`, so the grounded-send and verified-send gates fire per account. The campaign-level approval has already happened (one human decision); the gates are the per-send safety net that a campaign approval cannot override.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_campaign_engine.py`:

```python
from nexus.models.campaign import (
    CAMP_COMPLETED,
    TARGET_SENT,
)


@pytest.mark.asyncio
async def test_send_phase_sends_drafted_targets(ts):
    list_id = await _make_list_with_accounts(ts, [{"name": "Acme", "email": "lead@acme.com"}])
    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="Q3", list_id=list_id, icp={"industries": ["SaaS"]},
        sequence="ai-orchestrated-outbound", created_by_user_id="u1",
    )
    await svc.run_draft_phase(ts, campaign)
    assert campaign.status == CAMP_AWAITING_APPROVAL

    await svc.approve_and_send(ts, campaign, decided_by="u1")

    assert campaign.status == CAMP_COMPLETED
    targets = await svc.list_targets(ts, campaign.id)
    assert all(t.status == TARGET_SENT for t in targets if t.draft)
    assert campaign.report["sent"] >= 1


@pytest.mark.asyncio
async def test_approve_requires_awaiting_state(ts):
    list_id = await _make_list_with_accounts(ts, [{"name": "Acme", "email": "lead@acme.com"}])
    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="Q3", list_id=list_id, icp={}, sequence="seq", created_by_user_id="u1",
    )
    # Not yet drafted → cannot approve.
    with pytest.raises(Exception):
        await svc.approve_and_send(ts, campaign, decided_by="u1")


@pytest.mark.asyncio
async def test_send_phase_gates_refuse_per_account(ts):
    """Campaign approval is ONE human decision, but it cannot override the per-send hard
    gates. A draft that lost grounding (e.g. an edit) or carries an invalid address is
    refused at the send boundary and lands in the report; the grounded survivor still sends.

    This deliberately mutates target draft snapshots to reach the send-phase gates, because
    the draft phase's own ``_classify`` would otherwise have filtered these cases out before
    they were ever marked DRAFTED — proving the gates are a genuine second line of defense.
    (``Account`` and ``Contact`` are imported at the top of this test module.)
    """
    from nexus.models.campaign import SKIP_UNDELIVERABLE, SKIP_UNGROUNDED

    list_id = await _make_list_with_accounts(
        ts,
        [
            {"name": "Acme", "email": "lead@acme.com"},      # stays grounded+deliverable → SENT
            {"name": "Globex", "email": "lead@globex.com"},  # grounding stripped → refused
            {"name": "Initech", "email": "lead@initech.com"},  # address forced invalid → refused
        ],
    )
    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="Q3", list_id=list_id, icp={}, sequence="seq", created_by_user_id="u1",
    )
    await svc.run_draft_phase(ts, campaign)
    assert campaign.status == CAMP_AWAITING_APPROVAL

    accounts = {a.name: a for a in await ts.list(Account)}
    targets = {t.account_id: t for t in await svc.list_targets(ts, campaign.id)}
    # All three drafted under the always-grounding stub.
    assert all(targets[a.id].status == TARGET_DRAFTED for a in accounts.values())

    # Strip grounding off Globex's snapshot; ``_send_one`` reasserts ``target.draft`` onto
    # the run blackboard, so the grounded-send gate reads grounded=False and refuses.
    g = targets[accounts["Globex"].id]
    g.draft = {**g.draft, "grounded": False}
    # The verified-send gate re-verifies the LIVE contact address (not the snapshot's cached
    # status), so an invalid verdict must come from the contact record. Break Initech's address.
    initech_contact = await ts.first(Contact, Contact.account_id == accounts["Initech"].id)
    initech_contact.email = "not-an-email"
    await ts.flush()

    await svc.approve_and_send(ts, campaign, decided_by="u1")

    assert campaign.status == CAMP_COMPLETED
    targets = {t.account_id: t for t in await svc.list_targets(ts, campaign.id)}
    assert targets[accounts["Acme"].id].status == TARGET_SENT
    assert targets[accounts["Globex"].id].status == TARGET_SKIPPED
    assert targets[accounts["Globex"].id].skip_reason == SKIP_UNGROUNDED
    assert targets[accounts["Initech"].id].status == TARGET_SKIPPED
    assert targets[accounts["Initech"].id].skip_reason == SKIP_UNDELIVERABLE
    assert campaign.report["sent"] == 1
    assert campaign.report["skips"][SKIP_UNGROUNDED] == 1
    assert campaign.report["skips"][SKIP_UNDELIVERABLE] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_campaign_engine.py::test_send_phase_sends_drafted_targets -v`
Expected: FAIL — `AttributeError: 'CampaignService' object has no attribute 'approve_and_send'`

- [ ] **Step 3: Add the send phase to the service**

Add these imports to the top of `nexus/campaigns/service.py` (alongside the existing imports):

```python
from nexus.models.campaign import (
    CAMP_APPROVED,
    CAMP_SENDING,
    TARGET_APPROVED,
)
from nexus.models.orchestration import OrchestrationRun
from nexus.orchestration.tools import SendMessageTool, ToolContext, ToolError
```

(`TARGET_SENT` is already imported in the Task 6 import block, so it is not re-imported here.)

Add a module-level error class near the top of the file (after the imports):

```python
class CampaignError(Exception):
    """Raised for an invalid campaign state transition (e.g. approving too early)."""
```

Add these methods to `CampaignService` (after `run_draft_phase`):

```python
    async def approve_and_send(
        self, ts: TenantSession, campaign: Campaign, *, decided_by: str | None
    ) -> Campaign:
        """Campaign-level approval: one human decision, then send every DRAFTED target."""
        if campaign.status != CAMP_AWAITING_APPROVAL:
            raise CampaignError(
                f"campaign must be awaiting_approval to approve, is '{campaign.status}'"
            )
        campaign.status = CAMP_APPROVED
        await ts.flush()
        return await self.run_send_phase(ts, campaign)

    async def run_send_phase(self, ts: TenantSession, campaign: Campaign) -> Campaign:
        campaign.status = CAMP_SENDING
        await ts.flush()

        targets = await self.list_targets(ts, campaign.id)
        tool = SendMessageTool()
        runtime = get_agent_runtime()
        for target in targets:
            if target.status != TARGET_DRAFTED:
                continue
            await self._send_one(ts, campaign, target, tool=tool, runtime=runtime)

        campaign.report = await self._build_report(ts, campaign)
        campaign.status = CAMP_COMPLETED
        await ts.flush()
        return campaign

    async def _send_one(self, ts, campaign, target, *, tool, runtime) -> None:
        target.status = TARGET_APPROVED
        await ts.flush()
        # Reuse the target's research_compose run as the ToolContext carrier; its blackboard
        # already holds the draft. Reassert the draft snapshot in case it was edited/rebuilt.
        run = await ts.get(OrchestrationRun, target.run_id) if target.run_id else None
        if run is None:
            target.status = TARGET_FAILED
            target.error = "missing draft run for send"
            await ts.flush()
            return
        run.blackboard = dict(run.blackboard or {})
        run.blackboard["draft"] = dict(target.draft or {})
        tc = ToolContext(
            ts=ts, runtime=runtime, run=run, inputs={"sequence": campaign.sequence}
        )
        try:
            await tool.run(tc)
            target.status = TARGET_SENT
            await ts.flush()
        except ToolError as exc:
            # A gate refused at the boundary. Classify into the report's skip reasons.
            msg = str(exc).lower()
            target.status = TARGET_SKIPPED
            if "ungrounded" in msg:
                target.skip_reason = SKIP_UNGROUNDED
            elif "undeliverable" in msg or "invalid" in msg:
                target.skip_reason = SKIP_UNDELIVERABLE
            else:
                target.skip_reason = SKIP_NO_CONTACT
            target.error = str(exc)
            await ts.flush()
        except Exception as exc:  # isolation: one bad send never blocks the rest
            target.status = TARGET_FAILED
            target.error = f"{type(exc).__name__}: {exc}"
            await ts.flush()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_campaign_engine.py::test_send_phase_sends_drafted_targets tests/test_campaign_engine.py::test_approve_requires_awaiting_state tests/test_campaign_engine.py::test_send_phase_gates_refuse_per_account -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add nexus/campaigns/service.py tests/test_campaign_engine.py
git commit -m "feat(campaigns): send phase — replay drafts through SendMessageTool gates"
```

---

## Task 8: Cancel + idempotent re-approve

**Files:**
- Modify: `nexus/campaigns/service.py`
- Test: `tests/test_campaign_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_campaign_engine.py`:

```python
from nexus.models.campaign import CAMP_CANCELLED


@pytest.mark.asyncio
async def test_cancel_before_send_marks_cancelled(ts):
    list_id = await _make_list_with_accounts(ts, [{"name": "Acme", "email": "lead@acme.com"}])
    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="Q3", list_id=list_id, icp={}, sequence="seq", created_by_user_id="u1",
    )
    await svc.run_draft_phase(ts, campaign)
    await svc.cancel(ts, campaign)
    assert campaign.status == CAMP_CANCELLED


@pytest.mark.asyncio
async def test_double_approve_does_not_resend(ts):
    list_id = await _make_list_with_accounts(ts, [{"name": "Acme", "email": "lead@acme.com"}])
    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="Q3", list_id=list_id, icp={}, sequence="seq", created_by_user_id="u1",
    )
    await svc.run_draft_phase(ts, campaign)
    await svc.approve_and_send(ts, campaign, decided_by="u1")
    first_sent = campaign.report["sent"]
    # Re-approving a completed campaign raises rather than re-sending.
    with pytest.raises(Exception):
        await svc.approve_and_send(ts, campaign, decided_by="u1")
    assert campaign.report["sent"] == first_sent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_campaign_engine.py::test_cancel_before_send_marks_cancelled -v`
Expected: FAIL — `AttributeError: 'CampaignService' object has no attribute 'cancel'`

- [ ] **Step 3: Add cancel to the service**

Add `CAMP_CANCELLED` and `CAMP_TERMINAL` to the campaign-model import block in `nexus/campaigns/service.py`, then add this method to `CampaignService`:

```python
    async def cancel(self, ts: TenantSession, campaign: Campaign) -> Campaign:
        """Cancel a campaign that has not finished. Pending/drafting/drafted targets that
        have not been sent are left as-is in the report; the campaign stops here."""
        if campaign.status in CAMP_TERMINAL:
            return campaign
        campaign.status = CAMP_CANCELLED
        campaign.report = await self._build_report(ts, campaign)
        await ts.flush()
        return campaign
```

The idempotent re-approve test already passes because `approve_and_send` raises `CampaignError` unless the campaign is `awaiting_approval` (a completed campaign is not), so no code change is needed for that test — it is covered by Task 7's guard.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_campaign_engine.py::test_cancel_before_send_marks_cancelled tests/test_campaign_engine.py::test_double_approve_does_not_resend -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add nexus/campaigns/service.py tests/test_campaign_engine.py
git commit -m "feat(campaigns): cancel + guard against double-send on re-approve"
```

---

## Task 9: Worker handler for off-request execution

**Files:**
- Modify: `nexus/workers/tasks.py`
- Test: `tests/test_campaign_engine.py`

Wraps the service phases as a worker job so a campaign can run off the request (and so sub-project D can schedule it). Mirrors `handle_run_orchestration`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_campaign_engine.py`:

```python
from nexus.workers.tasks import handle_run_campaign


@pytest.mark.asyncio
async def test_worker_run_campaign_draft_phase(ts):
    list_id = await _make_list_with_accounts(ts, [{"name": "Acme", "email": "lead@acme.com"}])
    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="Q3", list_id=list_id, icp={}, sequence="seq", created_by_user_id="u1",
    )
    await ts.session.commit()  # the worker opens its own session; persist first
    result = await handle_run_campaign(
        {"tenant_id": ts.tenant_id, "campaign_id": campaign.id, "phase": "draft"}
    )
    assert result["campaign_id"] == campaign.id
    assert result["status"] == CAMP_AWAITING_APPROVAL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_campaign_engine.py::test_worker_run_campaign_draft_phase -v`
Expected: FAIL — `ImportError: cannot import name 'handle_run_campaign'`

- [ ] **Step 3: Add the handler + enqueue helper**

In `nexus/workers/tasks.py`, add this handler after `handle_run_orchestration`:

```python
async def handle_run_campaign(payload: dict) -> dict:
    """Off-request campaign driver. ``phase`` selects the work:
    ``"draft"`` runs the draft phase; ``"send"`` runs the send phase. Idempotent — a
    campaign already past the requested phase is a no-op returning its current status."""
    tenant_id = payload["tenant_id"]
    campaign_id = payload["campaign_id"]
    phase = payload.get("phase", "draft")
    from nexus.models.campaign import Campaign, CAMP_AWAITING_APPROVAL, CAMP_TERMINAL
    from nexus.campaigns.service import get_campaign_service

    async with tenant_session(tenant_id) as ts:
        campaign = await ts.get(Campaign, campaign_id)
        if campaign is None:
            return {"error": "campaign_not_found", "campaign_id": campaign_id}
        svc = get_campaign_service()
        if phase == "draft" and campaign.status not in CAMP_TERMINAL \
                and campaign.status != CAMP_AWAITING_APPROVAL:
            await svc.run_draft_phase(ts, campaign)
        elif phase == "send" and campaign.status == "approved":
            await svc.run_send_phase(ts, campaign)
        return {"campaign_id": campaign.id, "status": campaign.status}
```

Register it in the `HANDLERS` dict:

```python
HANDLERS: dict[str, Handler] = {
    "process_account": handle_process_account,
    "run_orchestration": handle_run_orchestration,
    "run_campaign": handle_run_campaign,
}
```

Add the enqueue helper after `enqueue_run_orchestration`:

```python
async def enqueue_run_campaign(
    tenant_id: str, campaign_id: str, *, phase: str = "draft", queue: TaskQueue | None = None
) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(
        Job(
            name="run_campaign",
            payload={"tenant_id": tenant_id, "campaign_id": campaign_id, "phase": phase},
        )
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_campaign_engine.py::test_worker_run_campaign_draft_phase -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/workers/tasks.py tests/test_campaign_engine.py
git commit -m "feat(campaigns): worker handler + enqueue helper for off-request phases"
```

---

## Task 10: Pydantic wire schemas

**Files:**
- Create: `nexus/campaigns/schemas.py`
- Test: `tests/test_campaign_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_campaign_engine.py`:

```python
from nexus.campaigns.schemas import CampaignOut, CampaignTargetOut


@pytest.mark.asyncio
async def test_campaign_out_projection(ts):
    list_id = await _make_list_with_accounts(ts, [{"name": "Acme", "email": "lead@acme.com"}])
    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="Q3", list_id=list_id, icp={"x": 1}, sequence="seq", created_by_user_id="u1",
    )
    out = CampaignOut.from_model(campaign)
    assert out.id == campaign.id
    assert out.name == "Q3"
    assert out.status == campaign.status
    assert out.list_id == list_id

    targets = await svc.list_targets(ts, campaign.id)
    tout = CampaignTargetOut.from_model(targets[0])
    assert tout.account_id == targets[0].account_id
    assert tout.status == targets[0].status
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_campaign_engine.py::test_campaign_out_projection -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.campaigns.schemas'`

- [ ] **Step 3: Write the schemas**

Create `nexus/campaigns/schemas.py`:

```python
"""Pydantic wire contracts for the Segment Campaign Engine API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from nexus.models.campaign import Campaign, CampaignTarget


class CampaignIn(BaseModel):
    name: str = Field(..., max_length=200)
    list_id: str
    icp: dict = Field(default_factory=dict)
    sequence: str = Field(default="ai-orchestrated-outbound", max_length=120)


class CampaignTargetOut(BaseModel):
    id: str
    account_id: str
    status: str
    skip_reason: str | None = None
    draft: dict = Field(default_factory=dict)
    error: str | None = None

    @classmethod
    def from_model(cls, t: CampaignTarget) -> "CampaignTargetOut":
        return cls(
            id=t.id,
            account_id=t.account_id,
            status=t.status,
            skip_reason=t.skip_reason,
            draft=t.draft or {},
            error=t.error,
        )


class CampaignOut(BaseModel):
    id: str
    name: str
    list_id: str
    status: str
    sequence: str
    icp: dict = Field(default_factory=dict)
    report: dict = Field(default_factory=dict)
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
            created_at=c.created_at,
        )


class CampaignDetailOut(CampaignOut):
    targets: list[CampaignTargetOut] = Field(default_factory=list)

    @classmethod
    def from_models(
        cls, c: Campaign, targets: list[CampaignTarget]
    ) -> "CampaignDetailOut":
        base = CampaignOut.from_model(c)
        return cls(**base.model_dump(), targets=[CampaignTargetOut.from_model(t) for t in targets])


class CampaignPreviewOut(BaseModel):
    """The approval sample: a few drafted targets + the skip report."""

    campaign_id: str
    status: str
    report: dict = Field(default_factory=dict)
    sample: list[CampaignTargetOut] = Field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_campaign_engine.py::test_campaign_out_projection -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/campaigns/schemas.py tests/test_campaign_engine.py
git commit -m "feat(campaigns): pydantic wire schemas"
```

---

## Task 11: API router + registration

**Files:**
- Create: `nexus/api/routers/campaigns.py`
- Modify: `nexus/api/routers/__init__.py`
- Test: `tests/test_campaign_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_campaign_engine.py`. This is an HTTP-level test using the verified `client` + `signup` + `auth` helpers. `signup` provisions a tenant **owner** (owner ⊇ manage_campaigns), so the token passes the RBAC gate. The List must live in the **same tenant as the token**, so seed it via `tenant_session(tid)` where `tid` is the token's tenant claim. Add `from nexus.core.security import decode_access_token` to the test file's imports.

```python
@pytest.mark.asyncio
async def test_campaign_api_create_and_approve(client):
    token = await signup(client, slug="campco", email="owner@campco.com", company="CampCo")
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        list_id = await _make_list_with_accounts(ts, [{"name": "Acme", "email": "lead@acme.com"}])
    # tenant_session committed the List on block exit; now drive the API.

    resp = await client.post(
        "/api/campaigns",
        json={"name": "Q3", "list_id": list_id, "icp": {}, "sequence": "seq"},
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    cid = body["id"]
    assert body["status"] == "awaiting_approval"  # created + driven to draft inline

    preview = await client.get(f"/api/campaigns/{cid}/preview", headers=auth(token))
    assert preview.status_code == 200
    assert "sample" in preview.json()

    approve = await client.post(f"/api/campaigns/{cid}/approve", headers=auth(token))
    assert approve.status_code == 200
    assert approve.json()["status"] == "completed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_campaign_engine.py::test_campaign_api_create_and_approve -v`
Expected: FAIL — 404 (route not registered)

- [ ] **Step 3: Write the router**

Create `nexus/api/routers/campaigns.py`:

```python
"""Segment Campaign Engine endpoints: create a campaign over a List, review the draft
sample, approve once, and send. Create drives the draft phase inline to ``awaiting_approval``
for snappy feedback (the same inline pattern as orchestration run creation)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.campaigns.schemas import (
    CampaignDetailOut,
    CampaignIn,
    CampaignOut,
    CampaignPreviewOut,
    CampaignTargetOut,
)
from nexus.campaigns.service import CampaignError, get_campaign_service
from nexus.core.config import get_settings
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.campaign import Campaign, TARGET_DRAFTED
from nexus.models.workflow import ProspectList

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


async def _get_campaign(ts: TenantSession, campaign_id: str) -> Campaign:
    campaign = await ts.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return campaign


@router.post("", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_campaigns)),
) -> CampaignOut:
    plist = await ts.get(ProspectList, body.list_id)
    if plist is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "List not found")
    svc = get_campaign_service()
    campaign = await svc.create(
        ts,
        name=body.name,
        list_id=body.list_id,
        icp=body.icp,
        sequence=body.sequence,
        created_by_user_id=principal.user_id,
    )
    # Drive the draft phase inline to the approval gate.
    await svc.run_draft_phase(ts, campaign)
    return CampaignOut.from_model(campaign)


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> list[CampaignOut]:
    stmt = ts.select(Campaign).order_by(Campaign.created_at.desc()).limit(100)
    rows = list((await ts.session.scalars(stmt)).all())
    return [CampaignOut.from_model(c) for c in rows]


@router.get("/{campaign_id}", response_model=CampaignDetailOut)
async def get_campaign(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CampaignDetailOut:
    campaign = await _get_campaign(ts, campaign_id)
    targets = await get_campaign_service().list_targets(ts, campaign.id)
    return CampaignDetailOut.from_models(campaign, targets)


@router.get("/{campaign_id}/preview", response_model=CampaignPreviewOut)
async def preview_campaign(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CampaignPreviewOut:
    campaign = await _get_campaign(ts, campaign_id)
    targets = await get_campaign_service().list_targets(ts, campaign.id)
    sample_n = get_settings().campaign_preview_sample
    drafted = [t for t in targets if t.status == TARGET_DRAFTED][:sample_n]
    return CampaignPreviewOut(
        campaign_id=campaign.id,
        status=campaign.status,
        report=campaign.report or {},
        sample=[CampaignTargetOut.from_model(t) for t in drafted],
    )


@router.post("/{campaign_id}/approve", response_model=CampaignOut)
async def approve_campaign(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_campaigns)),
) -> CampaignOut:
    campaign = await _get_campaign(ts, campaign_id)
    try:
        await get_campaign_service().approve_and_send(
            ts, campaign, decided_by=principal.user_id
        )
    except CampaignError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return CampaignOut.from_model(campaign)


@router.post("/{campaign_id}/cancel", response_model=CampaignOut)
async def cancel_campaign(
    campaign_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_campaigns)),
) -> CampaignOut:
    campaign = await _get_campaign(ts, campaign_id)
    await get_campaign_service().cancel(ts, campaign)
    return CampaignOut.from_model(campaign)
```

- [ ] **Step 4: Register the router**

In `nexus/api/routers/__init__.py`: add `campaigns` to the import tuple (alphabetically, after `auth`), and add `campaigns.router` to the `all_routers` list (after `workflow.router`).

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_campaign_engine.py::test_campaign_api_create_and_approve -v`
Expected: PASS. If `client` fixture differs, adapt to the conftest's authed-client fixture (manager role or above).

- [ ] **Step 6: Commit**

```bash
git add nexus/api/routers/campaigns.py nexus/api/routers/__init__.py tests/test_campaign_engine.py
git commit -m "feat(campaigns): REST API (create/list/detail/preview/approve/cancel)"
```

---

## Task 12: Campaign progress SSE

**Files:**
- Modify: `nexus/api/routers/campaigns.py`
- Test: `tests/test_campaign_engine.py`

v1 derives progress from polling campaign + target status server-side (no new durable event table). The stream emits a status snapshot whenever counts change and ends when the campaign reaches a terminal or `awaiting_approval` state. This satisfies "live progress without client polling" and is the seam sub-project E builds on; a durable campaign-event log can come later if richer replay is needed.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_campaign_engine.py`:

```python
@pytest.mark.asyncio
async def test_campaign_events_stream_smoke(client):
    token = await signup(client, slug="campsse", email="owner@campsse.com", company="CampSSE")
    tid = decode_access_token(token)["tid"]
    async with tenant_session(tid) as ts:
        list_id = await _make_list_with_accounts(ts, [{"name": "Acme", "email": "lead@acme.com"}])
    resp = await client.post(
        "/api/campaigns",
        json={"name": "Q3", "list_id": list_id, "icp": {}, "sequence": "seq"},
        headers=auth(token),
    )
    cid = resp.json()["id"]
    # The campaign is already awaiting_approval, so the stream replays a snapshot and closes.
    async with client.stream("GET", f"/api/campaigns/{cid}/events", headers=auth(token)) as s:
        assert s.status_code == 200
        assert "text/event-stream" in s.headers["content-type"]
        body = b""
        async for chunk in s.aiter_bytes():
            body += chunk
        assert b"awaiting_approval" in body or b"progress" in body
```

`httpx.AsyncClient` (the `client` fixture's type) supports `.stream`. If a future harness change drops it, fall back to asserting a plain `GET` returns `200` with `content-type: text/event-stream`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_campaign_engine.py::test_campaign_events_stream_smoke -v`
Expected: FAIL — 404 (events route not registered)

- [ ] **Step 3: Add the SSE endpoint**

Add to the top imports of `nexus/api/routers/campaigns.py`:

```python
import asyncio
import json
from typing import AsyncIterator

from fastapi import Header, Request
from fastapi.responses import StreamingResponse

from nexus.api.deps import get_principal
from nexus.core.rbac import has_permission
from nexus.models.campaign import CAMP_AWAITING_APPROVAL, CAMP_TERMINAL, CampaignTarget
from nexus.workers.tasks import tenant_session
```

Add the stream helpers and endpoint at the end of the file:

```python
def _format_sse(seq: int, type_: str, data: dict) -> str:
    return f"id: {seq}\nevent: {type_}\ndata: {json.dumps(data)}\n\n"


async def _counts(ts: TenantSession, campaign_id: str) -> dict[str, int]:
    targets = await ts.list(CampaignTarget, CampaignTarget.campaign_id == campaign_id)
    counts: dict[str, int] = {}
    for t in targets:
        counts[t.status] = counts.get(t.status, 0) + 1
    return counts


async def _campaign_stream(
    tenant_id: str, campaign_id: str, request: Request
) -> AsyncIterator[str]:
    seq = 0
    last_snapshot: tuple | None = None
    for _ in range(600):  # ~5 min ceiling
        if await request.is_disconnected():
            return
        async with tenant_session(tenant_id) as ts:
            campaign = await ts.get(Campaign, campaign_id)
            if campaign is None:
                yield _format_sse(seq, "error", {"detail": "campaign not found"})
                return
            counts = await _counts(ts, campaign_id)
            campaign_status = campaign.status
            report = campaign.report or {}
        snapshot = (campaign_status, tuple(sorted(counts.items())))
        if snapshot != last_snapshot:
            seq += 1
            yield _format_sse(
                seq, "progress",
                {"status": campaign_status, "counts": counts, "report": report},
            )
            last_snapshot = snapshot
        if campaign_status in CAMP_TERMINAL or campaign_status == CAMP_AWAITING_APPROVAL:
            return
        await asyncio.sleep(0.5)


@router.get("/{campaign_id}/events")
async def stream_campaign_events(
    campaign_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    principal: Principal = Depends(get_principal),
) -> StreamingResponse:
    # EventSource can't set Authorization headers, so gate explicitly (mirrors runs SSE).
    if not has_permission(principal.role, Permission.manage_campaigns):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "lacks manage_campaigns")
    return StreamingResponse(
        _campaign_stream(principal.tenant_id, campaign_id, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_campaign_engine.py::test_campaign_events_stream_smoke -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add nexus/api/routers/campaigns.py tests/test_campaign_engine.py
git commit -m "feat(campaigns): SSE progress stream"
```

---

## Task 13: Multi-tenant isolation test + full suite green

**Files:**
- Test: `tests/test_campaign_engine.py`

- [ ] **Step 1: Write the isolation test**

Append to `tests/test_campaign_engine.py`. Uses the `ts` and `other_ts` fixtures defined in Task 6 (two distinct tenants). `TenantSession.get` returns `None` when the row's `tenant_id` doesn't match the session's (verified in `nexus/core/tenancy.py`), so a tenant-B session cannot see a tenant-A campaign.

```python
@pytest.mark.asyncio
async def test_campaign_invisible_across_tenants(ts, other_ts):
    """A campaign in tenant A must not be visible from tenant B's session."""
    list_id = await _make_list_with_accounts(ts, [{"name": "Acme", "email": "lead@acme.com"}])
    svc = get_campaign_service()
    campaign = await svc.create(
        ts, name="Q3", list_id=list_id, icp={}, sequence="seq", created_by_user_id="u1",
    )
    await ts.session.commit()
    # other_ts is bound to a different tenant_id.
    from nexus.models.campaign import Campaign
    found = await other_ts.get(Campaign, campaign.id)
    assert found is None
```

- [ ] **Step 2: Run the isolation test**

Run: `pytest tests/test_campaign_engine.py::test_campaign_invisible_across_tenants -v`
Expected: PASS (RLS + tenant-scoped queries make the row invisible)

- [ ] **Step 3: Run the whole campaign suite**

Run: `pytest tests/test_campaign_engine.py -v`
Expected: all tests PASS

- [ ] **Step 4: Run the FULL suite to confirm zero regressions**

Run: `pytest -q`
Expected: all tests pass (the prior green baseline was 185 passed). The new file adds tests; total count rises, zero failures.

- [ ] **Step 5: Commit**

```bash
git add tests/test_campaign_engine.py
git commit -m "test(campaigns): multi-tenant isolation + suite green"
```

---

## Self-Review (completed during planning)

**1. Spec coverage:**
- Data model (`Campaign`, `CampaignTarget` + statuses) → Task 1; migration → Task 2.
- `research_compose` recipe → Task 4.
- Two-phase execution + background worker → Tasks 6 (draft), 7 (send), 9 (worker).
- Campaign-level approval gate + preview sample → Tasks 7 (approve), 5 (sample size config), 11 (`/preview`).
- Per-account isolation / skip-and-report → Tasks 6 (`_draft_one` try/except + `_classify` + `_build_report`), 7 (`_send_one` try/except).
- API + RBAC → Task 3 (permission), 10 (schemas), 11 (router).
- Event/SSE surfacing → Task 12.
- Testing strategy (offline, stub LLM, zero network) — every spec bullet is covered:
  - recipe validation → `test_research_compose_recipe_has_no_send_step` (Task 4).
  - skip-reason classification (unit) → `test_classify_skip_reasons` (Task 6).
  - target state-machine / draft integration → `test_draft_phase_drafts_and_reports_skips` (Task 6).
  - approval → send-phase hard gates fire → `test_send_phase_gates_refuse_per_account` (Task 7): a draft stripped of grounding is refused (`SKIP_UNGROUNDED`), a draft whose live contact address is invalid is refused (`SKIP_UNDELIVERABLE`), and the grounded+deliverable survivor is `SENT`.
  - idempotency / no double-send → `test_double_approve_does_not_resend` (Task 8).
  - multi-tenant isolation → `test_campaign_invisible_across_tenants` (Task 13).
- Out of scope confirmed: CRM (#5), contact sourcing (#4), channels (#6), scheduling (#2), live dashboard UI (#3), per-draft editing.

**2. Placeholder scan:** No TBD/TODO; every code step shows full code. The test file defines its own `ts`/`other_ts` fixtures (via `make_tenant` + `tenant_session`) and seeds Lists with `_make_list_with_accounts`, because `tests/conftest.py` (verified) provides only `client`/`signup`/`auth`/`make_tenant`/`tenant_session` — not a session fixture. HTTP tests (Tasks 11, 12) use `signup` to mint an owner token (owner holds `manage_campaigns`) and seed the List in that token's tenant.

**3. Offline grounding is guaranteed (resolved during review):** the draft phase produces genuinely grounded drafts with zero network because `StubResearchProvider.research()` (the default `NEXUS_RESEARCH_PROVIDER=stub`) returns `found=True` with templated highlights for any company — `ComposeMessageTool` sets `grounded = bool(facts)`, so a List account always drafts grounded. A syntactically-valid email verifies to `"unknown"` (passes both gates); a malformed address verifies to `STATUS_INVALID`. Scoring and messaging tolerate a missing `RelevanceProfile` (scoring defaults `icp_fit=50`; messaging defaults the value-prop and handles an empty contact list), so the `research_compose` run completes without one. The draft-phase test therefore needs no `RelevanceProfile` seed; the send-phase gate test reaches the gates by deliberately mutating drafted snapshots (the gates are a genuine second line of defense behind `_classify`).

**4. Type consistency:** Status constants (`CAMP_*`, `TARGET_*`, `SKIP_*`) are defined once in Task 1 and imported by name everywhere; `TARGET_SENT` is imported in the Task 6 service import block and used by `_build_report` (no string literals). Service method names are stable across tasks: `create`, `run_draft_phase`, `_draft_one`, `_classify`, `_build_report`, `approve_and_send`, `run_send_phase`, `_send_one`, `cancel`, `list_targets`. `CampaignError` defined in Task 7, imported by the router in Task 11. Schema classes (`CampaignIn/Out/DetailOut/TargetOut/PreviewOut`) defined in Task 10, used in Task 11. Worker `handle_run_campaign` + `enqueue_run_campaign` defined in Task 9.
