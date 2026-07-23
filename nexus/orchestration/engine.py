"""Run engine: drive a planned run to a terminal state, durably and resumably.

The engine is the "executor" half of the orchestrator. It takes a plan authored by the
:class:`~nexus.orchestration.planner.Planner`, persists it as :class:`RunStep` rows, and
walks the DAG: a step becomes runnable once all its dependencies have completed. Outbound
steps (anything ``requires_approval``) park at an :class:`Approval` instead of running, and
the run halts until a human decides — that is the safety gate that keeps a cold-outreach
send from ever going out unreviewed.

Durability & resumability come from three properties:
- every step's state lives in a row, so a process restart resumes from the DB, not memory;
- each transition appends a monotonic :class:`RunEvent` (the log an SSE client replays);
- the shared ``blackboard`` on the run is how subagents stay aware of each other's output.

Everything here is offline-safe: tools wrap agents that run against the stub LLM in tests.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified

from nexus.core.config import get_settings
from nexus.core.events import Event, get_event_bus
from nexus.agents.runtime import AgentRuntime, get_agent_runtime
from nexus.models.orchestration import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    Approval,
    OrchestrationRun,
    RunEvent,
    RunStep,
    RUN_AWAITING_APPROVAL,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PLANNING,
    RUN_RUNNING,
    RUN_TERMINAL,
    STEP_AWAITING_APPROVAL,
    STEP_COMPLETED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_REJECTED,
    STEP_SKIPPED,
    STEP_TERMINAL,
)
from nexus.core.tenancy import TenantSession
from nexus.orchestration.planner import Planner, get_planner
from nexus.orchestration.tools import ToolContext, ToolError, get_tool

import logging

logger = logging.getLogger("nexus.orchestration.engine")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrchestrationError(Exception):
    """Engine-level failure (bad approval, unknown run, etc.)."""


class OrchestrationEngine:
    """Stateless driver over the durable run model. Safe to share across requests."""

    def __init__(self, planner: Planner | None = None) -> None:
        self.planner = planner or get_planner()
        self.bus = get_event_bus()

    # -- creation -------------------------------------------------------------------

    async def create_run(
        self,
        ts: TenantSession,
        goal: str,
        goal_input: dict | None = None,
        *,
        created_by: str | None = None,
        idempotency_key: str | None = None,
        account_id: str | None = None,
    ) -> OrchestrationRun:
        """Plan a goal and persist its run + steps. Idempotent on ``idempotency_key``:
        a duplicate submit (double click, retry, race) returns the existing run rather
        than authoring a second plan — the unique constraint backs this at the DB level."""
        goal_input = dict(goal_input or {})

        if idempotency_key:
            existing = await ts.first(
                OrchestrationRun,
                OrchestrationRun.idempotency_key == idempotency_key,
            )
            if existing is not None:
                return existing

        account_id = account_id or goal_input.get("account_id")
        plan = self.planner.plan(goal, goal_input)

        blackboard: dict = {}
        if account_id:
            blackboard["account_id"] = account_id

        run = OrchestrationRun(
            tenant_id=ts.tenant_id,
            goal=goal,
            goal_input=goal_input,
            status=RUN_PLANNING,
            plan=plan,
            blackboard=blackboard,
            account_id=account_id,
            created_by=created_by,
            idempotency_key=idempotency_key,
        )
        ts.add(run)
        await ts.flush()

        for sd in plan:
            ts.add(
                RunStep(
                    tenant_id=ts.tenant_id,
                    run_id=run.id,
                    idx=sd["idx"],
                    tool=sd["tool"],
                    inputs=sd.get("inputs") or {},
                    depends_on=sd.get("depends_on") or [],
                    requires_approval=bool(sd.get("requires_approval")),
                    status=STEP_PENDING,
                )
            )
        await ts.flush()
        await self._emit(ts, run, "run.created", {"goal": goal, "steps": len(plan)})
        return run

    # -- execution ------------------------------------------------------------------

    async def execute_run(
        self, ts: TenantSession, run: OrchestrationRun, *, runtime: AgentRuntime | None = None
    ) -> OrchestrationRun:
        """Drive the run forward until it completes, fails, or parks at an approval."""
        if run.status in RUN_TERMINAL:
            return run
        runtime = runtime or get_agent_runtime()
        run.status = RUN_RUNNING
        await ts.flush()
        steps = await self._load_steps(ts, run)
        await self._drive(ts, run, steps, runtime=runtime)
        return run

    async def _drive(
        self, ts: TenantSession, run: OrchestrationRun, steps: list[RunStep], *, runtime: AgentRuntime
    ) -> None:
        # Runaway backstop: each iteration must consume one pending step, so the loop terminates in
        # <= len(steps). This generous cap turns a hypothetical stuck step (a step that fails to
        # leave PENDING) into a failed run instead of a wedged worker holding a DB connection.
        max_iterations = len(steps) * 2 + 8
        iterations = 0
        while True:
            iterations += 1
            if iterations > max_iterations:
                logger.error(
                    "orchestration run %s exceeded %d iterations (%d steps); failing to avoid a "
                    "runaway loop", run.id, max_iterations, len(steps),
                )
                step = self._next_runnable(steps)
                if step is not None:
                    step.status = STEP_FAILED
                    step.error = "runaway_guard: run exceeded its step-iteration budget"
                    await ts.flush()
                break
            step = self._next_runnable(steps)
            if step is None:
                break
            if step.requires_approval:
                # Park outbound work at the gate and stop. Resumes via ``decide``.
                await self._park(ts, run, step)
                return
            ok = await self._run_step(ts, run, step, runtime=runtime)
            if not ok:
                await self._cascade_skip(ts, run, steps, step.idx, reason="upstream step failed")
                break
        await self._finalize(ts, run, steps)

    async def _run_step(
        self, ts: TenantSession, run: OrchestrationRun, step: RunStep, *, runtime: AgentRuntime
    ) -> bool:
        """Execute one tool with bounded retries. Returns True on success."""
        tool = get_tool(step.tool)
        if tool is None:
            step.status = STEP_FAILED
            step.error = f"unknown tool '{step.tool}'"
            await ts.flush()
            await self._emit(ts, run, "step.failed", {"idx": step.idx, "error": step.error})
            return False

        settings = get_settings()
        step.status = "running"
        await self._emit(ts, run, "step.started", {"idx": step.idx, "tool": step.tool})

        last_err: str | None = None
        for attempt in range(1, settings.orch_max_attempts + 1):
            step.attempts = attempt
            tc = ToolContext(ts=ts, runtime=runtime, run=run, inputs=dict(step.inputs))
            try:
                output = await tool.run(tc)
                step.output = output if isinstance(output, dict) else {"result": output}
                step.status = STEP_COMPLETED
                step.error = None
                # Tools mutate run.blackboard in place; JSON columns need an explicit
                # dirty flag for the change to persist on flush.
                flag_modified(run, "blackboard")
                await ts.flush()
                await self._emit(
                    ts, run, "step.completed", {"idx": step.idx, "tool": step.tool}
                )
                return True
            except ToolError as exc:
                last_err = str(exc)
            except Exception as exc:  # defensive: a tool bug must not crash the run loop
                last_err = f"{type(exc).__name__}: {exc}"

        step.status = STEP_FAILED
        step.error = last_err
        await ts.flush()
        await self._emit(
            ts, run, "step.failed", {"idx": step.idx, "tool": step.tool, "error": last_err}
        )
        return False

    async def _park(self, ts: TenantSession, run: OrchestrationRun, step: RunStep) -> Approval:
        """Create the human approval gate for an outbound step and halt the run."""
        payload = dict(run.blackboard.get("draft") or {})
        approval = Approval(
            tenant_id=ts.tenant_id,
            run_id=run.id,
            step_id=step.id,
            kind=step.tool,
            payload=payload,
            status=APPROVAL_PENDING,
        )
        ts.add(approval)
        await ts.flush()
        step.status = STEP_AWAITING_APPROVAL
        step.approval_id = approval.id
        run.status = RUN_AWAITING_APPROVAL
        await ts.flush()
        await self._emit(
            ts,
            run,
            "approval.requested",
            {"approval_id": approval.id, "step_idx": step.idx, "kind": step.tool},
        )
        return approval

    # -- approval -------------------------------------------------------------------

    async def decide(
        self,
        ts: TenantSession,
        approval_id: str,
        *,
        decision: str,
        edits: dict | None = None,
        reason: str | None = None,
        from_account: str | None = None,
        delivery_mode: str | None = None,
        decided_by: str | None = None,
        runtime: AgentRuntime | None = None,
    ) -> OrchestrationRun:
        """Apply a human decision to a parked step, then resume (approve) or skip (reject).

        ``from_account`` lets the reviewer choose which configured sending mailbox the message
        goes out from; ``reason`` records why a send was rejected.
        """
        if decision not in ("approve", "reject"):
            raise OrchestrationError(f"invalid decision '{decision}'")
        approval = await ts.get(Approval, approval_id)
        if approval is None:
            raise OrchestrationError("approval not found")
        if approval.status != APPROVAL_PENDING:
            raise OrchestrationError(f"approval already {approval.status}")

        run = await ts.get(OrchestrationRun, approval.run_id)
        if run is None:
            raise OrchestrationError("run not found")
        runtime = runtime or get_agent_runtime()
        steps = await self._load_steps(ts, run)
        step = next((s for s in steps if s.id == approval.step_id), None)
        if step is None:
            raise OrchestrationError("approved step not found")

        approval.decided_by = decided_by
        approval.decided_at = _utcnow()

        if decision == "reject":
            approval.status = APPROVAL_REJECTED
            if reason:
                approval.edits = {**(approval.edits or {}), "reason": reason}
                flag_modified(approval, "edits")
            step.status = STEP_REJECTED
            step.error = f"rejected by reviewer: {reason}" if reason else "rejected by reviewer"
            await ts.flush()
            await self._emit(
                ts, run, "approval.rejected",
                {"approval_id": approval.id, "step_idx": step.idx, "reason": reason or ""},
            )
            await self._cascade_skip(ts, run, steps, step.idx, reason="upstream step rejected")
            await self._finalize(ts, run, steps)
            return run

        # Approve: a reviewer may have edited the draft at the gate (subject/body) and/or
        # chosen which configured mailbox the message sends from.
        if edits or from_account or delivery_mode:
            draft = dict(run.blackboard.get("draft") or {})
            if edits:
                draft.update(edits)
                approval.edits = {**(approval.edits or {}), **edits}
            if from_account:
                draft["from_account"] = from_account
            if delivery_mode:
                draft["delivery_mode"] = delivery_mode  # send | draft
            run.blackboard["draft"] = draft
            flag_modified(run, "blackboard")
        approval.status = APPROVAL_APPROVED
        run.status = RUN_RUNNING
        await ts.flush()
        await self._emit(
            ts, run, "approval.approved", {"approval_id": approval.id, "step_idx": step.idx}
        )

        ok = await self._run_step(ts, run, step, runtime=runtime)
        if not ok:
            await self._cascade_skip(ts, run, steps, step.idx, reason="upstream step failed")
            await self._finalize(ts, run, steps)
        else:
            await self._drive(ts, run, steps, runtime=runtime)
        return run

    async def redraft(
        self,
        ts: TenantSession,
        approval_id: str,
        *,
        instructions: str,
        runtime: AgentRuntime | None = None,
    ) -> Approval:
        """Regenerate the parked draft with the reviewer's instructions, leaving it pending.

        Re-runs the messaging agent grounded in the same research (so the grounded-send gate
        still holds), updates ``run.blackboard["draft"]`` and refreshes the approval payload so
        the reviewer sees the new draft without losing their place at the gate.
        """
        instructions = (instructions or "").strip()
        if not instructions:
            raise OrchestrationError("redraft instructions are required")
        approval = await ts.get(Approval, approval_id)
        if approval is None:
            raise OrchestrationError("approval not found")
        if approval.status != APPROVAL_PENDING:
            raise OrchestrationError(f"approval already {approval.status}")
        run = await ts.get(OrchestrationRun, approval.run_id)
        if run is None:
            raise OrchestrationError("run not found")
        if run.account_id is None and not run.blackboard.get("account_id"):
            raise OrchestrationError("cannot redraft without an account context")

        runtime = runtime or get_agent_runtime()
        draft = dict(run.blackboard.get("draft") or {})
        result = await runtime.run(
            "messaging",
            ts,
            account_id=run.account_id or run.blackboard.get("account_id"),
            contact_id=draft.get("contact_id"),
            guidance=instructions,
        )
        out = result.output if isinstance(result.output, dict) else {}
        if result.status != "completed" or out.get("error"):
            raise OrchestrationError(result.error or out.get("error") or "redraft failed")

        draft["subject"] = out.get("subject", draft.get("subject", ""))
        draft["body"] = out.get("body", draft.get("body", ""))
        draft["message"] = out.get("message", draft.get("message", ""))
        run.blackboard["draft"] = draft
        flag_modified(run, "blackboard")

        # The approval payload is the snapshot the UI renders — refresh its content fields.
        payload = dict(approval.payload or {})
        payload.update(
            {"subject": draft["subject"], "body": draft["body"],
             "message": draft.get("message", ""), "redrafted": True}
        )
        approval.payload = payload
        flag_modified(approval, "payload")
        await ts.flush()
        await self._emit(ts, run, "approval.redrafted", {"approval_id": approval.id})
        return approval

    # -- cancellation ---------------------------------------------------------------

    async def cancel_run(self, ts: TenantSession, run: OrchestrationRun) -> OrchestrationRun:
        if run.status in RUN_TERMINAL:
            return run
        steps = await self._load_steps(ts, run)
        for s in steps:
            if s.status == STEP_AWAITING_APPROVAL and s.approval_id:
                ap = await ts.get(Approval, s.approval_id)
                if ap is not None and ap.status == APPROVAL_PENDING:
                    ap.status = APPROVAL_REJECTED
                    ap.decided_at = _utcnow()
            if s.status in (STEP_PENDING, STEP_AWAITING_APPROVAL):
                s.status = STEP_SKIPPED
        run.status = RUN_CANCELLED
        await ts.flush()
        await self._emit(ts, run, "run.cancelled", {})
        return run

    # -- helpers --------------------------------------------------------------------

    async def _load_steps(self, ts: TenantSession, run: OrchestrationRun) -> list[RunStep]:
        stmt = ts.select(RunStep, RunStep.run_id == run.id).order_by(RunStep.idx)
        return list((await ts.session.scalars(stmt)).all())

    def _next_runnable(self, steps: list[RunStep]) -> RunStep | None:
        by_idx = {s.idx: s for s in steps}
        for s in steps:
            if s.status != STEP_PENDING:
                continue
            deps = s.depends_on or []
            if all(
                by_idx.get(d) is not None and by_idx[d].status == STEP_COMPLETED for d in deps
            ):
                return s
        return None

    def _dependents(self, steps: list[RunStep], idx: int) -> set[int]:
        """Transitive closure of steps that depend on ``idx`` (for cascade skip)."""
        result: set[int] = set()
        frontier = [idx]
        while frontier:
            cur = frontier.pop()
            for s in steps:
                if cur in (s.depends_on or []) and s.idx not in result:
                    result.add(s.idx)
                    frontier.append(s.idx)
        return result

    async def _cascade_skip(
        self, ts: TenantSession, run: OrchestrationRun, steps: list[RunStep], idx: int, *, reason: str
    ) -> None:
        by_idx = {s.idx: s for s in steps}
        skipped = []
        for dep_idx in self._dependents(steps, idx):
            s = by_idx[dep_idx]
            if s.status == STEP_PENDING:
                s.status = STEP_SKIPPED
                s.error = reason
                skipped.append(dep_idx)
        if skipped:
            await ts.flush()
            await self._emit(ts, run, "steps.skipped", {"idx": skipped, "reason": reason})

    async def _finalize(self, ts: TenantSession, run: OrchestrationRun, steps: list[RunStep]) -> None:
        statuses = [s.status for s in steps]
        if any(st == STEP_FAILED for st in statuses):
            run.status = RUN_FAILED
            run.error = next((s.error for s in steps if s.status == STEP_FAILED), None)
            evt = "run.failed"
        elif any(st == STEP_AWAITING_APPROVAL for st in statuses):
            run.status = RUN_AWAITING_APPROVAL
            evt = None
        elif all(st in STEP_TERMINAL for st in statuses):
            run.status = RUN_COMPLETED
            evt = "run.completed"
        else:
            run.status = RUN_RUNNING
            evt = None
        await ts.flush()
        if evt:
            await self._emit(ts, run, evt, {"status": run.status})

    async def _emit(
        self, ts: TenantSession, run: OrchestrationRun, type_: str, data: dict
    ) -> RunEvent:
        """Append a monotonic event to the run log and publish it on the in-proc bus.

        The event log is the durable source of truth an SSE client replays from its
        ``Last-Event-ID``; the bus publish is the live fan-out. Both carry the same
        ``run_id`` envelope so a future broker swap is transparent to subscribers.
        """
        current = await ts.session.scalar(
            select(func.max(RunEvent.seq)).where(
                RunEvent.run_id == run.id, RunEvent.tenant_id == ts.tenant_id
            )
        )
        seq = int(current or 0) + 1
        event = RunEvent(
            tenant_id=ts.tenant_id, run_id=run.id, seq=seq, type=type_, data=data
        )
        ts.add(event)
        await ts.flush()
        await self.bus.publish(
            Event(
                name=f"orchestration.{type_}",
                tenant_id=ts.tenant_id,
                payload=data,
                run_id=run.id,
                step_id=data.get("step_id"),
            )
        )
        return event


_engine: OrchestrationEngine | None = None


def get_orchestration_engine() -> OrchestrationEngine:
    global _engine
    if _engine is None:
        _engine = OrchestrationEngine()
    return _engine
