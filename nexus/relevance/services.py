"""Services: a full ICP per service a company sells (W8, owner decision 2026-10-29).

The workspace's ``RelevanceProfile`` is the DEFAULT service and every other service is an
``IcpService`` row shaped like it (``icp``, ``value_props``, ``product_context``), so everything
that scores, drafts or discovers against "the profile" takes a service unchanged.

**Which service applies** is answered in one place, in this order:

1. ``using_service(id)``: work a campaign does pitches the campaign's service. A drafted email, a
   call script and the campaign's ICP check run inside it, and ``engine.get_profile`` returns that
   service there. A context variable rather than a parameter on each reader: the drafting context,
   Find contacts and the agent runtime all read ``get_profile(ts)`` several calls below the code
   that knows the campaign, and threading an argument through them would be three places to miss.
   Set and reset around one piece of work, like ``agents.llm.using_model``.
2. The account's ``service_id``: an account found by a service is worked under it (Find contacts
   titles, a call script outside a campaign, its score) unless a campaign says otherwise.
3. The default service.

A workspace that adds no service never sets a scope and has no account with a ``service_id``, so
all three resolve to the profile: exactly what it had before (pinned in ``tests/test_icp_services``).

**Fit is scored per service** (``AccountServiceFit``): every account against every active service,
written when an account is delivered, on each scoring refresh, and when a service's ICP is saved.
The default service's fit stays in ``account_scores``, which five readers treat as the account's
score.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import AsyncIterator, Iterator

from nexus.core.db import utcnow
from nexus.models.relevance import AccountServiceFit, IcpService, RelevanceProfile

logger = logging.getLogger("nexus.relevance.services")

#: What the default service is called until the workspace names it.
DEFAULT_NAME = "Main service"
#: How the API names the default service. Never stored: a NULL ``service_id`` is the default.
DEFAULT_ID = "default"
#: How many services one workspace may keep active. A picker longer than this is not a picker.
MAX_ACTIVE = 20

# None: no scope (resolve from the account, then the default). "": the default, explicitly. An id:
# that service.
_scope: ContextVar[str | None] = ContextVar("nexus_icp_service", default=None)


def normalise_id(service_id: str | None) -> str | None:
    """The stored form: None for the default service, however it was named."""
    service_id = (service_id or "").strip()
    return None if service_id in ("", DEFAULT_ID) else service_id


@contextmanager
def using_service(service_id: str | None) -> Iterator[None]:
    """Work inside the block pitches ``service_id``; None or "default" is the default service,
    explicitly, so an account found by another service does not pull its own in."""
    token = _scope.set(normalise_id(service_id) or "")
    try:
        yield
    finally:
        _scope.reset(token)


def scoped_service_id() -> str | None:
    """None outside any scope, "" inside an explicit default scope, else the service id."""
    return _scope.get()


@asynccontextmanager
async def campaign_service(ts, campaign_id: str | None) -> AsyncIterator[None]:
    """``using_service`` for the service a campaign pitches. A missing campaign leaves the scope as
    it was, so a draft outside any campaign behaves exactly as before."""
    service_id: str | None = None
    found = False
    if campaign_id:
        from nexus.models.engagement import EngagementCampaign

        campaign = await ts.get(EngagementCampaign, campaign_id)
        if campaign is not None:
            found, service_id = True, campaign.service_id
    if not found:
        yield
        return
    with using_service(service_id):
        yield


async def get_service(ts, service_id: str | None) -> IcpService | None:
    """This workspace's service, archived or not; None for the default or an unknown id."""
    service_id = normalise_id(service_id)
    if service_id is None:
        return None
    return await ts.get(IcpService, service_id)


async def default_profile(ts) -> RelevanceProfile | None:
    return await ts.first(RelevanceProfile)


async def profile_for(ts, service_id: str | None):
    """The service named, or the default profile when it names the default or nothing known."""
    service = await get_service(ts, service_id)
    return service if service is not None else await default_profile(ts)


async def resolve_profile(ts, *, account=None):
    """The ICP work on ``account`` runs against: the scope, else the account's service, else the
    default (the order in the module docstring)."""
    scoped = scoped_service_id()
    if scoped is not None:
        return await profile_for(ts, scoped)
    return await profile_for(ts, getattr(account, "service_id", None))


async def profile_for_campaign(ts, campaign):
    return await profile_for(ts, getattr(campaign, "service_id", None))


async def active_services(ts) -> list[IcpService]:
    """Every service still offered, oldest first (the order the workspace added them)."""
    stmt = (ts.select(IcpService, IcpService.archived_at.is_(None))
            .order_by(IcpService.created_at, IcpService.id))
    return list((await ts.session.scalars(stmt)).all())


async def names(ts) -> dict[str | None, str]:
    """``{service_id: name}`` for every service, archived included; None is the default."""
    profile = await default_profile(ts)
    out: dict[str | None, str] = {None: display_name(profile)}
    for service in await ts.list(IcpService):
        out[service.id] = service.name
    return out


def display_name(profile) -> str:
    return ((getattr(profile, "name", None) or "").strip()) or DEFAULT_NAME


