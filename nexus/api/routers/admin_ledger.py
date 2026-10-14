"""The Ledger tab: is each store configured, reachable and at the right schema version (spec §18.2).

Gated on ``providers.manage``, the same permission as the connection strings themselves: applying a
schema and erasing a person are both acts on somebody else's database, and the people who hold those
credentials are the people who do them. No DSN is in any response — a store is reported by name,
state and version.

Erasure is here rather than on a tenant route on purpose: it deletes a person from every workspace's
contribution at once, which is a platform act, and the request names the person by address while the
job that does the work carries only their key.

**The stores are set up here too** (``PUT /admin/ledger/setup``, 2026-10-14). The connection strings
and the pseudonymisation secret were already Provider keys, and nothing on this tab said so: every
store read "Not configured" with no way forward from the screen that reported it. They are written
through the provider-key service, sealed and pinned like Mailbox apps' client secrets, and still
appear in no response.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

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
    # Where the connection string in use came from: "panel" (with the last four characters as
    # `hint`), "env" (the deployment's environment), or "" for none.
    source: str = ""
    hint: str = ""


class ErasureOut(BaseModel):
    """One erasure and what its last run found. Keys only: the request named an address, and
    this list must not become the record of whom we were asked to forget."""
    id: str
    key: str
    requested_at: str
    finished_at: str | None
    state: str            # done | incomplete | waiting
    deleted: dict
    remaining: dict
    unconfigured: list[str]
    errors: dict
    note: str


class LedgerStatusOut(BaseModel):
    capture_enabled: bool
    pseudonym_secret_configured: bool
    stores: list[StoreStatusOut]
    outbox: dict
    last_built_at: str | None
    consented_workspaces: int
    opted_out_workspaces: int
    undecided_workspaces: int
    recent_erasures: list[ErasureOut] = []


class LedgerSetupIn(BaseModel):
    """Omitted or empty leaves a value alone: a form that re-posts its blank password boxes must
    not erase a live connection string."""

    model_config = {"extra": "forbid"}

    archive_dsn: str | None = Field(default=None, max_length=2000)
    training_dsn: str | None = Field(default=None, max_length=2000)
    insights_dsn: str | None = Field(default=None, max_length=2000)
    generate_pseudonym_secret: bool = False


class ApplySchemaOut(BaseModel):
    store: str
    applied: list[str]
    status: StoreStatusOut


class ErasePersonIn(BaseModel):
    model_config = {"extra": "forbid"}

    email: EmailStr


class ErasePersonOut(BaseModel):
    queued: bool
    # "" when the pseudonymisation secret is not configured: the outbox is still cleared and the
    # person is never shipped, but there is no ledger key to search the stores by (nor anything in
    # them, since shipping needs that secret).
    person_key: str
    erasure_id: str = ""


@router.get("", response_model=LedgerStatusOut)
async def ledger_status(
    _: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> LedgerStatusOut:
    return await _status_out()


@router.put("/setup", response_model=LedgerStatusOut)
async def save_ledger_setup(
    body: LedgerSetupIn,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> LedgerStatusOut:
    """Store the connection strings, and create the pseudonymisation secret once.

    Every connection string is checked before any is written, by the SSRF guard ``stores.connect``
    applies (PostgreSQL only, no private or metadata host outside a local stack). The secret is
    GENERATED, never typed: nobody needs to know it, a typed one is usually guessable, and it is
    backed up with the database it is sealed in. Replacing one is refused here, because every
    archived row is sealed with a key derived from it and every person key is an HMAC under it:
    a new secret makes the archive unreadable and stops erasure finding anyone already archived."""
    from nexus.engagement import config
    from nexus.engagement.ledger import stores
    from nexus.sources.safety import SourceRejected, validate_dsn

    to_store: dict[str, str] = {}
    for store in stores.STORES:
        value = (getattr(body, f"{store}_dsn") or "").strip()
        if not value:
            continue
        try:
            validate_dsn(value, allow_private=stores._allow_private())
        except SourceRejected as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"{store.title()} store: {exc}") from exc
        to_store[f"ledger_{store}"] = value
    if body.generate_pseudonym_secret:
        if await config.pseudonym_secret():
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A pseudonymisation secret is already in use. Replacing it makes everything "
                "already archived unreadable and stops erasure finding anyone in it, so it is "
                "not done from this screen.")
        import secrets as _secrets

        to_store["ledger_pseudonym"] = _secrets.token_urlsafe(48)

    after: dict[str, dict] = {}
    for key_id, value in to_store.items():
        row = await _store_key(key_id, value, principal.user_id)
        # Never the value: the hint (last four characters) is enough to say which one is live.
        after[key_id] = {"set": True, "hint": row.key_hint}
    if after:
        await _audit(principal, "ledger.setup", "ledger", after)
    return await _status_out()


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
    """Erase a person from the outbox and every store, and never collect them again.

    The erasure is recorded BEFORE the job is queued, so the shipper stops archiving this person
    from this moment, whether or not a worker is running. Works without the pseudonymisation
    secret too: that is exactly when the outbox fills up with nowhere to ship it."""
    from nexus.engagement import config
    from nexus.engagement.ledger import erasures
    from nexus.engagement.ledger.pseudonym import person_key
    from nexus.workers.tasks import enqueue_ledger_erase_person

    secret = await config.pseudonym_secret()
    key = person_key(secret, str(body.email)) if secret else ""
    record = await erasures.record_request(str(body.email), person_key=key,
                                           user_id=principal.user_id)
    await enqueue_ledger_erase_person(key, erasure_id=record.id)
    # The address is never written to the audit row: the key is what the stores hold, and an audit
    # log of "who asked us to forget whom" would be the record they asked us to remove.
    await _audit(principal, "ledger.person_erased", key or record.id,
                 {"queued": True, "erasure_id": record.id})
    return ErasePersonOut(queued=True, person_key=key, erasure_id=record.id)


async def _status_out() -> LedgerStatusOut:
    from nexus.core.config import get_settings
    from nexus.engagement import config
    from nexus.engagement.ledger import builder, shipper, stores

    secret = await config.pseudonym_secret()
    try:
        built_at = await builder.last_built_at() if secret else None
    except Exception:
        built_at = None
    store_rows = []
    for store in stores.STORES:
        row = await stores.status(store)
        row["source"], row["hint"] = await _key_source(f"ledger_{store}", row["configured"])
        store_rows.append(StoreStatusOut(**row))
    return LedgerStatusOut(
        capture_enabled=get_settings().ledger_capture_enabled,
        pseudonym_secret_configured=bool(secret),
        stores=store_rows,
        outbox=await shipper.backlog(),
        last_built_at=built_at.isoformat() if built_at else None,
        recent_erasures=await _recent_erasures(),
        **await _consent_counts(),
    )


async def _key_source(key_id: str, configured: bool) -> tuple[str, str]:
    """Which value is live: the panel's (pinned first, then oldest, enabled only, the resolver's
    own order) or the environment's."""
    from nexus.providers.service import list_keys

    live = [k for k in await list_keys(key_id) if k.enabled]
    if live:
        return "panel", live[0].key_hint
    return ("env", "") if configured else ("", "")


