# Phase 05: Training & Insights Ledger — Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every workspace decides once whether its data helps improve the AI, and for those that say yes, `ledger.emit()` records what the product does — AI calls, charges, signals, scores, enrichment, outcomes, calls, audited actions and failed jobs — into a transactional outbox that phase 06 ships to the three stores.

**Architecture:** `nexus/engagement/ledger/` holds three small modules: `consent.py` (append-only decisions, newest in force, cached per session), `envelope.py` (one JSON shape, a closed registry of event types, hard size caps) and `emit.py` (the single call site contract: same transaction as the action, consent-gated, kill-switched, and never raising into the caller). Sign-up records the choice in the transaction that creates the tenant; an existing workspace is asked once by a prompt in the app shell; Settings can switch it off, which deletes what is still in the outbox. Eleven existing seams each gain one `emit()` call inside their existing transaction.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0 (`begin_nested` savepoints), Pydantic v2, Prometheus counters, React 18 + TypeScript strict + CSS Modules.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §18.1, §18.6, §13, D24. **Depends on:** phase 01 (`training_consents`, `ledger_outbox`, migration `0057`), phase 02 (the ENGAGEMENT runtime group), phase 04 (the suppression seams).

**Verified:** the code in this plan was applied to a tree holding phases 01–05 and the WHOLE suite was run in the CI image: **3,579 passed** with `ruff check nexus tests` clean and `npm run typecheck` clean. That run is also what found the three plan corrections committed alongside this file (the Gmail push token, the provider-key count, the CRM scheduler set) — see [00-roadmap.md](00-roadmap.md).

---

## What this phase must not break

`emit()` is called from the hottest paths in the product — metering, audit, ingestion, scoring, enrichment. Four rules make that safe, and every one of them is a test in this plan:

1. **It never raises into the caller.** Every failure is logged, counted and swallowed, and the insert runs inside a `begin_nested()` savepoint so a failed insert cannot poison the caller's transaction either. The same rule `record_audit` and `metered()` already follow.
2. **It writes nothing without consent.** `pending` (never asked) and `off` both record nothing and buffer nothing.
3. **It is stoppable.** `ledger_capture_enabled` in the Control plane stops collection for every workspace at once.
4. **It shares the action's transaction.** The event exists exactly when the action committed, and disappears with a rollback — an event describing something that did not happen would poison a training set.

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Create | `nexus/engagement/ledger/__init__.py` | package |
| Create | `nexus/engagement/ledger/consent.py` | append-only decisions, per-session cache, sign-up decision |
| Create | `nexus/engagement/ledger/envelope.py` | the envelope, the closed event registry, size caps |
| Create | `nexus/engagement/ledger/emit.py` | the one call that records an event |
| Create | `nexus/api/routers/engagement_settings.py` | consent API + the confidence settings (D23) |
| Modify | `nexus/api/routers/__init__.py` | register the router |
| Modify | `nexus/core/config.py`, `nexus/runtime_config/catalog.py` | `ledger_capture_enabled` + its Control-plane switch |
| Modify | `nexus/core/metrics.py` | `nexus_ledger_events_total` |
| Modify | `nexus/api/schemas.py`, `nexus/api/routers/auth.py`, `nexus/auth/registration.py` | the sign-up choice, through the OTP path too |
| Modify | `nexus/agents/runtime.py`, `nexus/billing/meter.py`, `nexus/orchestration/engine.py`, `nexus/outcomes/service.py`, `nexus/calling/service.py`, `nexus/ingestion/service.py`, `nexus/agents/scoring.py`, `nexus/enrichment/waterfall.py`, `nexus/enrichment/account.py`, `nexus/core/audit.py`, `nexus/engagement/suppression/service.py` | one `emit()` each |
| Modify | `nexus/workers/tasks.py` | `error.job_failed` for a tenant job that raised |
| Modify | `tests/test_b2b_enrichment_actor.py` | two structural guards now read `_enrich` |
| Create | `frontend/src/pages/DataUsePage.tsx` (+ `.module.css`) | public "what is collected" page |
| Create | `frontend/src/components/engagement/ConsentPrompt.tsx` (+ `.module.css`) | the one-time prompt |
| Create | `frontend/src/pages/settings/TrainingConsentCard.tsx` (+ `.module.css`) | the switch in Settings |
| Modify | `frontend/src/App.tsx`, `frontend/src/components/layout/AppShell.tsx`, `frontend/src/pages/LoginPage.tsx` (+ `.module.css`), `frontend/src/pages/SettingsPage.tsx`, `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts` | route, prompt, sign-up checkbox, client |
| Create | `tests/test_engagement_ledger_capture.py` | envelope, consent, emit, sign-up, API, seams, UI |

---

### Task 1: The consent record

**Files:**
- Create: `nexus/engagement/ledger/__init__.py`, `nexus/engagement/ledger/consent.py`
- Test: `tests/test_engagement_ledger_capture.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_engagement_ledger_capture.py` with the module docstring, the imports, `_consented_tenant`, `_outbox` and `test_nothing_is_recorded_before_a_workspace_decides_or_after_it_says_no` from the final file (Task 9 Step 1). The test needs `emit`, which Task 3 writes; it fails for the right reason in both tasks.

- [ ] **Step 2: Run to see it fail**

Run: `pytest tests/test_engagement_ledger_capture.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.ledger'`

- [ ] **Step 3: Implement**

`nexus/engagement/ledger/__init__.py`:

```python
"""Training & insights ledger (spec §18)."""
```

`nexus/engagement/ledger/consent.py`:

```python
"""Whether a workspace contributes to the training & insights ledger (D24, spec §18.6).

**Append-only decisions; the newest is in force.** A workspace with no row has never been asked —
that is every workspace created before the ledger — and reads ``pending``: nothing is collected until
an owner or admin answers the one-time prompt. New workspaces record their sign-up choice in the same
transaction that creates them, so they are never ``pending``.

**Read once per transaction.** ``status`` caches the answer on the SQLAlchemy session, so an action
that emits ten events costs one query. A decision recorded through ``record`` clears that cache, and
every other request or job opens its own session, so switching off takes effect for the very next
transaction anywhere.
"""
from __future__ import annotations

from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.ledger import CONSENT_SOURCES, TrainingConsent

#: Bumped when what is collected, or how it is used, changes. Stored on every decision and on every
#: training example, so data collected under older terms can be told apart.
TERMS_VERSION = "2026-09-17"
_CACHE_KEY = "ledger_consent_status"


async def latest(ts: TenantSession) -> TrainingConsent | None:
    stmt = (ts.select(TrainingConsent)
            .order_by(TrainingConsent.decided_at.desc(), TrainingConsent.created_at.desc())
            .limit(1))
    return (await ts.session.scalars(stmt)).first()


async def status(ts: TenantSession) -> str:
    """``on``, ``off`` or ``pending``. Never raises: an unreadable answer is ``pending``."""
    cached = ts.session.info.get(_CACHE_KEY)
    if cached is not None:
        return cached
    try:
        row = await latest(ts)
    except Exception:
        return "pending"
    value = row.status if row is not None else "pending"
    ts.session.info[_CACHE_KEY] = value
    return value


async def record(ts: TenantSession, *, status_value: str, source: str,
                 user_id: str | None) -> TrainingConsent:
    if status_value not in ("on", "off"):
        raise ValueError("choose on or off")
    if source not in CONSENT_SOURCES:
        raise ValueError(f"unknown consent source {source!r}")
    row = TrainingConsent(status=status_value, source=source, terms_version=TERMS_VERSION,
                          decided_by_user_id=user_id, decided_at=utcnow())
    ts.add(row)
    await ts.flush()
    ts.session.info.pop(_CACHE_KEY, None)
    return row


def add_signup_decision(session, *, tenant_id: str, user_id: str, opted_in: bool) -> None:
    """Stage the sign-up choice on a raw session inside the tenant-creating transaction.

    Sign-up has no ``TenantSession`` yet, so the row names its tenant explicitly; the flush guard
    accepts a row that already carries one."""
    session.add(TrainingConsent(
        tenant_id=tenant_id, status="on" if opted_in else "off", source="signup",
        terms_version=TERMS_VERSION, decided_by_user_id=user_id, decided_at=utcnow(),
    ))
```

