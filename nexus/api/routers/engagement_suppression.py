# nexus/api/routers/engagement_suppression.py
"""Do-not-contact list for the workspace (spec §9, D7).

Any member can see the list, add an address or a whole domain, upload a list of either, and check
which addresses on screen are blocked, because an SDR about to email someone needs to know. Lifting a block needs ``manage_engagement``
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
    # For a domain block this is `@acme.io`, the stored key; `kind` says which it is.
    email: str
    kind: str = "email"
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
        from nexus.engagement.suppression.service import PERMANENT, is_domain_block

        return cls(
            id=row.id, email=row.email, kind="domain" if is_domain_block(row) else "email",
            reason=row.reason, contact_id=row.contact_id,
            source_message_id=row.source_message_id, created_by_user_id=row.created_by_user_id,
            created_at=row.created_at, lifted_at=row.lifted_at,
            lifted_by_user_id=row.lifted_by_user_id, lift_note=row.lift_note or "",
            liftable=row.lifted_at is None and row.reason not in PERMANENT,
        )


class AddIn(BaseModel):
    model_config = {"extra": "forbid"}

    # An address or a domain; the field keeps its old name so an older client still works.
    email: str = Field(min_length=3, max_length=320)


#: Rows per upload request. The page sends a longer file in batches of this size, so one request
#: stays one short transaction.
BULK_MAX = 1000


class BulkIn(BaseModel):
    model_config = {"extra": "forbid"}

    entries: list[str] = Field(min_length=1, max_length=BULK_MAX)


class BulkOut(BaseModel):
    emails_blocked: int
    domains_blocked: int
    already_blocked: int
    # The lines that were neither, as written, so the operator can see what was skipped. Capped:
    # a file of 1,000 lines of prose should not come back as 1,000 lines.
    unreadable: list[str]
    unreadable_count: int


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
    from nexus.engagement.suppression.service import block_entry

    try:
        row, _created = await block_entry(ts, body.email, created_by_user_id=principal.user_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return BlockOut.of(row)


@router.post("/bulk", response_model=BulkOut)
async def bulk_block(
    body: BulkIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> BulkOut:
    """Block every address and domain in an uploaded list, as manual blocks.

    Each line is read on its own: an address blocks that person, anything else that reads as a
    domain blocks everyone there. A header row, a blank line or prose is reported back, never
    guessed at. Duplicates in the file, and entries already blocked, are counted, not doubled.
    """
    from nexus.engagement.suppression.service import block_entry, classify_entry

    emails = domains = already = 0
    unreadable: list[str] = []
    seen: set[tuple[str, str]] = set()
    for raw in body.entries:
        kind, value = classify_entry(raw)
        if kind is None:
            if value:
                unreadable.append(value[:200])
            continue
        if (kind, value) in seen:
            continue
        seen.add((kind, value))
        _row, created = await block_entry(ts, value if kind == "email" else "@" + value,
                                          created_by_user_id=principal.user_id)
        if not created:
            already += 1
        elif kind == "email":
            emails += 1
        else:
            domains += 1
    return BulkOut(emails_blocked=emails, domains_blocked=domains, already_blocked=already,
                   unreadable=unreadable[:20], unreadable_count=len(unreadable))


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
