"""Relationship Graph endpoints (A1 search, A4 intro-paths): connect a network source, import or
sync its contacts, then search the deduped graph and map warm intros. Tenant-scoped + RBAC; OAuth
is never serialized out; cross-member reads pass the pooling visibility predicate."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.config import get_settings
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.identity import Membership
from nexus.models.network import NetworkPerson, NetworkSourceAccount
from nexus.network import oauth as network_oauth
from nexus.network.connectors.base import NetworkSyncBatch
from nexus.network.connectors.oauthbase import OAuthConnector  # noqa: F401
from nexus.network.connectors.registry import (
    get_network_connector,
    provider_configured,
)
from nexus.network.crypto import seal_tokens
from nexus.network.intro import intro_paths
from nexus.network.linkedin_csv import LinkedInCsvError, parse_linkedin_csv
from nexus.network.schemas import (
    ConnectRequest,
    ImportRequest,
    IngestResultOut,
    IntroPathOut,
    NetworkAccountOut,
    OAuthStartOut,
    PatchAccountRequest,
    PersonOut,
    SearchHitOut,
    SearchRequest,
    SyncEnqueuedOut,
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
    # OAuth providers must go through the consent flow (/oauth/{provider}/start), not a bare create.
    if body.provider in ("google", "microsoft"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"connect {body.provider} via /network/oauth/{body.provider}/start",
        )
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


@router.post("/accounts/{account_id}/sync", response_model=SyncEnqueuedOut)
async def sync_account(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> SyncEnqueuedOut:
    member = await _member(ts, principal)
    acc = await _owned_account(ts, member, account_id)
    await enqueue_sync_network_account(ts.tenant_id, acc.id)
    return SyncEnqueuedOut(enqueued=True, account_id=acc.id)


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
    # Only surface a person the member can actually see (owns or pooled edge). Return 404 (not 403)
    # for an invisible-but-existing person so we never reveal that the person id exists at all.
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


@router.get("/oauth/{provider}/start", response_model=OAuthStartOut)
async def oauth_start(
    provider: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> OAuthStartOut:
    if provider not in ("google", "microsoft"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown provider")
    if not provider_configured(provider):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{provider} is not configured")
    member = await _member(ts, principal)
    connector = get_network_connector(provider)
    verifier, challenge = network_oauth.make_pkce()
    state = network_oauth.sign_state(
        member_id=member.id, tenant_id=ts.tenant_id, provider=provider, verifier=verifier
    )
    return OAuthStartOut(authorize_url=connector.authorize_url_for(state=state,
                                                                   code_challenge=challenge))


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Provider redirect target. Validates state, exchanges the code, stores encrypted tokens, and
    bounces the browser back to the SPA. No auth dependency: the signed ``state`` is the credential."""
    base = get_settings().network_oauth_redirect_base.rstrip("/")
    if error or not code or not state:
        return RedirectResponse(f"{base}/network?error={error or 'oauth_failed'}")
    claims = network_oauth.verify_state(state)
    if not claims or claims.get("prov") != provider:
        return RedirectResponse(f"{base}/network?error=bad_state")

    connector = get_network_connector(provider)
    try:
        tokens = await connector.exchange_code(code=code, code_verifier=claims["pkce"])
    except Exception:
        return RedirectResponse(f"{base}/network?error=exchange_failed")

    import time as _time

    bundle = {
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token", ""),
        "expires_at": int(_time.time()) + int(tokens.get("expires_in", 3600)),
    }
    tid, member_id = claims["tid"], claims["mid"]
    # Bind the tenant the same way the request pipeline does (callback has no Principal).
    from nexus.workers.tasks import tenant_session as _tenant_session

    async with _tenant_session(tid) as ts:
        existing = await ts.first(
            NetworkSourceAccount,
            NetworkSourceAccount.member_id == member_id,
            NetworkSourceAccount.provider == provider,
        )
        if existing is None:
            existing = NetworkSourceAccount(
                member_id=member_id, user_id=claims.get("uid", member_id), provider=provider,
                external_account_id=f"{provider}:{member_id}",
                display_email=f"{provider.title()} account",
            )
            ts.add(existing)
        existing.oauth = seal_tokens(bundle)
        existing.status = "connected"
        existing.last_error = None
        await ts.flush()
        await enqueue_sync_network_account(tid, existing.id)
    return RedirectResponse(f"{base}/network?connected={provider}")


@router.post("/accounts/{account_id}/import-linkedin", response_model=IngestResultOut)
async def import_linkedin(
    account_id: str,
    file: UploadFile = File(...),
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> IngestResultOut:
    member = await _member(ts, principal)
    acc = await _owned_account(ts, member, account_id)
    content = await file.read()
    try:
        identities = parse_linkedin_csv(content)
    except LinkedInCsvError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    from nexus.network.connectors.base import NetworkSyncBatch
    from nexus.network.service import ingest_batch

    res = await ingest_batch(ts, acc, NetworkSyncBatch(identities=identities))
    return IngestResultOut(**res)