- [ ] **Step 4: Run to see how far it gets**

Run: `pytest tests/test_engagement_ledger_capture.py -n0 -q`
Expected: still FAIL, now on `No module named 'nexus.engagement.ledger.emit'` — the consent half imports cleanly.

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/ledger tests/test_engagement_ledger_capture.py
git commit -m "feat(ledger): append-only training consent, newest decision in force"
```

---

### Task 2: The envelope, the kill switch and the counter

**Files:**
- Create: `nexus/engagement/ledger/envelope.py`
- Modify: `nexus/core/config.py`, `nexus/runtime_config/catalog.py`, `nexus/core/metrics.py`
- Test: `tests/test_engagement_ledger_capture.py`

- [ ] **Step 1: Write the failing test**

Add `test_the_envelope_is_json_safe_bounded_and_self_correlated` from the final file.

- [ ] **Step 2: Run to see it fail**

Run: `pytest tests/test_engagement_ledger_capture.py::test_the_envelope_is_json_safe_bounded_and_self_correlated -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.ledger.envelope'`

- [ ] **Step 3: Implement**

`nexus/engagement/ledger/envelope.py`:

```python
"""The ledger event envelope (spec §18.1): one JSON shape for every event, schema-versioned.

The event types are a closed list. A typo in an ``emit`` call is dropped and counted rather than
creating a new family nobody's dataset builder reads, which is how a training set quietly loses
the events it was built for.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

SCHEMA_VERSION = 1

EVENT_TYPES: frozenset[str] = frozenset({
    # AI
    "ai.call", "research.completed",
    # drafting and review (phase 08)
    "draft.created", "draft.edited", "draft.approved", "draft.rejected",
    # sending (phase 07)
    "message.sent", "message.bounced",
    # replies (phases 09-10)
    "reply.received", "reply.classified", "reply.corrected", "reply.decided",
    "response.drafted", "response.sent",
    # enrollments (phase 08)
    "enrollment.started", "enrollment.paused", "enrollment.snoozed", "enrollment.resumed",
    "enrollment.stopped", "enrollment.completed",
    # the rest of the product (this phase)
    "call.disposition", "signal.ingested", "account.scored", "enrichment.account",
    "enrichment.contact", "outcome.recorded", "credits.charged", "workflow.run_finished",
    "audit.action", "contact.suppressed", "contact.unsuppressed", "consent.changed",
    "error.job_failed",
})

#: Per-string cap. A research brief or a transcript is kept; a scraped page is not.
MAX_TEXT = 20_000
#: Whole-payload cap after the per-string cap. Beyond it the payload is replaced by a marker, so one
#: pathological event cannot bloat the outbox or a store batch.
MAX_PAYLOAD_BYTES = 256_000


def _bounded(value, depth: int = 0):
    if depth > 8:
        return "[nested too deep]"
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…[truncated]"
    if isinstance(value, dict):
        return {str(k): _bounded(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bounded(v, depth + 1) for v in list(value)[:500]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def app_version() -> str:
    return os.environ.get("NEXUS_APP_VERSION", "")


def build(
    *, event_id: str, event_type: str, tenant_id: str, occurred_at: datetime,
    actor_user_id: str | None = None, actor_role: str | None = None, refs: dict | None = None,
    correlation_id: str | None = None, causation_id: str | None = None, model: str | None = None,
    prompt_version: str | None = None, flags: dict | None = None, payload: dict | None = None,
) -> dict:
    """A JSON-safe envelope. Pure: the same inputs build the same dict."""
    body = _bounded(payload or {})
    if len(json.dumps(body, default=str)) > MAX_PAYLOAD_BYTES:
        body = {"truncated": True, "reason": "payload over the ledger size cap"}
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": SCHEMA_VERSION,
        "occurred_at": occurred_at.isoformat(),
        "tenant_id": tenant_id,
        "actor": {"user_id": actor_user_id, "role": actor_role},
        "refs": {k: v for k, v in (refs or {}).items() if v},
        "chain": {"correlation_id": correlation_id or event_id, "causation_id": causation_id},
        "context": {"app_version": app_version(), "model": model or "",
                    "prompt_version": prompt_version or "", "flags": flags or {}},
        "payload": body,
    }
```

In `nexus/core/config.py`, after `ledger_pseudonym_secret: str = ""` (added in phase 02):

```python
    # Kill switch for ledger capture on every workspace, whatever each one consented to.
    ledger_capture_enabled: bool = True
```

In `nexus/runtime_config/catalog.py`, immediately after the `engagement_campaigns_enabled` `SettingSpec` (the one whose `risk="high"`), add:

```python
    SettingSpec(
        key="ledger_capture_enabled", label="Training ledger capture", group=ENGAGEMENT,
        kind="bool",
        effect="Records server-side activity from workspaces that opted in, for model training "
               "and prospect insights. Off stops recording for every workspace at once.",
        warning="Activity while it is off is never recorded and cannot be recovered later; "
                "training sets and insights have a gap for that period.",
        risk="medium",
    ),
```

In `nexus/core/metrics.py`, directly above `def record_webhook_event(...)`:

```python
def record_ledger_event(event_type: str, outcome: str) -> None:
    """Ledger emits by type and outcome (``recorded`` | ``dropped`` | ``failed``). Event types are
    a closed registry, so the label set is bounded."""
    _observe(
        _counter(
            "nexus_ledger_events_total",
            "Training & insights ledger events by type and outcome. `failed` means an action went "
            "ahead without its event; `dropped` means an unregistered event type.",
            ("event_type", "outcome"),
        ),
        {"event_type": event_type, "outcome": outcome},
        inc=1,
    )
```

> The label set is bounded because `EVENT_TYPES` is closed — the rule `workers/metrics.py` states for job names, which are derived from user input and therefore stay unlabelled.

- [ ] **Step 4: Run to see it pass**

Run: `pytest tests/test_engagement_ledger_capture.py::test_the_envelope_is_json_safe_bounded_and_self_correlated tests/test_runtime_control_plane.py tests/test_runtime_config.py -n0 -q`
Expected: all pass. `test_runtime_control_plane.py` is the structural guard that every catalog key is read outside `config.py` — `ledger_capture_enabled` is read by `emit()` in the next task, so run it again there.

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/ledger/envelope.py nexus/core/config.py nexus/runtime_config/catalog.py nexus/core/metrics.py tests/test_engagement_ledger_capture.py
git commit -m "feat(ledger): schema-versioned envelope, closed event registry, capture kill switch"
```

---

### Task 3: `emit()`

**Files:**
- Create: `nexus/engagement/ledger/emit.py`
- Test: `tests/test_engagement_ledger_capture.py`

- [ ] **Step 1: Write the failing tests**

Add `test_an_event_is_written_in_the_actions_transaction_and_vanishes_with_a_rollback` and `test_emit_never_breaks_the_caller` from the final file.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_ledger_capture.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.ledger.emit'`

- [ ] **Step 3: Implement**

`nexus/engagement/ledger/emit.py`:

