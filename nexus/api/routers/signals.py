"""Signal library: browse normalized GTM signals with filters and pagination."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import SignalOut
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.signal import SignalEvent

router = APIRouter(prefix="/signals", tags=["signals"])


def _signal_out(s: SignalEvent) -> SignalOut:
    return SignalOut(
        id=s.id,
        account_id=s.account_id,
        contact_id=s.contact_id,
        kind=s.kind,
        source=s.source,
        title=s.title,
        body=s.body,
        url=s.url,
        strength=s.strength,
        occurred_at=s.occurred_at.isoformat(),
    )


@router.get("", response_model=list[SignalOut])
async def list_signals(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
    account_id: str | None = None,
    kind: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[SignalOut]:
    where = []
    if account_id:
        where.append(SignalEvent.account_id == account_id)
    if kind:
        where.append(SignalEvent.kind == kind)
    stmt = (
        ts.select(SignalEvent, *where)
        .order_by(SignalEvent.occurred_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await ts.session.scalars(stmt)).all()
    return [_signal_out(s) for s in rows]
