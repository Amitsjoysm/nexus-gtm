# nexus/api/routers/engagement_suppression.py
"""Do-not-contact list for the workspace (spec §9, D7).

Any member can see the list, add an address and check which addresses on screen are blocked,
because an SDR about to email someone needs to know. Lifting a block needs ``manage_engagement``
and a note, and an unsubscribe is never liftable (409), whoever asks.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.engagement import DoNotContact

router = APIRouter(prefix="/engagement/do-not-contact", tags=["engagement"])


class BlockOut(BaseModel):
    id: str
    email: str
    reason: str
    contact_id: str | None
    source_message_id: str | None
    created_by_user_id: str | None
    created_at: datetime
    lifted_at: datetime | None
    lifted_by_user_id: str | None
    lift_note: str
    liftable: bool

    @classmethod
    def of(cls, row: DoNotContact) -> "BlockOut":
        from nexus.engagement.suppression.service import PERMANENT

        return cls(
            id=row.id, email=row.email, reason=row.reason, contact_id=row.contact_id,
            source_message_id=row.source_message_id, created_by_user_id=row.created_by_user_id,
            created_at=row.created_at, lifted_at=row.lifted_at,
            lifted_by_user_id=row.lifted_by_user_id, lift_note=row.lift_note or "",
            liftable=row.lifted_at is None and row.reason not in PERMANENT,
        )


class AddIn(BaseModel):
    model_config = {"extra": "forbid"}

    email: str = Field(min_length=3, max_length=320)


class LiftIn(BaseModel):
    model_config = {"extra": "forbid"}

    note: str = Field(min_length=5, max_length=2000)


class CheckIn(BaseModel):
    model_config = {"extra": "forbid"}

    emails: list[str] = Field(default_factory=list, max_length=500)


@router.get("", response_model=list[BlockOut])
async def list_blocks(
    q: str = "",
    include_lifted: bool = False,
    limit: int = 100,
    offset: int = 0,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_engagement)),
) -> list[BlockOut]:
    from nexus.engagement.suppression.service import list_blocks as _list

    rows = await _list(ts, q=q, include_lifted=include_lifted, limit=limit, offset=offset)
    return [BlockOut.of(r) for r in rows]


@router.post("", response_model=BlockOut, status_code=status.HTTP_201_CREATED)
async def add_block(
    body: AddIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> BlockOut:
    from nexus.engagement.suppression.service import suppress

    try:
        row = await suppress(ts, email=body.email, reason="manual",
                             created_by_user_id=principal.user_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return BlockOut.of(row)


@router.post("/check", response_model=dict[str, str])
async def check_blocks(
    body: CheckIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_engagement)),
) -> dict[str, str]:
    """``{address: reason}`` for the blocked addresses among ``emails``; for badges on lists."""
    from nexus.engagement.suppression.service import active_reasons

    return await active_reasons(ts, body.emails)


@router.post("/{block_id}/lift", response_model=BlockOut)
async def lift_block(
    block_id: str,
    body: LiftIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_engagement)),
) -> BlockOut:
    from nexus.engagement.suppression.service import PermanentBlock, lift

    row = await ts.get(DoNotContact, block_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Block not found")
    try:
        await lift(ts, row, user_id=principal.user_id, note=body.note)
    except PermanentBlock as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return BlockOut.of(row)