```python
"""``emit()`` — the one call that records a ledger event (spec §18.1).

    await emit(ts, "outcome.recorded", refs={"account_id": ..., "contact_id": ...},
               payload={"stage": "meeting"})

Four guarantees, each load-bearing:

* **Same transaction as the action.** The outbox row is added to the caller's session, so it
  commits exactly when the action commits and disappears when the action rolls back. An event about
  something that did not happen would poison a training set.
* **Never harmful.** Any failure is logged and counted and swallowed, and the insert runs in a
  SAVEPOINT so a failed insert cannot poison the caller's transaction either. The action always
  matters more than the evidence about it — the same rule ``metered()`` and ``record_audit`` follow.
* **Consent-gated.** Nothing is written or buffered unless the workspace's consent is ``on``.
* **Stoppable.** ``ledger_capture_enabled`` (Control plane) stops collection everywhere, at once.
"""
from __future__ import annotations

import logging
from datetime import datetime

from nexus.core.tenancy import TenantSession

logger = logging.getLogger("nexus.engagement.ledger")


async def emit(
    ts: TenantSession, event_type: str, *, refs: dict | None = None, payload: dict | None = None,
    actor_user_id: str | None = None, actor_role: str | None = None,
    correlation_id: str | None = None, causation_id: str | None = None, model: str | None = None,
    prompt_version: str | None = None, occurred_at: datetime | None = None,
) -> str | None:
    """Record one event; returns its id, or ``None`` when nothing was recorded. Never raises."""
    from nexus.core import metrics
    from nexus.core.config import get_settings
    from nexus.core.db import utcnow
    from nexus.engagement.ids import new_ulid
    from nexus.engagement.ledger import consent, envelope
    from nexus.models.ledger import LedgerOutbox

    try:
        if not get_settings().ledger_capture_enabled:
            return None
        if event_type not in envelope.EVENT_TYPES:
            logger.warning("ledger event type %r is not registered; dropped", event_type)
            metrics.record_ledger_event("unregistered", "dropped")
            return None
        if await consent.status(ts) != "on":
            return None
        event_id = new_ulid()
        when = occurred_at or utcnow()
        body = envelope.build(
            event_id=event_id, event_type=event_type, tenant_id=ts.tenant_id, occurred_at=when,
            actor_user_id=actor_user_id, actor_role=actor_role, refs=refs,
            correlation_id=correlation_id, causation_id=causation_id, model=model,
            prompt_version=prompt_version, payload=payload,
        )
        async with ts.session.begin_nested():
            ts.add(LedgerOutbox(event_id=event_id, event_type=event_type,
                                schema_version=envelope.SCHEMA_VERSION, occurred_at=when,
                                payload=body))
            await ts.flush()
        metrics.record_ledger_event(event_type, "recorded")
        return event_id
    except Exception:
        logger.warning("ledger emit failed for %s", event_type, exc_info=True)
        try:
            from nexus.core import metrics as _metrics

            _metrics.record_ledger_event(event_type if isinstance(event_type, str) else "?",
                                         "failed")
        except Exception:
            pass
        return None
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_ledger_capture.py tests/test_runtime_control_plane.py -n0 -q`
Expected: the four tests written so far pass.

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/ledger/emit.py tests/test_engagement_ledger_capture.py
git commit -m "feat(ledger): emit() — same transaction, consent-gated, never raises"
```

---

### Task 4: The sign-up choice, through the OTP path too

**Files:**
- Modify: `nexus/api/schemas.py`, `nexus/api/routers/auth.py`, `nexus/auth/registration.py`
- Test: `tests/test_engagement_ledger_capture.py`

- [ ] **Step 1: Write the failing tests**

Add `test_signup_records_the_choice_on_the_form`, `test_the_otp_path_carries_the_choice_through_verification` and `test_a_second_workspace_records_its_own_choice` from the final file.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_ledger_capture.py -n0 -q`
Expected: FAIL — the consent row is `pending`, because nothing records the form's choice.

- [ ] **Step 3: Implement**

In `nexus/api/schemas.py`, add the same field to **three** models — `SignupRequest`, `RegisterStartRequest` and `NewWorkspaceRequest` — after their last existing field:

```python
    # "Help improve the AI with this workspace's data" (D24). Shown and pre-selected on the form;
    # omitted by an older client, which is the same pre-selected choice.
    training_consent: bool = True
```

In `nexus/api/routers/auth.py`, in `signup`, after the owner `Membership` is added (`db.add(membership)`):

```python
        # The ledger consent chosen on the form, in the SAME transaction (D24).
        from nexus.engagement.ledger.consent import add_signup_decision

        add_signup_decision(db, tenant_id=tenant.id, user_id=user.id,
                            opted_in=req.training_consent)
```

In the same file, in `create_workspace`, after its `db.add(membership)`:

```python
        from nexus.engagement.ledger.consent import add_signup_decision

        add_signup_decision(db, tenant_id=tenant.id, user_id=principal.user_id,
                            opted_in=req.training_consent)
```

Still in `auth.py`, in `register_start`, pass the choice through:

```python
            email=req.email,
            password=req.password,
            training_consent=req.training_consent,
        )
    except RegistrationError as exc:
```

In `nexus/auth/registration.py`, add the parameter to `start_registration`:

```python
    email: str,
    password: str,
    training_consent: bool = True,
) -> StartResult:
```

