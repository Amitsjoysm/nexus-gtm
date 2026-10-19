"""Prospector (``/prospector``): search a B2B database of contacts and companies, and take records.

Thin on purpose: each endpoint checks the role (every action is rep+, decision D9, through
``manage_accounts``), checks the module gate (``module.prospector``: a platform switch always
refuses, a plan only under enforcement), calls one service function, and turns a
``ProspectorError`` into its HTTP status. The services own every rule.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.prospector.errors import ProspectorError
from nexus.prospector.schemas import (
    DeliverIn,
    DeliverOut,
    ExportIn,
    FieldCatalogOut,
    QuoteIn,
    QuoteOut,
    SavedIn,
    SavedOut,
    SearchIn,
    SearchOut,
    VerifyIn,
    VerifyOut,
)

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


@router.post("/quote", response_model=QuoteOut)
async def quote(
    body: QuoteIn,
    ts: TenantSession = Depends(module_gate),
    _: Principal = Depends(require(Permission.manage_accounts)),
) -> QuoteOut:
    """What a delivery would cost, field by field, against the balance. Spends nothing."""
    from nexus.billing.credits import balance
    from nexus.prospector import charges
    from nexus.prospector.deliver import prepare
    from nexus.prospector.search import client

    try:
        q, _bought = await prepare(ts, selection=body.selection, action=body.action,
                                   include_phones=body.include_phones, client=client())
    except ProspectorError as exc:
        raise _http(exc) from None
    rates = await charges.rate_cards(ts)
    credits = {cap: qty * rates.get(cap, 0.0) for cap, qty in q.charges.items()}
    total = round(sum(credits.values()), 4)
    try:
        held_balance = float(await balance(ts))
    except Exception:
        held_balance = None
    return QuoteOut(
        records=len(q.lines), charges=q.charges, credits=credits, total_credits=total,
        already_paid=q.already_paid, held=q.held, unavailable=q.unavailable,
        balance=held_balance, enough=held_balance is None or held_balance >= total,
    )


@router.post("/deliver", response_model=DeliverOut)
async def deliver(
    body: DeliverIn,
    ts: TenantSession = Depends(module_gate),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> DeliverOut:
    """Save, list, reveal or list-their-companies, charged once per field (D4-D6)."""
    from nexus.prospector import deliver as service
    from nexus.prospector.search import client

    try:
        return await service.deliver(
            ts, user_id=principal.user_id, role=principal.role, selection=body.selection,
            action=body.action, list_id=body.list_id, include_phones=body.include_phones,
            request_id=body.request_id, client=client())
    except ProspectorError as exc:
        raise _http(exc) from None


@router.post("/export")
async def export(
    body: ExportIn,
    ts: TenantSession = Depends(module_gate),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> Response:
    """The selected rows as CSV, charged like a delivery. The charge is committed before the file
    is returned, so a download that fails is retried (with a new request id) for free."""
    from nexus.api.csv_export import csv_response
    from nexus.prospector.export import export_rows
    from nexus.prospector.search import client

    try:
        filename, header, rows = await export_rows(
            ts, user_id=principal.user_id, selection=body.selection,
            include_phones=body.include_phones, request_id=body.request_id, client=client())
    except ProspectorError as exc:
        raise _http(exc) from None
    await ts.commit()
    return csv_response(filename, header, rows)


@router.post("/verify", response_model=VerifyOut)
async def verify(
    body: VerifyIn,
    ts: TenantSession = Depends(module_gate),
    principal: Principal = Depends(require(Permission.manage_accounts)),
) -> VerifyOut:
    """Verify selected contacts' addresses: free for Prospector-delivered ones (D20), up to 25
    inline and the rest in the background."""
    from nexus.prospector.verify import verify_contacts

    return VerifyOut(**await verify_contacts(ts, user_id=principal.user_id,
                                             contact_ids=body.contact_ids))
