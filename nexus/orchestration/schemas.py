"""Pydantic request/response models for the orchestration API.

These are the wire contracts. They deliberately project only what a client needs (no raw
ORM rows) and keep the run/step/approval shapes flat so the frontend run console can render
a timeline and approval cards without bespoke parsing.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from nexus.models.orchestration import (
    Approval,
    OrchestrationRun,
    RunStep,
)


class RunCreateRequest(BaseModel):
    goal: str = Field(..., max_length=60)
    input: dict = Field(default_factory=dict)
    account_id: str | None = None
    # Caller-supplied key to make submission idempotent (double-click / retry safe).
    idempotency_key: str | None = Field(default=None, max_length=120)


class RunStepOut(BaseModel):
    idx: int
    tool: str
    status: str
    attempts: int
    requires_approval: bool
    depends_on: list[int]
    approval_id: str | None = None
    error: str | None = None
    # Each step's own output, so every AI run's result is inspectable separately (not just the
    # merged blackboard). Empty until the step completes.
    output: dict = Field(default_factory=dict)

    @classmethod
    def from_model(cls, s: RunStep) -> "RunStepOut":
        return cls(
            idx=s.idx,
            tool=s.tool,
            status=s.status,
            attempts=s.attempts,
            requires_approval=s.requires_approval,
            depends_on=list(s.depends_on or []),
            approval_id=s.approval_id,
            error=s.error,
            output=s.output or {},
        )


class RunOut(BaseModel):
    id: str
    goal: str
    status: str
    account_id: str | None = None
    chat_session_id: str | None = None
    error: str | None = None
    created_at: datetime
    steps: list[RunStepOut] = Field(default_factory=list)
    blackboard: dict = Field(default_factory=dict)
    # Step progress as plain numbers, so a caller that does not need the full step list can still
    # show it. The runs LIST builds `RunOut` without steps — loading every step of every run, each
    # carrying its own `output` blob, to render one "3/5" label would be a lot of payload for a
    # number. So the list reported `steps=[]` and the UI computed "0/0 steps" for runs that had
    # completed successfully; a finished 25-account discovery run displayed as having done nothing.
    step_total: int = 0
    step_done: int = 0

    @classmethod
    def from_model(
        cls,
        run: OrchestrationRun,
        steps: list[RunStep] | None = None,
        *,
        step_total: int | None = None,
        step_done: int | None = None,
    ) -> "RunOut":
        """``steps`` populates the detail view; ``step_total``/``step_done`` let the list pass
        counts it aggregated separately. When steps ARE supplied the counts are derived from them,
        so the two can never disagree."""
        out = [RunStepOut.from_model(s) for s in (steps or [])]
        if steps is not None:
            step_total = len(out)
            step_done = sum(1 for s in out if s.status == "completed")
        return cls(
            id=run.id,
            goal=run.goal,
            status=run.status,
            account_id=run.account_id,
            chat_session_id=run.chat_session_id,
            error=run.error,
            created_at=run.created_at,
            steps=out,
            blackboard=run.blackboard or {},
            step_total=step_total or 0,
            step_done=step_done or 0,
        )


class ApprovalOut(BaseModel):
    id: str
    run_id: str
    step_id: str
    kind: str
    status: str
    payload: dict = Field(default_factory=dict)
    edits: dict = Field(default_factory=dict)  # reviewer edits + reject reason (no secrets)
    decided_at: datetime | None = None

    @classmethod
    def from_model(cls, a: Approval) -> "ApprovalOut":
        return cls(
            id=a.id,
            run_id=a.run_id,
            step_id=a.step_id,
            kind=a.kind,
            status=a.status,
            payload=a.payload or {},
            edits=a.edits or {},
            decided_at=a.decided_at,
        )


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    # Optional reviewer edits applied to the draft before it goes out (subject/body).
    edits: dict = Field(default_factory=dict)
    # On approve: which configured SMTP mailbox to send from (account id; default if omitted).
    from_account: str | None = None
    # On approve: "send" delivers; "draft" saves to the mailbox's Drafts for manual send.
    delivery_mode: Literal["send", "draft"] = "send"
    # On reject: why, for the audit trail and the rep who has to follow up.
    reason: str | None = Field(default=None, max_length=500)


class ApprovalRedraftRequest(BaseModel):
    """Reviewer instructions to regenerate a parked draft with AI (the approval stays pending)."""

    instructions: str = Field(min_length=1, max_length=1000)


class ResultColumn(BaseModel):
    key: str
    label: str
    kind: str


class ResultsResponse(BaseModel):
    """Server-side-filtered discovery results plus the dynamic custom-field columns the
    table should render. ``candidates`` stay as plain dicts — they are already a flat,
    frontend-ready projection written by the discovery agent."""

    run_id: str
    target: str | None = None
    total: int
    counts: dict = Field(default_factory=dict)
    columns: list[ResultColumn] = Field(default_factory=list)
    candidates: list[dict] = Field(default_factory=list)
