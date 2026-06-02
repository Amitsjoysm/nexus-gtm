"""Alerts endpoints: list and acknowledge tenant-scoped notifications."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from nexus.alerts.service import get_alert_service
from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import AlertOut
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.alerts import Alert

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _alert_out(a: Alert) -> AlertOut:
    return AlertOut(
        id=a.id,
        title=a.title,
        body=a.body,
        severity=a.severity,
        channel=a.channel,
        status=a.status,
        account_id=a.account_id,
        signal_id=a.signal_id,
        source=a.source,
        meta=a.meta or {},
    )


@router.get("", response_model=list[AlertOut])
async def list_alerts(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[AlertOut]:
    alerts = await get_alert_service().list(
        ts, status=status_filter, limit=limit, offset=offset
    )
    return [_alert_out(a) for a in alerts]


@router.post("/{alert_id}/ack", response_model=AlertOut)
async def acknowledge_alert(
    alert_id: str,
    principal: Principal = Depends(require(Permission.manage_accounts)),
    ts: TenantSession = Depends(get_tenant_session),
) -> AlertOut:
    alert = await get_alert_service().acknowledge(ts, alert_id, user_id=principal.user_id)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert not found")
    return _alert_out(alert)
