"""Prospector (``/prospector``): search a B2B database of contacts and companies, and take records.

Thin on purpose: each endpoint checks the role (every action is rep+, decision D9, through
``manage_accounts``), checks the module gate (``module.prospector``: a platform switch always
refuses, a plan only under enforcement), calls one service function, and turns a
``ProspectorError`` into its HTTP status. The services own every rule.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.prospector.errors import ProspectorError
from nexus.prospector.schemas import FieldCatalogOut, SavedIn, SavedOut, SearchIn, SearchOut

logger = logging.getLogger("nexus.api.routers.prospector")

router = APIRouter(prefix="/prospector", tags=["prospector"])

MODULE = "module.prospector"


async def module_gate(ts: TenantSession = Depends(get_tenant_session)) -> TenantSession:
    """402 when the module is switched off for everyone, or off the plan under enforcement."""
    from nexus.billing.entitlements import preflight

    (await preflight(ts, MODULE)).raise_if_blocked()
    return ts


def _http(exc: ProspectorError) -> HTTPException:
    return HTTPException(exc.status, detail=exc.detail, headers=exc.headers or None)


@router.get("/filters", response_model=FieldCatalogOut)
async def filters(
    view: Literal["contacts", "companies"],
    _ts: TenantSession = Depends(module_gate),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> FieldCatalogOut:
    from nexus.prospector.fields import catalog

    return FieldCatalogOut(**catalog(view))


@router.post("/search", response_model=SearchOut)
async def search(
    body: SearchIn,
    ts: TenantSession = Depends(module_gate),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> SearchOut:
    from nexus.prospector import search as service

    try:
        return await service.search(ts, user_id=principal.user_id, body=body)
    except ProspectorError as exc:
        raise _http(exc) from None


@router.post("/saved", response_model=SavedOut)
async def saved(
    body: SavedIn,
    ts: TenantSession = Depends(module_gate),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> SavedOut:
    from nexus.prospector.saved import saved_page

    try:
        return await saved_page(ts, view=body.view, filters=body.filters,
                                workspace=body.workspace, page=body.page,
                                page_size=body.page_size)
    except ProspectorError as exc:
        raise _http(exc) from None