async def _store_key(key_id: str, value: str, user_id: str):
    """Add the key (or find it, if this exact one is stored) and pin it, so it is the one in use:
    ``config.secret`` takes the first of the pool, which would otherwise stay the OLDEST key."""
    from nexus.providers.crypto import key_digest
    from nexus.providers.service import DuplicateKey, add_key, list_keys, prefer_key

    try:
        row = await add_key(key_id, "Set from Ledger", value, user_id=user_id)
    except DuplicateKey:
        digest = key_digest(value)
        row = next(k for k in await list_keys(key_id) if k.key_digest == digest)
    await prefer_key(row.id)
    return row


async def _recent_erasures() -> list[ErasureOut]:
    from nexus.engagement.ledger import erasures

    out = []
    for row in await erasures.recent():
        report = row.report or {}
        errors = dict(report.get("errors") or {})
        state = "done" if row.finished_at else ("incomplete" if errors else "waiting")
        out.append(ErasureOut(
            id=row.id, key=row.erasure_key[:8],
            requested_at=row.requested_at.isoformat(),
            finished_at=row.finished_at.isoformat() if row.finished_at else None,
            state=state, deleted=dict(report.get("deleted") or {}),
            remaining=dict(report.get("remaining") or {}),
            unconfigured=list(report.get("unconfigured") or []), errors=errors,
            note=str(report.get("stores_skipped") or ""),
        ))
    return out


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
