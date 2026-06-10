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