def has_icp(profile) -> bool:
    return bool(profile is not None and (getattr(profile, "icp", None) or {}))


# ---- fit per service -----------------------------------------------------------------------------

def fit_against(service, account) -> tuple[int, dict | None, list[str]]:
    """``(icp_fit, gate, reasons)`` of ``account`` against ``service``. Deterministic, no I/O: the
    same `score_icp_fit` and `icp_gate` the default service is scored and gated with."""
    from nexus.relevance import get_relevance_engine
    from nexus.relevance.gate import icp_gate, mandatory_fields

    icp = dict(getattr(service, "icp", None) or {})
    fit = get_relevance_engine().score_icp_fit(service, account)
    gate = icp_gate(icp, account).as_dict() if mandatory_fields(icp) else None
    return int(fit.score), gate, list(fit.reasons)[:6]


async def score_services(ts, accounts, *, services=None) -> int:
    """Write each account's fit against each active service. Returns rows written. Never raises:
    a refresh, a delivery or a save must not fail because a fit could not be stored."""
    accounts = [a for a in (accounts or []) if getattr(a, "id", None)]
    if not accounts:
        return 0
    try:
        services = list(services) if services is not None else await active_services(ts)
        if not services:
            return 0
        existing = {
            (row.account_id, row.service_id): row
            for row in await ts.list(
                AccountServiceFit,
                AccountServiceFit.account_id.in_([a.id for a in accounts]),
                AccountServiceFit.service_id.in_([s.id for s in services]),
            )
        }
        now = utcnow()
        written = 0
        # In a SAVEPOINT, which flushes the caller's own pending rows first: a second writer racing
        # on one (account, service) costs this write, never the delivery or refresh around it.
        async with ts.session.begin_nested():
            for account in accounts:
                for service in services:
                    score, gate, reasons = fit_against(service, account)
                    row = existing.get((account.id, service.id))
                    if row is None:
                        ts.add(AccountServiceFit(
                            tenant_id=ts.tenant_id, account_id=account.id,
                            service_id=service.id, icp_fit=score, gate=gate, reasons=reasons,
                            computed_at=now))
                    else:
                        row.icp_fit, row.gate, row.reasons, row.computed_at = (
                            score, gate, reasons, now)
                    written += 1
            await ts.flush()
        return written
    except Exception:
        logger.warning("could not store per-service fit", exc_info=True)
        return 0


async def score_service_everywhere(ts, service, *, batch: int = 500) -> int:
    """Score every active account against one service, a batch at a time: run when a service's
    ICP is saved, so the Accounts list and Account 360 show the ICP that is saved."""
    from nexus.models.account import Account

    total = 0
    offset = 0
    while True:
        stmt = (ts.select(Account, Account.archived_at.is_(None))
                .order_by(Account.created_at, Account.id).limit(batch).offset(offset))
        rows = list((await ts.session.scalars(stmt)).all())
        if not rows:
            return total
        total += await score_services(ts, rows, services=[service])
        offset += batch


async def fits_for(ts, account_ids: list[str]) -> dict[str, dict[str, int]]:
    """``{account_id: {service_id: icp_fit}}`` for the stored fits, in one read."""
    if not account_ids:
        return {}
    rows = await ts.list(AccountServiceFit, AccountServiceFit.account_id.in_(account_ids))
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        out.setdefault(row.account_id, {})[row.service_id] = int(row.icp_fit or 0)
    return out


async def fits_for_account(ts, account) -> list[dict]:
    """Account 360: the account's fit against every service, default first.

    The default's fit is computed here, from the profile, as the Fit column has always shown it
    for an account with no service. Each other service's stored row is used while it is newer than
    both the service and the account; otherwise it is scored again and stored, so the panel never
    shows a fit against an ICP that has since changed."""
    from nexus.core.db import ensure_aware

    profile = await default_profile(ts)
    out: list[dict] = []
    if profile is not None:
        score, gate, reasons = fit_against(profile, account)
        out.append({"service_id": None, "name": display_name(profile), "is_default": True,
                    "icp_fit": score if has_icp(profile) else None, "gate": gate,
                    "reasons": reasons if has_icp(profile) else [],
                    "found_here": account.service_id is None})
    services = await active_services(ts)
    if not services:
        return out
    stored = {row.service_id: row for row in await ts.list(
        AccountServiceFit, AccountServiceFit.account_id == account.id)}
    stale = []
    for service in services:
        row = stored.get(service.id)
        if row is None or any(
            when is not None and ensure_aware(when) > ensure_aware(row.computed_at)
            for when in (service.updated_at, account.updated_at)
        ):
            stale.append(service)
    if stale:
        await score_services(ts, [account], services=stale)
        stored = {row.service_id: row for row in await ts.list(
            AccountServiceFit, AccountServiceFit.account_id == account.id)}
    for service in services:
        row = stored.get(service.id)
        configured = has_icp(service)
        out.append({"service_id": service.id, "name": service.name, "is_default": False,
                    "icp_fit": int(row.icp_fit) if (row is not None and configured) else None,
                    "gate": row.gate if row is not None else None,
                    "reasons": list(row.reasons or []) if (row is not None and configured) else [],
                    "found_here": account.service_id == service.id})
    return out
