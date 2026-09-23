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


class EngagementStatusOut(BaseModel):
    #: The engine's switch. While it is off every campaign, reply desk and template route 404s,
    #: so the screens hide rather than offer pages that cannot load.
    engine_on: bool
    can_manage: bool


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
        # What has already been shipped is removed by the deletion job, which writes its
        # verification report (rows remaining, expected zero) to the audit log (spec §18.6).
        from nexus.workers.tasks import enqueue_ledger_delete_workspace

        await enqueue_ledger_delete_workspace(ts.tenant_id)
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


@router.get("/status", response_model=EngagementStatusOut)
async def get_engagement_status(
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> EngagementStatusOut:
    """Answered whatever the switch says. Everything else about campaigns is dark while it is
    off, so this is the one route the navigation can ask."""
    from nexus.engagement import config

    return EngagementStatusOut(
        engine_on=config.campaigns_enabled(),
        can_manage=has_permission(Role(principal.role), Permission.manage_engagement),
    )


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
