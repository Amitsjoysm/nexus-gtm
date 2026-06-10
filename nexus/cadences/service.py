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

        # Stop conditions, checked before composing so a terminated enrollment never wastes
        # a compose or touches the prospect again.
        if await self._has_reply(ts, e):
            return ("stop", STOP_REPLIED)
        if self._exceeded_duration(e, now):
            return ("stop", STOP_MAX_TOUCHES)

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
            # A dead address dooms every future touch to the same contact — stop here.
            if skip == SKIP_UNDELIVERABLE:
                return ("stop", STOP_UNDELIVERABLE)
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


_service = CadenceService()


def get_cadence_service() -> CadenceService:
    return _service
