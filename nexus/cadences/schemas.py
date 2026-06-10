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
