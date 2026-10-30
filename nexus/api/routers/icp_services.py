"""Services a workspace sells, each with a full ICP of its own (W8, owner decision 2026-10-29).

The default service is the workspace's relevance profile, named ``default`` here; ``PUT
/relevance/profile`` still edits it, and so does ``PUT /relevance/services/default``. Every other
service is an ``icp_services`` row. Saved ICPs are normalised exactly as the profile's are (the
must-match options, the LinkedIn industry codes), so Populate and the daily sweep read a service's
ICP the way they read the profile's.
"""
from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.relevance import IcpService
from nexus.relevance import services as svc

logger = logging.getLogger("nexus.api.icp_services")

router = APIRouter(prefix="/relevance/services", tags=["relevance"])


class ServiceIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=120)
    source_url: str | None = Field(default=None, max_length=500)
    icp: dict = Field(default_factory=dict)
    value_props: list[dict] = Field(default_factory=list)
    product_context: str = Field(default="", max_length=20_000)


class ServiceOut(BaseModel):
    #: None for the default service, which the API also accepts as "default".
    id: str | None
    name: str
    is_default: bool
    source_url: str | None = None
    icp: dict = Field(default_factory=dict)
    value_props: list[dict] = Field(default_factory=list)
    product_context: str = ""
    #: New companies a day; None for the default means the platform default applies.
    daily_target: int | None = None
    archived_at: datetime | None = None
    #: Accounts this service found (Populate and daily discovery).
    accounts: int = 0
    #: Set on a save that changed the ICP: the screen asks "how many companies now?".
    icp_changed: bool = False


class ServiceFitOut(BaseModel):
    service_id: str | None
    name: str
    is_default: bool
    #: None while the service has no ICP to score against.
    icp_fit: int | None = None
    gate: dict | None = None
    reasons: list[str] = Field(default_factory=list)
    #: This is the service whose discovery found the account.
    found_here: bool = False


async def _found_counts(ts: TenantSession) -> dict[str | None, int]:
    from nexus.models.account import Account

    rows = (await ts.session.execute(
        select(Account.service_id, func.count())
        .where(Account.tenant_id == ts.tenant_id, Account.archived_at.is_(None))
        .group_by(Account.service_id))).all()
    return {service_id: int(n) for service_id, n in rows}


async def _default_out(ts: TenantSession, counts: dict | None = None) -> ServiceOut:
    from nexus.models.identity import Tenant
    from nexus.relevance.engine import get_or_create_profile

    profile = await get_or_create_profile(ts)
    tenant = await ts.session.get(Tenant, ts.tenant_id)
    counts = counts if counts is not None else await _found_counts(ts)
    return ServiceOut(
        id=None, name=svc.display_name(profile), is_default=True,
        icp=profile.icp or {}, value_props=profile.value_props or [],
        product_context=profile.product_context or "",
        daily_target=getattr(tenant, "icp_daily_count", None), accounts=counts.get(None, 0),
    )


def _service_out(service: IcpService, counts: dict | None = None) -> ServiceOut:
    return ServiceOut(
        id=service.id, name=service.name, is_default=False, source_url=service.source_url,
        icp=service.icp or {}, value_props=service.value_props or [],
        product_context=service.product_context or "", daily_target=service.daily_target or 0,
        archived_at=service.archived_at, accounts=(counts or {}).get(service.id, 0),
    )


def _checked_url(raw: str | None) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    url = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "The service page must be a web address, like acme.com/services/abc.")
    return url


async def _name_taken(ts: TenantSession, name: str, *, except_id: str | None) -> bool:
    """Two offered services with one name would be one choice in every picker."""
    wanted = name.strip().lower()
    profile = await svc.default_profile(ts)
    if except_id != svc.DEFAULT_ID and svc.display_name(profile).lower() == wanted:
        return True
    return any(s.name.strip().lower() == wanted and s.id != except_id
               for s in await svc.active_services(ts))


async def _normalised_icp(raw: dict, saved: dict) -> tuple[dict, bool]:
    """The ICP as the profile's is stored, and whether it changed (codes the server derived are
    not a change)."""
    from nexus.api.routers.relevance import _gate_options, _with_linkedin_codes, _without_codes

    icp = await _with_linkedin_codes(_gate_options(dict(raw or {})), dict(saved or {}))
    return icp, bool(_without_codes(saved or {}) != _without_codes(icp) and icp)


async def _rescore(tenant_id: str, service_id: str) -> None:
    """Score every account against this service's new ICP, as a job. A queue that is down leaves
    the stored fits stale; Account 360 re-scores a stale one when it is opened."""
    try:
        from nexus.workers.tasks import enqueue_score_service_fits

        await enqueue_score_service_fits(tenant_id, service_id)
    except Exception:
        logger.warning("could not queue re-scoring for service %s", service_id, exc_info=True)


