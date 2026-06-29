"""Relationship Graph endpoints (A1 search, A4 intro-paths): connect a network source, import or
sync its contacts, then search the deduped graph and map warm intros. Tenant-scoped + RBAC; OAuth
is never serialized out; cross-member reads pass the pooling visibility predicate."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.identity import Membership
from nexus.models.network import NetworkPerson, NetworkSourceAccount
from nexus.network.connectors.base import NetworkSyncBatch
from nexus.network.intro import intro_paths
from nexus.network.schemas import (
    ConnectRequest,
    ImportRequest,
    IngestResultOut,
    IntroPathOut,
    NetworkAccountOut,
    PatchAccountRequest,
    PersonOut,
    SearchHitOut,
    SearchRequest,
)
from nexus.network.search import search_network
from nexus.network.service import ingest_batch, set_pooling
from nexus.workers.tasks import enqueue_sync_network_account

router = APIRouter(prefix="/network", tags=["network"])


async def _member(ts: TenantSession, principal: Principal) -> Membership:
    m = await ts.first(Membership, Membership.user_id == principal.user_id)
    if m is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no membership for current user")
    return m


def _account_out(a: NetworkSourceAccount) -> NetworkAccountOut:
    return NetworkAccountOut(
        id=a.id, provider=a.provider, external_account_id=a.external_account_id,
        display_email=a.display_email, status=a.status, pooling_enabled=a.pooling_enabled,
        last_synced_at=a.last_synced_at.isoformat() if a.last_synced_at else None,
    )


def _person_out(p: NetworkPerson) -> PersonOut:
    return PersonOut(
        id=p.id, primary_email=p.primary_email, full_name=p.full_name,
        title=p.title, company=p.company, location=p.location,
    )


async def _owned_account(
    ts: TenantSession, member: Membership, account_id: str
) -> NetworkSourceAccount:
    acc = await ts.get(NetworkSourceAccount, account_id)
    if acc is None or acc.member_id != member.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")
    return acc


@router.post("/accounts", response_model=NetworkAccountOut, status_code=status.HTTP_201_CREATED)
async def connect_account(
    body: ConnectRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> NetworkAccountOut:
    member = await _member(ts, principal)
    acc = NetworkSourceAccount(
        member_id=member.id, user_id=principal.user_id, provider=body.provider,
        external_account_id=body.external_account_id,
        display_email=body.display_email or body.external_account_id,
    )
    ts.add(acc)
    await ts.flush()
    return _account_out(acc)


@router.get("/accounts", response_model=list[NetworkAccountOut])
async def list_accounts(
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> list[NetworkAccountOut]:
    member = await _member(ts, principal)
    rows = await ts.list(NetworkSourceAccount, NetworkSourceAccount.member_id == member.id)
    return [_account_out(a) for a in rows]


@router.patch("/accounts/{account_id}", response_model=NetworkAccountOut)
async def patch_account(
    account_id: str,
    body: PatchAccountRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> NetworkAccountOut:
    member = await _member(ts, principal)
    acc = await _owned_account(ts, member, account_id)
    if body.pooling_enabled is not None:
        await set_pooling(ts, acc, body.pooling_enabled)
    if body.status is not None:
        acc.status = body.status
    await ts.flush()
    return _account_out(acc)


@router.post("/accounts/{account_id}/import", response_model=IngestResultOut)
async def import_batch(
    account_id: str,
    body: ImportRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> IngestResultOut:
    member = await _member(ts, principal)
    acc = await _owned_account(ts, member, account_id)
    batch = NetworkSyncBatch(
        identities=body.identities, touchpoints=body.touchpoints, next_cursor=body.next_cursor
    )
    res = await ingest_batch(ts, acc, batch)
    return IngestResultOut(**res)


@router.post("/accounts/{account_id}/sync")
async def sync_account(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> dict:
    member = await _member(ts, principal)
    acc = await _owned_account(ts, member, account_id)
    await enqueue_sync_network_account(ts.tenant_id, acc.id)
    return {"enqueued": True, "account_id": acc.id}


@router.post("/search", response_model=list[SearchHitOut])
async def search(
    body: SearchRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> list[SearchHitOut]:
    member = await _member(ts, principal)
    hits = await search_network(ts, member_id=member.id, query=body.query, limit=body.limit)
    return [
        SearchHitOut(
            person=_person_out(h.person), score=round(h.score, 4),
            best_strength=h.best_strength, broker_member_ids=h.broker_member_ids,
        )
        for h in hits
    ]


@router.get("/people/{person_id}", response_model=PersonOut)
async def get_person(
    person_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> PersonOut:
    member = await _member(ts, principal)
    person = await ts.get(NetworkPerson, person_id)
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")
    # only surface a person the member can actually see (owns or pooled edge)
    paths = await intro_paths(ts, person_id=person_id, member_id=member.id)
    if not paths:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "person not found")
    return _person_out(person)


@router.get("/people/{person_id}/intro-paths", response_model=list[IntroPathOut])
async def get_intro_paths(
    person_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> list[IntroPathOut]:
    member = await _member(ts, principal)
    paths = await intro_paths(ts, person_id=person_id, member_id=member.id)
    return [
        IntroPathOut(
            broker_member_id=p.broker_member_id, broker_user_id=p.broker_user_id,
            relation=p.relation, strength=p.strength,
            last_touch_at=p.last_touch_at.isoformat() if p.last_touch_at else None,
            provider=p.provider,
        )
        for p in paths
    ]
