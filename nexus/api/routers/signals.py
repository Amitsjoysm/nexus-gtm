"""Signal library: browse normalized GTM signals with filters and pagination."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.api.schemas import SignalOut
from nexus.core.db import utcnow
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
    max_age_days: int | None = Query(default=None, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[SignalOut]:
    where = []
    if account_id:
        where.append(SignalEvent.account_id == account_id)
    if kind:
        where.append(SignalEvent.kind == kind)
    if max_age_days is not None:
        # Recency window (e.g. 7/15/30/60/90 days). Server-side so pagination stays correct;
        # backed by the (tenant_id, occurred_at) composite index on signal_events.
        where.append(SignalEvent.occurred_at >= utcnow() - timedelta(days=max_age_days))
    stmt = (
        ts.select(SignalEvent, *where)
        .order_by(SignalEvent.occurred_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await ts.session.scalars(stmt)).all()
    return [_signal_out(s) for s in rows]


# ---- collection preferences -------------------------------------------------------------------
#
# A tester asked why signals they had never enabled were appearing, and whether they were being
# billed for them. `signal_sources` is a deployment-global setting naming which COLLECTORS run;
# this is the per-workspace control over which KINDS get kept.


class SignalPreferenceOut(BaseModel):
    kind: str
    enabled: bool


class SignalPreferenceIn(BaseModel):
    """`extra="forbid"` so a typo'd field is a 422 rather than a silently ignored setting."""

    model_config = {"extra": "forbid"}

    enabled: bool


@router.get("/preferences", response_model=list[SignalPreferenceOut])
async def list_signal_preferences(
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[SignalPreferenceOut]:
    """Every known signal kind with its effective state.

    Built from the catalogue and overlaid with stored rows, never from the rows alone — a screen
    listing only what somebody already toggled cannot be used to toggle anything the first time.
    """
    from nexus.ingestion.preferences import current_preferences

    return [
        SignalPreferenceOut(kind=kind, enabled=enabled)
        for kind, enabled in (await current_preferences(ts)).items()
    ]


@router.put("/preferences/{kind}", response_model=SignalPreferenceOut)
async def set_signal_preference(
    kind: str,
    body: SignalPreferenceIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> SignalPreferenceOut:
    """Switch one signal kind on or off for this workspace.

    An unknown kind is refused rather than stored: a row for a kind nothing emits is invisible
    configuration that reads as active and never applies — the same trap `runtime_config` avoids by
    skipping rows whose key has left the catalogue.
    """
    from nexus.ingestion.preferences import set_kind
    from nexus.ingestion.service import SIGNAL_KINDS

    if kind not in SIGNAL_KINDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"unknown signal kind {kind!r}. Known kinds: {', '.join(sorted(SIGNAL_KINDS))}",
        )
    row = await set_kind(ts, kind, enabled=body.enabled)
    await ts.commit()
    return SignalPreferenceOut(kind=row.kind, enabled=bool(row.enabled))