store it on the pending row (the column exists from phase 01's migration `0057`):

```python
        resends=0,
        last_sent_at=now,
        training_consent=bool(training_consent),
    )
    db.add(pending)
```

and record it in `verify_and_create`, after the owner membership is added:

```python
        # The consent chosen on the form survived the OTP step on the pending row (D24).
        from nexus.engagement.ledger.consent import add_signup_decision

        add_signup_decision(db, tenant_id=tenant.id, user_id=user.id,
                            opted_in=bool(pending.training_consent))
```

> A workspace created by either path is therefore never `pending`: it answered at sign-up. `pending` means exactly "created before the ledger existed", which is what the one-time prompt is for.

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_ledger_capture.py tests/test_auth_signup.py tests/test_auth_otp_registration.py tests/test_workspaces.py -n0 -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add nexus/api/schemas.py nexus/api/routers/auth.py nexus/auth/registration.py tests/test_engagement_ledger_capture.py
git commit -m "feat(ledger): record the sign-up consent in the tenant-creating transaction"
```

---

### Task 5: The settings API

**Files:**
- Create: `nexus/api/routers/engagement_settings.py`
- Modify: `nexus/api/routers/__init__.py`
- Test: `tests/test_engagement_ledger_capture.py`

- [ ] **Step 1: Write the failing tests**

Add `test_an_existing_workspace_is_asked_once_and_switching_off_deletes_the_outbox` and `test_managers_set_the_confidence_range_and_reps_only_read_it` from the final file.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_ledger_capture.py -n0 -q`
Expected: FAIL with `404` — there is no `/engagement/settings` route.

- [ ] **Step 3: Implement**

`nexus/api/routers/engagement_settings.py`:

```python
# nexus/api/routers/engagement_settings.py
"""Workspace engagement settings: training & insights consent (D24) and the confidence bar (D23).

Two settings, two audiences, on purpose:

* **Consent** is decided by owners and admins (``manage_workspace``): it is an agreement about the
  workspace's data. Every member can READ it, because the one-time prompt has to know whether to
  appear and the screen should say what is collected. Switching off deletes what is still waiting
  in the outbox, in the same transaction; data already shipped to the stores is deleted by the
  ledger's deletion job (phase 06).
* **The confidence bar and its range** are set by managers and up (``manage_engagement``): they
  decide what happens to replies without a human, which is a team-lead decision (D23).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission, Role, has_permission
from nexus.core.tenancy import TenantSession

router = APIRouter(prefix="/engagement/settings", tags=["engagement"])


class TrainingConsentOut(BaseModel):
    status: str
    source: str | None
    terms_version: str
    decided_at: datetime | None
    can_decide: bool
    #: True when the workspace has never decided and this member may decide: show the prompt.
    prompt: bool


class TrainingConsentIn(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal["on", "off"]


class EngagementSettingsOut(BaseModel):
    reply_confidence_default: float
    reply_confidence_min: float
    reply_confidence_max: float
    reply_reminder_business_hours: int
    ooo_default_days: int
    can_edit: bool


class EngagementSettingsPatch(BaseModel):
    model_config = {"extra": "forbid"}

    reply_confidence_default: float | None = None
    reply_confidence_min: float | None = None
    reply_confidence_max: float | None = None
    reply_reminder_business_hours: int | None = None
    ooo_default_days: int | None = None


async def _consent_out(ts: TenantSession, principal: Principal) -> TrainingConsentOut:
    from nexus.engagement.ledger import consent

    row = await consent.latest(ts)
    can_decide = has_permission(Role(principal.role), Permission.manage_workspace)
    state = row.status if row else "pending"
    return TrainingConsentOut(
        status=state, source=row.source if row else None,
        terms_version=row.terms_version if row else consent.TERMS_VERSION,
        decided_at=row.decided_at if row else None, can_decide=can_decide,
        prompt=state == "pending" and can_decide,
    )


@router.get("/training", response_model=TrainingConsentOut)
async def get_training_consent(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> TrainingConsentOut:
    return await _consent_out(ts, principal)


@router.put("/training", response_model=TrainingConsentOut)
async def set_training_consent(
    body: TrainingConsentIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_workspace)),
) -> TrainingConsentOut:
    from sqlalchemy import delete

    from nexus.core.audit import record_audit
    from nexus.engagement.ledger import consent
    from nexus.engagement.ledger.emit import emit
    from nexus.models.ledger import LedgerOutbox

    previous = await consent.status(ts)
    source = "prompt" if previous == "pending" else "settings"
    await consent.record(ts, status_value=body.status, source=source, user_id=principal.user_id)
    if body.status == "off":
        await ts.session.execute(delete(LedgerOutbox).where(LedgerOutbox.tenant_id == ts.tenant_id))
    else:
        await emit(ts, "consent.changed", actor_user_id=principal.user_id,
                   actor_role=principal.role,
                   payload={"status": "on", "source": source, "terms_version": consent.TERMS_VERSION})
    await record_audit(ts, "engagement.training_consent", actor_user_id=principal.user_id,
                       target_type="tenant", target_id=ts.tenant_id,
                       meta={"status": body.status, "source": source, "previous": previous})
    return await _consent_out(ts, principal)


async def _tenant(ts: TenantSession):
    from nexus.models.identity import Tenant

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    return tenant


@router.get("", response_model=EngagementSettingsOut)
async def get_engagement_settings(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> EngagementSettingsOut:
    from dataclasses import asdict

    from nexus.engagement.settings import read_settings

    settings = read_settings((await _tenant(ts)).email_settings)
    return EngagementSettingsOut(
        **asdict(settings),
        can_edit=has_permission(Role(principal.role), Permission.manage_engagement),
    )


@router.put("", response_model=EngagementSettingsOut)
async def update_engagement_settings(
    body: EngagementSettingsPatch,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_engagement)),
) -> EngagementSettingsOut:
    from dataclasses import asdict

    from nexus.core.audit import record_audit
    from nexus.engagement.settings import read_settings, validate_update, with_settings

    tenant = await _tenant(ts)
    current = read_settings(tenant.email_settings)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        updated = validate_update(current, patch)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    tenant.email_settings = with_settings(tenant.email_settings, updated)
    await ts.flush()
    await record_audit(ts, "engagement.settings", actor_user_id=principal.user_id,
                       target_type="tenant", target_id=ts.tenant_id,
                       meta={"before": asdict(current), "after": asdict(updated)})
    return EngagementSettingsOut(**asdict(updated), can_edit=True)
```

In `nexus/api/routers/__init__.py`, add `engagement_settings` to the import list beside `engagement_suppression`, and `engagement_settings.router,` to the list of routers registered under the API prefix.

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_ledger_capture.py tests/test_admin_routes_are_not_discoverable.py -n0 -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add nexus/api/routers/engagement_settings.py nexus/api/routers/__init__.py tests/test_engagement_ledger_capture.py
git commit -m "feat(ledger): consent API and the workspace confidence settings"
```

---

### Task 6: The emit seams

**Files:**
- Modify: `nexus/agents/runtime.py`, `nexus/billing/meter.py`, `nexus/orchestration/engine.py`, `nexus/outcomes/service.py`, `nexus/calling/service.py`, `nexus/ingestion/service.py`, `nexus/agents/scoring.py`, `nexus/enrichment/waterfall.py`, `nexus/enrichment/account.py`, `nexus/core/audit.py`, `nexus/engagement/suppression/service.py`, `tests/test_b2b_enrichment_actor.py`
- Test: `tests/test_engagement_ledger_capture.py`

Every seam follows the same shape: **after** the action has flushed, inside its existing transaction, one `await emit(...)` with the refs that name what it was about. The import is function-local at every seam — `nexus/core/audit.py` and `nexus/billing/meter.py` are imported by nearly everything, and a module-level import of the ledger from them would be a cycle.

- [ ] **Step 1: Write the failing tests**

Add `test_existing_actions_record_their_events_for_a_consented_workspace` (behaviour: an audited action, a metered charge and an ingested signal each leave their event) and `test_every_seam_in_the_dependency_map_emits` (structure: each file in the roadmap's seam list contains an `emit(` call) from the final file.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_ledger_capture.py -n0 -q`
Expected: FAIL — no events are recorded, and the structural test lists every file with no `emit(`.

- [ ] **Step 3: Implement**

`nexus/agents/runtime.py` — after `if persist:` writes the run (`ts.add(run)` / `await ts.flush()`):

```python
            from nexus.engagement.ledger.emit import emit

            await emit(
                ts, "research.completed" if agent_name == "research" else "ai.call",
                refs={"account_id": account_id, "agent_run_id": run.id},
                model=getattr(self.llm, "model", "") or type(self.llm).__name__,
                payload={"agent": agent_name, "status": status, "inputs": inputs,
                         "output": output, "error": error, "tokens": ctx.tokens,
                         "latency_ms": latency_ms},
            )
```

`nexus/billing/meter.py` — after the `_stamp_cost` block:

```python
    if result.recorded:
        from nexus.engagement.ledger.emit import emit

        await emit(ts, "credits.charged", actor_user_id=user_id,
                   payload={"capability_id": capability_id, "quantity": float(quantity),
                            "source": source, "attrs": attrs or {},
                            "unit_cost_usd": cost.usd / max(float(quantity), 1.0)})
```

`nexus/orchestration/engine.py` — in `_finalize`, after `await self._emit(ts, run, evt, {"status": run.status})`:

```python
            from nexus.engagement.ledger.emit import emit as ledger_emit

            await ledger_emit(ts, "workflow.run_finished", refs={"run_id": run.id},
                              payload={"status": run.status, "error": run.error,
                                       "steps": [{"idx": s.idx, "tool": s.tool,
                                                  "status": s.status} for s in steps]})
```

`nexus/outcomes/service.py` — between the outcome's flush and its `return`:

```python
        from nexus.engagement.ledger.emit import emit

        await emit(ts, "outcome.recorded",
                   refs={"account_id": outcome.account_id, "contact_id": contact_id,
                         "campaign_id": campaign_id, "outcome_id": outcome.id},
                   payload={"stage": stage, "meta": meta or {}})
```

> `payload.meta` is where phase 12 finds the message an outcome is attributed to: later phases record `meta={"message_id": ...}` and the dataset builder reads it there.

`nexus/calling/service.py` — in `log_disposition`, between the flush and the `return activity`:

```python
        from nexus.engagement.ledger.emit import emit

        await emit(ts, "call.disposition",
                   refs={"account_id": task.account_id, "contact_id": task.contact_id,
                         "call_task_id": task.id, "call_activity_id": activity.id},
                   payload={"disposition": disposition, "notes": notes or "",
                            "duration_s": duration_s, "next_step": next_step,
                            "transcript": transcript or ""})
```

`nexus/ingestion/service.py` — after `await raise_alerts_for(ts, account, created)`, one event per signal actually created (deduped ones are not events):

```python
            from nexus.engagement.ledger.emit import emit

            for ev in created:
                await emit(ts, "signal.ingested",
                           refs={"account_id": account.id, "signal_id": ev.id,
                                 "company_id": account.company_id},
                           payload={"kind": ev.kind, "subtype": ev.subtype, "source": ev.source,
                                    "title": ev.title, "body": ev.body or "", "url": ev.url,
                                    "strength": ev.strength,
                                    "occurred_at": ev.occurred_at})
```

`nexus/agents/scoring.py` — after the score is added and flushed:

```python
        from nexus.engagement.ledger.emit import emit

        await emit(ctx.ts, "account.scored",
                   refs={"account_id": ctx.account.id, "score_id": score.id,
                         "company_id": ctx.account.company_id},
                   payload={"icp_fit": icp_fit, "intent": intent, "health": health,
                            "composite": composite, "rationale": rationale})
```

`nexus/enrichment/waterfall.py` — in `enrich_contact`, between the flush and `return merged`:

```python
        from nexus.engagement.ledger.emit import emit

        await emit(ts, "enrichment.contact", actor_user_id=user_id,
                   refs={"account_id": account.id, "contact_id": contact.id},
                   payload={"found": merged.found, "source": merged.source,
                            "email_found": bool(merged.email),
                            "email_status": merged.email_status,
                            "email_confidence": merged.email_confidence,
                            "phone_found": bool(merged.phone)})
```

`nexus/enrichment/account.py` — this one needs a **rename**, because `SearchBackedAccountEnricher.enrich` has five early returns and an event per return would either be five copies or four misses. Rename the existing method to `_enrich` (body untouched) and add a wrapper above it with the original signature:

```python
    async def enrich(
        self, ts, account: Account, *, user_id: str | None = None,
        raise_on_block: bool = False, meter: bool = True, force: bool = False,
        web_search: bool = True,
    ) -> list[str]:
        """Fill blank firmographics (see ``_enrich``), then record what was filled in the ledger.

        A thin wrapper so the event is written once, whichever of ``_enrich``'s many early returns
        was taken."""
        filled = await self._enrich(ts, account, user_id=user_id, raise_on_block=raise_on_block,
                                    meter=meter, force=force, web_search=web_search)
        from nexus.engagement.ledger.emit import emit

        await emit(ts, "enrichment.account", actor_user_id=user_id,
                   refs={"account_id": account.id, "company_id": account.company_id},
                   payload={"filled": list(filled), "web_search": web_search})
        return filled
```

Two existing guards in `tests/test_b2b_enrichment_actor.py` read the SOURCE of that method to prove the actor runs before the web path, so both must now read the body rather than the wrapper — `inspect.getsource(SearchBackedAccountEnricher.enrich)` → `inspect.getsource(SearchBackedAccountEnricher._enrich)` in `test_the_actor_is_tried_before_the_web_path` and `test_a_good_actor_answer_stops_the_search`, with a comment saying why:

```python
    # `enrich` is the thin wrapper that records the ledger event; the ordering this guards
    # lives in `_enrich`, where the enrichment itself happens.
```

`nexus/core/audit.py` — in `record_audit`, after the log sink line `audit(action, tenant_id=ts.tenant_id, actor=actor_user_id, **payload)`:

```python

    from nexus.engagement.ledger.emit import emit

    await emit(ts, "audit.action", actor_user_id=actor_user_id,
               refs={"target_id": target_id},
               payload={"action": action, "target_type": target_type, "meta": payload})
```

`nexus/engagement/suppression/service.py` — after the `engagement.dnc.add` audit call:

```python
    from nexus.engagement.ledger.emit import emit

    await emit(ts, "contact.suppressed", actor_user_id=created_by_user_id,
               refs={"contact_id": contact_id, "message_id": source_message_id},
               payload={"reason": reason})
```

and after the `engagement.dnc.lift` audit call:

```python
    from nexus.engagement.ledger.emit import emit

    await emit(ts, "contact.unsuppressed", actor_user_id=user_id,
               refs={"contact_id": block.contact_id},
               payload={"reason": block.reason, "note": block.lift_note})
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_ledger_capture.py tests/test_b2b_enrichment_actor.py tests/test_audit_log.py tests/test_billing_metering.py tests/test_ingestion_service.py tests/test_outcomes.py -n0 -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add nexus/agents nexus/billing/meter.py nexus/orchestration/engine.py nexus/outcomes/service.py nexus/calling/service.py nexus/ingestion/service.py nexus/enrichment nexus/core/audit.py nexus/engagement/suppression/service.py tests/test_b2b_enrichment_actor.py tests/test_engagement_ledger_capture.py
git commit -m "feat(ledger): record what the product already does at eleven existing seams"
```

---

### Task 7: A failed job is an event

**Files:**
- Modify: `nexus/workers/tasks.py`
- Test: `tests/test_engagement_ledger_capture.py`

- [ ] **Step 1: Write the failing test**

`test_every_seam_in_the_dependency_map_emits` already covers `nexus/workers/tasks.py`; it fails until this task.

- [ ] **Step 2: Run to see it fail**

Run: `pytest tests/test_engagement_ledger_capture.py::test_every_seam_in_the_dependency_map_emits -n0 -q`
Expected: FAIL naming `nexus/workers/tasks.py`.

- [ ] **Step 3: Implement**

In `dispatch`, call the recorder before returning the failure shape, and add the helper below it:

```python
        await _record_job_failure(job, exc)
        return {"error": f"{type(exc).__name__}: {exc}", JOB_FAILED_KEY: True}


async def _record_job_failure(job: Job, exc: Exception) -> None:
    """A ledger ``error.job_failed`` for a tenant job that raised. Never raises: the retry and
    dead-letter path must run whatever happens here."""
    tenant_id = (job.payload or {}).get("tenant_id")
    if not tenant_id:
        return
    try:
        from nexus.engagement.ledger.emit import emit

        async with tenant_session(tenant_id) as ts:
            await emit(ts, "error.job_failed",
                       payload={"job": job.name, "attempt": job.attempts + 1,
                                "max_attempts": job.max_attempts,
                                "error": f"{type(exc).__name__}: {exc}"[:500]})
    except Exception:
        logger.debug("could not record job failure in the ledger", exc_info=True)
```

> A job with no `tenant_id` in its payload is a platform sweep and has no workspace to attribute to — and no workspace whose consent could permit recording it. It is left to the dead-letter table, which is where an operator looks for it.

- [ ] **Step 4: Run to see it pass**

Run: `pytest tests/test_engagement_ledger_capture.py tests/test_job_durability.py -n0 -q`
Expected: all pass — the retry and dead-letter behaviour is unchanged.

- [ ] **Step 5: Commit**

```bash
git add nexus/workers/tasks.py tests/test_engagement_ledger_capture.py
git commit -m "feat(ledger): a tenant job that raised records why"
```

---

### Task 8: Asking, explaining and switching off

**Files:**
- Create: `frontend/src/pages/DataUsePage.tsx` (+ `.module.css`), `frontend/src/components/engagement/ConsentPrompt.tsx` (+ `.module.css`), `frontend/src/pages/settings/TrainingConsentCard.tsx` (+ `.module.css`)
- Modify: `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts`, `frontend/src/pages/LoginPage.tsx` (+ `.module.css`), `frontend/src/App.tsx`, `frontend/src/components/layout/AppShell.tsx`, `frontend/src/pages/SettingsPage.tsx`
- Test: `tests/test_engagement_ledger_capture.py`

- [ ] **Step 1: Write the failing test**

Add `test_signup_offers_the_choice_and_the_prompt_and_settings_exist` from the final file. There is no frontend test runner, so this reads the source — the established pattern in `tests/test_plan_gated_nav.py`.

- [ ] **Step 2: Run to see it fail**

Run: `pytest tests/test_engagement_ledger_capture.py::test_signup_offers_the_choice_and_the_prompt_and_settings_exist -n0 -q`
Expected: FAIL — `DataUsePage.tsx` does not exist.

- [ ] **Step 3: Implement**

In `frontend/src/lib/types.ts`, add the field to `SignupRequest`:

```ts
  /** "Help improve the AI with this workspace's data" (D24). Pre-selected on the form. */
  training_consent?: boolean;
```

and append the consent state at the end of the file:

```ts
/** GET /engagement/settings/training — the workspace's ledger consent (D24). */
export interface TrainingConsentState {
  status: "on" | "off" | "pending";
  source: "signup" | "prompt" | "settings" | null;
  terms_version: string;
  decided_at: string | null;
  can_decide: boolean;
  prompt: boolean;
}
```

In `frontend/src/lib/api.ts`, import `TrainingConsentState` beside `DoNotContactEntry` and add the two calls above the `// ---- engagement: do not contact ----` section:

```ts
  // ---- engagement: training & insights consent ----
  trainingConsent(signal?: AbortSignal) {
    return this.request<TrainingConsentState>("/engagement/settings/training", { signal });
  }
  setTrainingConsent(status: "on" | "off") {
    return this.request<TrainingConsentState>("/engagement/settings/training", {
      method: "PUT", body: { status },
    });
  }
```

`frontend/src/pages/DataUsePage.tsx` — the page the sign-up form links to, reachable **before** an account exists:

```tsx
import { Link } from "react-router-dom";
import styles from "./DataUsePage.module.css";

/**
 * What "Help improve the AI with this workspace's data" collects (D24, spec §18).
 *
 * Public, because the sign-up form links here before an account exists. Plain words, no marketing:
 * the choice is only informed if this page says exactly what is kept, where, and how to stop it.
 * Keep it in step with `nexus/engagement/ledger/envelope.py::EVENT_TYPES` and spec §18.
 */
export function DataUsePage() {
  return (
    <main className={styles.page}>
      <article className={styles.article}>
        <h1>How workspace data improves the AI</h1>
        <p className={styles.lede}>
          When this is on, the product records what happens in your workspace so that its drafting,
          reply reading and timing advice can be improved, and so that SDRs can see when a prospect
          usually replies. You can switch it off at any time under Settings; switching off stops
          collection immediately and deletes what was collected.
        </p>

        <h2>What is recorded</h2>
        <ul>
          <li>AI requests and results: drafted emails, research briefs, reply readings, the model used.</li>
          <li>How your team used them: edits to drafts, approvals, decisions on replies.</li>
          <li>Outreach outcomes: emails sent, bounces, replies, meetings, and when they happened.</li>
          <li>Account activity the product already stores: signals, scores, enrichment, calls.</li>
        </ul>
        <p>Clicks and page views in the browser are not recorded.</p>

        <h2>Two uses, kept apart</h2>
        <ul>
          <li>
            <strong>Training.</strong> Before anything is used to train a model, names, email
            addresses, phone numbers, company names and links are replaced with placeholders, and
            people, companies and workspaces are replaced with one-way keys.
          </li>
          <li>
            <strong>Prospect insights.</strong> Facts such as when a person tends to reply are kept
            with the person so SDRs can be told. Another workspace sees a pattern only when at least
            three workspaces have history with that person, and never who emailed them, what was
            said, or which workspaces.
          </li>
        </ul>

        <h2>Who can change it</h2>
        <p>
          Workspace owners and admins. A person can ask for their data to be erased; erasure removes
          it from every store.
        </p>

        <p className={styles.back}>
          <Link to="/login">Back to sign up</Link>
        </p>
      </article>
    </main>
  );
}

export default DataUsePage;
```

`frontend/src/pages/DataUsePage.module.css`:

```css
.page {
  min-height: 100dvh;
  padding: var(--space-8) var(--space-4);
  background: var(--bg);
  color: var(--text);
}

.article {
  max-inline-size: 68ch;
  margin: 0 auto;
  line-height: var(--leading);
}

.article h1 {
  font-size: var(--text-2xl);
  margin: 0 0 var(--space-4);
}

.article h2 {
  font-size: var(--text-lg);
  margin: var(--space-6) 0 var(--space-2);
}

.article ul {
  padding-left: var(--space-5);
}

.article li + li {
  margin-top: var(--space-2);
}

.lede {
  font-size: var(--text-lg);
  color: var(--text-muted);
}

.back {
  margin-top: var(--space-8);
}
```

`frontend/src/components/engagement/ConsentPrompt.tsx`:

```tsx
import { useState } from "react";
import { Button, Modal } from "@/components/ui";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { TrainingConsentState } from "@/lib/types";
import styles from "./ConsentPrompt.module.css";

/**
 * The one-time question for workspaces created before the ledger existed (D24, spec §18.6).
 *
 * Shown to an owner or admin while the workspace has never decided; nothing is collected until then.
 * "Keep on" is the pre-selected choice, as it is at sign-up, and both buttons record a decision, so
 * the prompt never returns once answered. Closing it without choosing asks again next session.
 */
export function ConsentPrompt() {
  const api = useApiClient();
  const toast = useToast();
  const state = useApi<TrainingConsentState>((signal) => api.trainingConsent(signal), []);
  const [dismissed, setDismissed] = useState(false);
  const [saving, setSaving] = useState<"on" | "off" | null>(null);

  if (!state.data?.prompt || dismissed) return null;

  async function decide(status: "on" | "off") {
    setSaving(status);
    try {
      await api.setTrainingConsent(status);
      toast.success(
        status === "on" ? "Thanks" : "Switched off",
        status === "on"
          ? "Workspace data will help improve the AI. You can change this in Settings."
          : "Nothing will be collected. You can change this in Settings.",
      );
      setDismissed(true);
    } catch (err) {
      toast.error("Couldn't save your choice", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(null);
    }
  }

  return (
    <Modal
      open
      onClose={() => setDismissed(true)}
      title="Help improve the AI with this workspace's data?"
      description="The product can learn from what happens in this workspace: drafts and edits, replies and outcomes. Training copies have names, addresses and companies replaced."
      footer={
        <>
          <Button variant="ghost" onClick={() => decide("off")} loading={saving === "off"}
            disabled={saving !== null}>
            Turn off
          </Button>
          <Button onClick={() => decide("on")} loading={saving === "on"} disabled={saving !== null}
            autoFocus>
            Keep on
          </Button>
        </>
      }
    >
      <p className={styles.text}>
        <a href="/data-use" target="_blank" rel="noopener noreferrer">What is collected and how it is used</a>
      </p>
    </Modal>
  );
}
```

`frontend/src/components/engagement/ConsentPrompt.module.css`:

```css
.text {
  margin: 0;
  font-size: var(--text-sm);
  line-height: var(--leading);
}
```

`frontend/src/pages/settings/TrainingConsentCard.tsx`:

```tsx
import { useState } from "react";
import { Badge, Button, Card, CardHeader, Skeleton } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { TrainingConsentState } from "@/lib/types";
import styles from "./TrainingConsentCard.module.css";

/** Settings: switch the training & insights ledger on or off for this workspace (D24). */
export function TrainingConsentCard() {
  const api = useApiClient();
  const toast = useToast();
  const state = useApi<TrainingConsentState>((signal) => api.trainingConsent(signal), []);
  const [saving, setSaving] = useState(false);

  async function set(status: "on" | "off") {
    setSaving(true);
    try {
      const next = await api.setTrainingConsent(status);
      state.setData(next);
      toast.success(
        status === "on" ? "Switched on" : "Switched off",
        status === "on"
          ? "New activity will be collected from now on."
          : "Collection stopped and collected data is being deleted.",
      );
    } catch (err) {
      toast.error("Couldn't change it", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card padding="lg">
      <CardHeader
        title="Help improve the AI"
        subtitle="Let workspace activity improve drafting, reply reading and prospect timing advice."
      />
      <DataState state={state} errorTitle="Couldn't load this setting"
        skeleton={<Skeleton width="100%" height={48} />}>
        {(s) => (
          <div className={styles.row}>
            <div className={styles.status}>
              <Badge tone={s.status === "on" ? "success" : s.status === "off" ? "neutral" : "warning"} dot>
                {s.status === "on" ? "On" : s.status === "off" ? "Off" : "Not decided yet"}
              </Badge>
              <a href="/data-use" target="_blank" rel="noopener noreferrer">What is collected</a>
            </div>
            {s.can_decide && (
              s.status === "on" ? (
                <Button variant="secondary" loading={saving} onClick={() => set("off")}>
                  Turn off and delete
                </Button>
              ) : (
                <Button loading={saving} onClick={() => set("on")}>Turn on</Button>
              )
            )}
          </div>
        )}
      </DataState>
    </Card>
  );
}
```

`frontend/src/pages/settings/TrainingConsentCard.module.css`:

```css
.row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.status {
  display: inline-flex;
  align-items: center;
  gap: var(--space-3);
  font-size: var(--text-sm);
}
```

In `frontend/src/pages/LoginPage.tsx`, hold the pre-selected choice:

```tsx
  // Pre-selected, as agreed for sign-up (D24); the link says exactly what it means.
  const [trainingConsent, setTrainingConsent] = useState(true);
```

send it with the sign-up body:

```tsx
          full_name: fullName,
          email,
          password,
          training_consent: trainingConsent,
        });
```

and render the checkbox immediately above the `{mode === "login" && (` workspace-slug field:

```tsx
            {mode === "signup" && !verifying && (
              <label className={styles.consent}>
                <input
                  type="checkbox"
                  checked={trainingConsent}
                  onChange={(e) => setTrainingConsent(e.target.checked)}
                />
                <span>
                  Help improve the AI with this workspace's data.{" "}
                  <a href="/data-use" target="_blank" rel="noopener noreferrer">What is collected</a>
                </span>
              </label>
            )}
```

Append to `frontend/src/pages/LoginPage.module.css`:

```css
.consent {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  font-size: var(--text-sm);
  color: var(--text-muted);
  line-height: var(--leading);
}
.consent input {
  margin-top: 0.2em;
  width: 1rem;
  height: 1rem;
  flex: none;
}
```

In `frontend/src/App.tsx`, add the lazy page beside `MailboxesPage`:

```tsx
const DataUsePage = lazyPage(() => import("@/pages/DataUsePage"), "DataUsePage");
```

and the public route beside `/reset-password`:

```tsx
              {/* Public: the sign-up form links here before an account exists (D24). */}
              <Route path="/data-use" element={<DataUsePage />} />
```

In `frontend/src/components/layout/AppShell.tsx`, import the prompt and render it under the impersonation banner:

```tsx
import { ConsentPrompt } from "@/components/engagement/ConsentPrompt";
```

```tsx
      <ImpersonationBanner />
      {/* Asks an owner or admin once, for workspaces created before the ledger existed. */}
      <ConsentPrompt />
```

In `frontend/src/pages/SettingsPage.tsx`, import the card and render it first in the settings stack:

```tsx
import { TrainingConsentCard } from "@/pages/settings/TrainingConsentCard";
```

```tsx
      <div className={styles.stack}>
        <TrainingConsentCard />
```

- [ ] **Step 4: Run to see it pass**

Run: `pytest tests/test_engagement_ledger_capture.py -n0 -q`
Then: `cd frontend && npm run typecheck`
Expected: tests pass; typecheck clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src tests/test_engagement_ledger_capture.py
git commit -m "feat(ledger): ask at sign-up, ask existing workspaces once, switch off in Settings"
```

---

### Task 9: The whole test file, and the whole suite

**Files:**
- Test: `tests/test_engagement_ledger_capture.py`

- [ ] **Step 1: Write the complete test file**

`tests/test_engagement_ledger_capture.py` in full — every earlier task took its tests from here, so the file should now match this exactly:

```python
"""Training & insights ledger capture: consent, the envelope, emit(), sign-up, seams (D24, §18)."""
from __future__ import annotations

import pathlib
from datetime import datetime, timezone

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, make_tenant, principal_from_token, signup, tenant_session

NEXUS = pathlib.Path(__file__).resolve().parents[1] / "nexus"
SRC = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"


async def _consented_tenant(slug: str = "ledger") -> str:
    from nexus.engagement.ledger import consent

    tid = await make_tenant(slug=slug, name=slug.title())
    async with tenant_session(tid) as ts:
        await consent.record(ts, status_value="on", source="settings", user_id=None)
    return tid


async def _outbox(tid: str) -> list:
    from nexus.models.ledger import LedgerOutbox

    async with tenant_session(tid) as ts:
        return await ts.list(LedgerOutbox)


# ---- envelope -----------------------------------------------------------------------------------

def test_the_envelope_is_json_safe_bounded_and_self_correlated():
    import json

    from nexus.engagement.ledger import envelope

    body = envelope.build(
        event_id="01J0000000000000000000000A", event_type="outcome.recorded", tenant_id="t1",
        occurred_at=datetime(2026, 9, 17, 9, 14, 3, tzinfo=timezone.utc),
        refs={"account_id": "a1", "contact_id": None},
        payload={"when": datetime(2026, 9, 1, tzinfo=timezone.utc), "long": "x" * 50_000},
    )
    json.dumps(body)
    assert body["schema_version"] == envelope.SCHEMA_VERSION
    assert body["refs"] == {"account_id": "a1"}
    assert body["chain"]["correlation_id"] == "01J0000000000000000000000A"
    assert body["payload"]["long"].endswith("[truncated]")
    assert len(body["payload"]["long"]) < 21_000
    huge = envelope.build(event_id="e", event_type="ai.call", tenant_id="t", occurred_at=datetime.now(timezone.utc),
                          payload={f"k{i}": "y" * 19_000 for i in range(20)})
    assert huge["payload"] == {"truncated": True, "reason": "payload over the ledger size cap"}


# ---- consent and emit -----------------------------------------------------------------------------

async def test_nothing_is_recorded_before_a_workspace_decides_or_after_it_says_no():
    from nexus.engagement.ledger import consent
    from nexus.engagement.ledger.emit import emit

    tid = await make_tenant(slug="undecided")
    async with tenant_session(tid) as ts:
        assert await consent.status(ts) == "pending"
        assert await emit(ts, "outcome.recorded", payload={"stage": "sent"}) is None
        await consent.record(ts, status_value="off", source="prompt", user_id=None)
        assert await emit(ts, "outcome.recorded", payload={"stage": "sent"}) is None
    assert await _outbox(tid) == []


async def test_an_event_is_written_in_the_actions_transaction_and_vanishes_with_a_rollback():
    from nexus.engagement.ledger.emit import emit
    from nexus.models.outcome import Outcome

    tid = await _consented_tenant("txn")
    async with tenant_session(tid) as ts:
        event_id = await emit(ts, "outcome.recorded", refs={"account_id": "a1"},
                              payload={"stage": "meeting"})
    rows = await _outbox(tid)
    assert [r.event_id for r in rows] == [event_id]
    assert rows[0].payload["event_type"] == "outcome.recorded"
    assert rows[0].payload["payload"] == {"stage": "meeting"}

    with pytest.raises(RuntimeError):
        async with tenant_session(tid) as ts:
            ts.add(Outcome(stage="sent"))
            await emit(ts, "outcome.recorded", payload={"stage": "sent"})
            raise RuntimeError("the action failed")
    assert len(await _outbox(tid)) == 1


async def test_emit_never_breaks_the_caller(monkeypatch):
    from nexus.engagement.ledger.emit import emit
    from nexus.models.outcome import Outcome

    tid = await _consented_tenant("safe")
    async with tenant_session(tid) as ts:
        assert await emit(ts, "not.a.registered.type") is None
        # A real failure inside emit: an occurred_at that is not a datetime.
        assert await emit(ts, "outcome.recorded", occurred_at="yesterday") is None
        ts.add(Outcome(stage="sent"))  # the session is still usable afterwards
        await ts.flush()
    monkeypatch.setattr(get_settings(), "ledger_capture_enabled", False)
    async with tenant_session(tid) as ts:
        assert await emit(ts, "outcome.recorded", payload={"stage": "sent"}) is None
    assert await _outbox(tid) == []


# ---- sign-up -------------------------------------------------------------------------------------

async def _consent_rows(tenant_id: str):
    from nexus.models.ledger import TrainingConsent

    async with tenant_session(tenant_id) as ts:
        return await ts.list(TrainingConsent)


async def test_signup_records_the_choice_on_the_form(client):
    token = await signup(client, slug="optin", email="owner@optinco.com", company="Optin")
    rows = await _consent_rows(principal_from_token(token).tenant_id)
    assert [(r.status, r.source) for r in rows] == [("on", "signup")]

    r = await client.post("/api/auth/signup", json={
        "company_name": "Optout", "company_slug": "optout", "full_name": "Rep",
        "email": "owner@optoutco.com", "password": "password123", "training_consent": False,
    })
    rows = await _consent_rows(r.json()["tenant_id"])
    assert [(r.status, r.source) for r in rows] == [("off", "signup")]


async def test_the_otp_path_carries_the_choice_through_verification():
    from nexus.auth.otp import hash_otp, otp_secret
    from nexus.auth.registration import verify_and_create
    from nexus.core.db import get_sessionmaker, utcnow
    from nexus.models.identity import PendingRegistration

    from datetime import timedelta

    async with get_sessionmaker()() as db:
        db.add(PendingRegistration(
            email="ada@otpco.com", full_name="Ada", company_name="Otp Co", company_slug="otp-co",
            password_hash="x", otp_hash=hash_otp("424242", otp_secret()),
            expires_at=utcnow() + timedelta(minutes=10), training_consent=False,
        ))
        await db.commit()
        _user, tenant = await verify_and_create(db, email="ada@otpco.com", code="424242")
    rows = await _consent_rows(tenant.id)
    assert [(r.status, r.source) for r in rows] == [("off", "signup")]


async def test_a_second_workspace_records_its_own_choice(client):
    token = await signup(client, slug="first", email="owner@firstco.com", company="First")
    r = await client.post("/api/auth/workspaces", json={"name": "Second", "slug": "second-ws",
                                                        "training_consent": False},
                          headers=auth(token))
    assert r.status_code == 201, r.text
    rows = await _consent_rows(r.json()["tenant_id"])
    assert [(row.status, row.source) for row in rows] == [("off", "signup")]


# ---- consent API -----------------------------------------------------------------------------------

async def test_an_existing_workspace_is_asked_once_and_switching_off_deletes_the_outbox(client):
    from nexus.core.security import create_access_token
    from nexus.engagement.ledger.emit import emit
    from nexus.models.ledger import TrainingConsent

    token = await signup(client, slug="legacy", email="owner@legacyco.com", company="Legacy")
    tid = principal_from_token(token).tenant_id
    async with tenant_session(tid) as ts:  # make it look like a pre-ledger workspace
        for row in await ts.list(TrainingConsent):
            await ts.delete(row)

    state = (await client.get("/api/engagement/settings/training", headers=auth(token))).json()
    assert state["status"] == "pending" and state["prompt"] is True

    rep = create_access_token(user_id="rep", tenant_id=tid, role="rep")
    rep_state = (await client.get("/api/engagement/settings/training", headers=auth(rep))).json()
    assert rep_state["prompt"] is False
    assert (await client.put("/api/engagement/settings/training", json={"status": "on"},
                             headers=auth(rep))).status_code == 403

    on = await client.put("/api/engagement/settings/training", json={"status": "on"},
                          headers=auth(token))
    assert on.json()["status"] == "on" and on.json()["source"] == "prompt"
    assert on.json()["prompt"] is False
    async with tenant_session(tid) as ts:
        await emit(ts, "outcome.recorded", payload={"stage": "sent"})
    assert len(await _outbox(tid)) >= 2  # consent.changed, the outcome, and the audit action

    off = await client.put("/api/engagement/settings/training", json={"status": "off"},
                           headers=auth(token))
    assert off.json()["status"] == "off" and off.json()["source"] == "settings"
    assert await _outbox(tid) == []


async def test_managers_set_the_confidence_range_and_reps_only_read_it(client):
    from nexus.core.security import create_access_token

    token = await signup(client, slug="range", email="owner@rangeco.com", company="Range")
    tid = principal_from_token(token).tenant_id
    rep = create_access_token(user_id="rep", tenant_id=tid, role="rep")
    read = (await client.get("/api/engagement/settings", headers=auth(rep))).json()
    assert read["reply_confidence_default"] == 0.8 and read["can_edit"] is False
    assert (await client.put("/api/engagement/settings", json={"ooo_default_days": 5},
                             headers=auth(rep))).status_code == 403
    bad = await client.put("/api/engagement/settings",
                           json={"reply_confidence_min": 0.9, "reply_confidence_default": 0.8},
                           headers=auth(token))
    assert bad.status_code == 422
    good = await client.put("/api/engagement/settings",
                            json={"reply_confidence_min": 0.7, "ooo_default_days": 5},
                            headers=auth(token))
    assert good.status_code == 200
    assert (good.json()["reply_confidence_min"], good.json()["ooo_default_days"]) == (0.7, 5)


# ---- seams ---------------------------------------------------------------------------------------

async def test_existing_actions_record_their_events_for_a_consented_workspace():
    from nexus.core.audit import record_audit
    from nexus.engagement.suppression.service import suppress
    from nexus.models.account import Account
    from nexus.outcomes.service import get_outcome_service

    tid = await _consented_tenant("seams")
    async with tenant_session(tid) as ts:
        account = Account(name="Acme", domain="acme.io")
        ts.add(account)
        await ts.flush()
        await get_outcome_service().record(ts, stage="replied", account=account)
        await record_audit(ts, "test.action", target_type="thing", target_id="t1")
        await suppress(ts, email="jane@acme.io", reason="manual")
    types = {row.event_type for row in await _outbox(tid)}
    assert {"outcome.recorded", "audit.action", "contact.suppressed"} <= types


def test_every_seam_in_the_dependency_map_emits():
    seams = {
        "agents/runtime.py": "ai.call",
        "billing/meter.py": "credits.charged",
        "orchestration/engine.py": "workflow.run_finished",
        "outcomes/service.py": "outcome.recorded",
        "calling/service.py": "call.disposition",
        "ingestion/service.py": "signal.ingested",
        "agents/scoring.py": "account.scored",
        "enrichment/waterfall.py": "enrichment.contact",
        "enrichment/account.py": "enrichment.account",
        "core/audit.py": "audit.action",
        "workers/tasks.py": "error.job_failed",
        "engagement/suppression/service.py": "contact.suppressed",
    }
    for rel, event_type in seams.items():
        source = (NEXUS / rel).read_text(encoding="utf-8")
        assert "from nexus.engagement.ledger.emit import emit" in source, f"{rel} does not emit"
        assert f'"{event_type}"' in source, f"{rel} does not emit {event_type}"


def test_signup_offers_the_choice_and_the_prompt_and_settings_exist():
    login = (SRC / "pages/LoginPage.tsx").read_text(encoding="utf-8")
    assert "useState(true)" in login and "training_consent: trainingConsent" in login
    assert 'href="/data-use"' in login
    app = (SRC / "App.tsx").read_text(encoding="utf-8")
    assert '<Route path="/data-use" element={<DataUsePage />} />' in app
    shell = (SRC / "components/layout/AppShell.tsx").read_text(encoding="utf-8")
    assert "<ConsentPrompt />" in shell
    assert "<TrainingConsentCard />" in (SRC / "pages/SettingsPage.tsx").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run the phase**

Run: `pytest tests/test_engagement_ledger_capture.py -n0 -q`
Expected: `12 passed`.

- [ ] **Step 3: Run everything this phase touches**

`emit()` now runs inside metering, audit, ingestion, scoring, enrichment and every worker job, so the only honest check is the whole suite:

Run: `pytest -q` then `ruff check nexus tests` then `cd frontend && npm run typecheck`

RUN_RESULT (2026-09-18, CI image `python:3.11`, phases 01–05 applied cumulatively):

```
3579 passed in 5412.96s (1:30:12)
All checks passed!          # ruff check nexus tests
tsc --noEmit -p tsconfig.json   # clean
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_engagement_ledger_capture.py
git commit -m "test(ledger): consent, envelope, emit, sign-up paths, seams and the screens"
```

---

## What phase 06 depends on

- `ledger_outbox` rows carry the whole envelope in `payload`, so the shipper needs nothing else from the app database to build an archive row.
- `consent.TERMS_VERSION` is stamped on every decision; phase 06 copies it onto every training row, so data collected under older terms can be told apart.
- Switching consent off deletes the outbox **and** (from phase 06) enqueues `ledger_delete_workspace`, which removes what has already been shipped and reports the rows remaining.
- The payload shapes for drafting, sending and replies are defined in phase 06's `ledger/payloads.py`; phases 07–10 fill them in.
