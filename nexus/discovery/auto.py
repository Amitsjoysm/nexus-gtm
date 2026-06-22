"""Daily ICP Auto-Discovery driver.

For one tenant, find net-new companies via the company-search waterfall and add ONLY the ones
that **strictly** match the saved ICP: a hard size-band filter, then an ICP-fit score that must
clear ``min_fit``. Sub-threshold candidates are never persisted, so the SDR's list fills with
high-fit accounts and nothing else. Dedup is by domain across all accounts (incl. archived), so a
company is never surfaced twice.

The heavy network bits (company search) are injectable, so the strict-matching logic is fully
unit-tested offline. Scored accounts land with an ICP-fit ``AccountScore`` so the Accounts list
shows a Fit badge immediately; the regular account-refresh tick later deepens them (signals/intent).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from nexus.core.tenancy import TenantSession
from nexus.integrations.company_search import CompanyCandidate
from nexus.models.account import Account
from nexus.models.intelligence import AccountScore
from nexus.relevance import get_relevance_engine
from nexus.relevance.engine import get_profile

logger = logging.getLogger("nexus.discovery.auto")

Search = Callable[..., Awaitable[list[CompanyCandidate]]]


def _profile_to_search_icp(profile) -> dict:
    """Build the conversational ICP dict the company-search waterfall expects from the saved
    (scoring-format) RelevanceProfile."""
    icp = profile.icp or {}
    return {
        "industries": list(icp.get("industries", []) or []),
        "geo": list(icp.get("countries", []) or []),
        "company_size": {"min": icp.get("employee_min"), "max": icp.get("employee_max")},
        "icp_description": getattr(profile, "product_context", "") or "",
    }


def _within_size_band(employee_count: int | None, icp: dict) -> bool:
    """Hard size gate: a known headcount outside the stated band is a definitive non-match.
    Unknown headcount is kept (it's ranked, not excluded). Mirrors the discovery agent."""
    if employee_count is None:
        return True
    lo, hi = icp.get("employee_min"), icp.get("employee_max")
    if lo is not None and employee_count < lo:
        return False
    if hi is not None and employee_count > hi:
        return False
    return True


async def auto_discover_for_tenant(
    ts: TenantSession,
    *,
    target_count: int,
    min_fit: int,
    pool_limit: int,
    search: Search | None = None,
) -> dict:
    """Discover up to ``target_count`` net-new, strictly-ICP-matching accounts for this tenant.
    Returns ``{discovered, screened, account_ids}`` (or ``{skipped: 'no_icp'}`` with no ICP)."""
    profile = await get_profile(ts)
    if profile is None or not profile.icp:
        return {"discovered": 0, "screened": 0, "account_ids": [], "skipped": "no_icp"}

    if search is None:
        from nexus.integrations.registry import build_registry_from_settings

        search = build_registry_from_settings().company_search

    icp = _profile_to_search_icp(profile)
    try:
        candidates = await search(icp, limit=pool_limit)
    except Exception as exc:  # a search outage must not crash the heartbeat
        logger.warning("icp auto-discovery search failed: %r", exc)
        candidates = []

    relevance = get_relevance_engine()
    fallback_industry = icp["industries"][0] if icp["industries"] else None
    fallback_country = icp["geo"][0] if icp["geo"] else None

    account_ids: list[str] = []
    screened = 0
    for cand in candidates:
        if len(account_ids) >= target_count:
            break
        domain = (cand.domain or "").strip().lower()
        if not domain:
            continue
        # Dedup across ALL accounts (incl. archived) so a company is never re-surfaced.
        if await ts.first(Account, Account.domain == domain) is not None:
            continue
        if not _within_size_band(cand.employee_count, profile.icp):
            continue
        account = Account(
            tenant_id=ts.tenant_id,
            name=(cand.name or domain).strip(),
            domain=domain,
            industry=cand.industry or fallback_industry,
            country=cand.country or fallback_country,
            employee_count=cand.employee_count,
            source="auto_discovery",
        )
        fit = relevance.score_icp_fit(profile, account)
        screened += 1
        if fit.score < min_fit:
            continue  # strict ICP gate — sub-threshold candidates are never persisted
        ts.add(account)
        await ts.flush()
        ts.add(
            AccountScore(
                tenant_id=ts.tenant_id, account_id=account.id, composite=round(fit.score)
            )
        )
        await ts.flush()
        account_ids.append(account.id)

    return {"discovered": len(account_ids), "screened": screened, "account_ids": account_ids}
