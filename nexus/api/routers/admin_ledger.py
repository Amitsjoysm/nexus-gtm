"""The Ledger tab: is each store configured, reachable and at the right schema version (spec §18.2).

Gated on ``providers.manage``, the same permission as the connection strings themselves: applying a
schema and erasing a person are both acts on somebody else's database, and the people who hold those
credentials are the people who do them. No DSN is in any response — a store is reported by name,
state and version.

Erasure is here rather than on a tenant route on purpose: it deletes a person from every workspace's
contribution at once, which is a platform act, and the request names the person by address while the
job that does the work carries only their key.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.permissions import PROVIDERS_MANAGE

router = APIRouter(prefix="/admin/ledger", tags=["admin-ledger"])


class StoreStatusOut(BaseModel):
    store: str
    configured: bool
    reachable: bool
    owns_schema: bool
    applied: list[str]
    pending: list[str]
    detail: str


class LedgerStatusOut(BaseModel):
    capture_enabled: bool
    pseudonym_secret_configured: bool
    stores: list[StoreStatusOut]
    outbox: dict
    last_built_at: str | None
    consented_workspaces: int
    opted_out_workspaces: int
    undecided_workspaces: int


class ApplySchemaOut(BaseModel):
    store: str
    applied: list[str]
    status: StoreStatusOut


class ErasePersonIn(BaseModel):
    model_config = {"extra": "forbid"}

    email: EmailStr


class ErasePersonOut(BaseModel):
    queued: bool
    person_key: str


@router.get("", response_model=LedgerStatusOut)
async def ledger_status(
    _: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> LedgerStatusOut:
    from nexus.core.config import get_settings
    from nexus.engagement import config
    from nexus.engagement.ledger import builder, shipper, stores

    secret = await config.pseudonym_secret()
    try:
        built_at = await builder.last_built_at() if secret else None
    except Exception:
        built_at = None
    return LedgerStatusOut(
        capture_enabled=get_settings().ledger_capture_enabled,
        pseudonym_secret_configured=bool(secret),
        stores=[StoreStatusOut(**await stores.status(store)) for store in stores.STORES],
        outbox=await shipper.backlog(),
        last_built_at=built_at.isoformat() if built_at else None,
        **await _consent_counts(),
    )


@router.post("/{store}/schema", response_model=ApplySchemaOut)
async def apply_store_schema(
    store: str,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> ApplySchemaOut:
    from nexus.engagement.ledger import schema, stores

    if store not in stores.STORES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown ledger store")
    try:
        applied = await schema.apply_schema(store)
    except stores.StoreNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This store has no connection string yet") from exc
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"The store refused the schema: {type(exc).__name__}") from exc
    await _audit(principal, "ledger.schema_applied", store, {"applied": applied})
    return ApplySchemaOut(store=store, applied=applied,
                          status=StoreStatusOut(**await stores.status(store)))


@router.post("/erase-person", response_model=ErasePersonOut)
async def erase_person(
    body: ErasePersonIn,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> ErasePersonOut:
    from nexus.engagement import config
    from nexus.engagement.ledger.pseudonym import person_key
    from nexus.workers.tasks import enqueue_ledger_erase_person

    secret = await config.pseudonym_secret()
    if not secret:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "The pseudonymisation secret is not configured, so nothing can be "
                            "found by person key")
    key = person_key(secret, str(body.email))
    await enqueue_ledger_erase_person(key)
    # The address is never written to the audit row: the key is what the stores hold, and an audit
    # log of "who asked us to forget whom" would be the record they asked us to remove.
    await _audit(principal, "ledger.person_erased", key, {"queued": True})
    return ErasePersonOut(queued=True, person_key=key)


async def _consent_counts() -> dict:
    """How many workspaces contribute. Cross-tenant, so it runs on the platform sessionmaker."""
    from sqlalchemy import func, select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.identity import Tenant
    from nexus.models.ledger import TrainingConsent

    async with get_platform_sessionmaker()() as session:
        newest = (
            select(TrainingConsent.tenant_id,
                   func.max(TrainingConsent.decided_at).label("decided_at"))
            .group_by(TrainingConsent.tenant_id)
            .subquery()
        )
        rows = (await session.execute(
            select(TrainingConsent.status, func.count())
            .join(newest, (TrainingConsent.tenant_id == newest.c.tenant_id)
                  & (TrainingConsent.decided_at == newest.c.decided_at))
            .group_by(TrainingConsent.status)
        )).all()
        decided = {status_value: int(count) for status_value, count in rows}
        tenants = (await session.execute(select(func.count(Tenant.id)))).scalar_one()
    on, off = decided.get("on", 0), decided.get("off", 0)
    return {"consented_workspaces": on, "opted_out_workspaces": off,
            "undecided_workspaces": max(int(tenants) - on - off, 0)}


async def _audit(principal: Principal, action: str, target: str, after: dict) -> None:
    from nexus.billing.audit import record_admin_action
    from nexus.core.db import get_platform_sessionmaker

    async with get_platform_sessionmaker()() as session:
        await record_admin_action(session, actor=principal.user_id, action=action,
                                  target=target, after=after)
        await session.commit()