@router.get("", response_model=list[ServiceOut])
async def list_services(
    include_archived: bool = False,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[ServiceOut]:
    """The default service first, then the others in the order they were added."""
    counts = await _found_counts(ts)
    rows = [await _default_out(ts, counts)]
    where = [] if include_archived else [IcpService.archived_at.is_(None)]
    stmt = ts.select(IcpService, *where).order_by(IcpService.created_at, IcpService.id)
    rows += [_service_out(s, counts) for s in (await ts.session.scalars(stmt)).all()]
    return rows


@router.post("", response_model=ServiceOut, status_code=status.HTTP_201_CREATED)
async def create_service(
    body: ServiceIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> ServiceOut:
    """Add a service. Its daily discovery starts OFF (``daily_target`` 0): adding a service never
    starts buying companies by itself; Settings turns it on."""
    if len(await svc.active_services(ts)) >= svc.MAX_ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"A workspace can offer up to {svc.MAX_ACTIVE} services. Archive one "
                            "it no longer sells first.")
    if await _name_taken(ts, body.name, except_id=None):
        raise HTTPException(status.HTTP_409_CONFLICT, f"There is already a service called "
                            f"{body.name.strip()}.")
    icp, changed = await _normalised_icp(body.icp, {})
    service = IcpService(
        tenant_id=ts.tenant_id, name=body.name.strip(), source_url=_checked_url(body.source_url),
        icp=icp, value_props=body.value_props, product_context=body.product_context,
        daily_target=0,
    )
    ts.add(service)
    await ts.flush()
    if icp:
        await _rescore(ts.tenant_id, service.id)
    out = _service_out(service)
    out.icp_changed = changed
    return out


@router.put("/{service_id}", response_model=ServiceOut)
async def update_service(
    service_id: str,
    body: ServiceIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> ServiceOut:
    """Save a service's name, page and ICP. ``default`` saves the workspace's profile, as ``PUT
    /relevance/profile`` does, and names it."""
    if await _name_taken(ts, body.name, except_id=service_id):
        raise HTTPException(status.HTTP_409_CONFLICT, f"There is already a service called "
                            f"{body.name.strip()}.")
    if svc.normalise_id(service_id) is None:
        from nexus.relevance.engine import get_or_create_profile

        profile = await get_or_create_profile(ts)
        icp, changed = await _normalised_icp(body.icp, profile.icp or {})
        profile.icp, profile.value_props = icp, body.value_props
        profile.product_context = body.product_context
        name = body.name.strip()
        profile.name = None if name == svc.DEFAULT_NAME else name
        await ts.flush()
        out = await _default_out(ts)
        out.icp_changed = changed
        return out
    service = await svc.get_service(ts, service_id)
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service not found")
    icp, changed = await _normalised_icp(body.icp, service.icp or {})
    service.name = body.name.strip()
    service.source_url = _checked_url(body.source_url)
    service.icp, service.value_props = icp, body.value_props
    service.product_context = body.product_context
    await ts.flush()
    if changed:
        await _rescore(ts.tenant_id, service.id)
    out = _service_out(service, await _found_counts(ts))
    out.icp_changed = changed
    return out


@router.post("/{service_id}/archive", response_model=ServiceOut)
async def archive_service(
    service_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> ServiceOut:
    """Stop offering a service: it leaves the pickers and its daily discovery stops. Accounts it
    found keep it, and campaigns built for it keep pitching it. The default cannot be archived."""
    service = await svc.get_service(ts, service_id)
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service not found")
    if service.archived_at is None:
        from nexus.core.db import utcnow

        service.archived_at = utcnow()
        await ts.flush()
    return _service_out(service, await _found_counts(ts))


@router.post("/{service_id}/restore", response_model=ServiceOut)
async def restore_service(
    service_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_relevance)),
) -> ServiceOut:
    service = await svc.get_service(ts, service_id)
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service not found")
    if service.archived_at is not None:
        if len(await svc.active_services(ts)) >= svc.MAX_ACTIVE:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"A workspace can offer up to {svc.MAX_ACTIVE} services.")
        if await _name_taken(ts, service.name, except_id=service.id):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"Another service is now called {service.name}. Rename it first.")
        service.archived_at = None
        await ts.flush()
        await _rescore(ts.tenant_id, service.id)
    return _service_out(service, await _found_counts(ts))


@router.get("/fits/{account_id}", response_model=list[ServiceFitOut])
async def account_service_fits(
    account_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> list[ServiceFitOut]:
    """Account 360: this account's ICP fit against every service the workspace sells."""
    from nexus.models.account import Account

    account = await ts.get(Account, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return [ServiceFitOut(**row) for row in await svc.fits_for_account(ts, account)]
