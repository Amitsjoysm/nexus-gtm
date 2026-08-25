"""Daily ICP Auto-Discovery driver.

For one tenant, find net-new companies via the company-search waterfall, **crawl their firmographics
from the web**, then add ONLY the ones that **strictly** match the saved ICP: a hard size-band
filter, then an ICP-fit score that must clear ``min_fit``. Sub-threshold candidates are never
persisted, so the SDR's list fills with high-fit accounts and nothing else. Dedup is by domain
across all accounts (incl. archived), so a company is never surfaced twice.

Pipeline: search (Exa) → build transient candidates → **enrich (our web crawler)** → score → keep.
Enrichment matters because search returns domain/industry/geo but not headcount/tech/revenue, so
without it every candidate scores identically; crawling fills those blanks so the score actually
ranks. It's gated + bounded + best-effort, so offline/CI it's a no-op and a crawl outage can't block
discovery.

The heavy network bits (company search + enrichment) are injectable/gated, so the strict-matching
logic is fully unit-tested offline. Scored accounts land with an ICP-fit ``AccountScore`` so the
Accounts list shows a Fit badge immediately; the regular account-refresh tick later deepens them.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from sqlalchemy import select

from nexus.core.config import get_settings
from nexus.core.tenancy import TenantSession
from nexus.integrations.company_search import CompanyCandidate
from nexus.models.account import Account
from nexus.models.intelligence import AccountScore
from nexus.relevance import get_relevance_engine
from nexus.relevance.engine import get_profile

logger = logging.getLogger("nexus.discovery.auto")

Search = Callable[..., Awaitable[list[CompanyCandidate]]]

# Cap the excludeDomains list sent to the search backend (Exa caps the request size). The local
# domain dedup below is the backstop for anything beyond the cap, so correctness never depends on it.
_EXCLUDE_CAP = 256


async def _enrich_candidates(
    ts: TenantSession, accounts: list[Account], *, concurrency: int
) -> None:
    """Crawl the web to fill each candidate's blank firmographics (industry/headcount/geo/tech) so
    scoring can differentiate them. Concurrent, bounded, best-effort.

    Billing lives in ``enrich_batch``: one ``enrich.account`` charge for the whole batch, taken
    before the concurrency starts, because metering N candidates inside the gather would put N
    coroutines on one AsyncSession. A blocked tenant gets an unenriched candidate set — a worse
    ranking — rather than a discovery run that dies partway."""
    from nexus.enrichment.account import get_account_enricher

    await get_account_enricher().enrich_batch(ts, accounts, concurrency=concurrency)


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


async def rescreen_discovered_account(ts: TenantSession, account: Account) -> bool:
    """Post-enrichment ICP re-screen: archive an auto-discovered account whose now-known
    headcount is definitively outside the ICP size band.

    Discovery's size gate runs on incomplete data — search rarely returns headcount, and
    unknown headcount is (correctly) kept. Once the account-refresh crawl fills
    ``employee_count``, a candidate can prove out-of-band; without this re-screen it would
    linger in the SDR's list forever. Guards:

    - only ``source == "auto_discovery"`` — manual/CRM accounts were chosen by a human;
    - only a KNOWN out-of-band headcount (the same definitive-non-match rule as discovery);
    - never once a rep has engaged (any contacts on the account = it's theirs to keep).

    Archives (``custom_fields.archived``) rather than deletes, so the account stays
    inspectable and can be unarchived. Returns True when the account was archived.
    """
    if account.source != "auto_discovery" or account.employee_count is None:
        return False
    if account.is_archived:
        return False  # already out of the list; nothing to do
    profile = await get_profile(ts)
    if profile is None or not profile.icp:
        return False
    if _within_size_band(account.employee_count, profile.icp):
        return False
    from nexus.models.account import Contact

    engaged = await ts.session.scalar(
        select(Contact.id).where(Contact.account_id == account.id).limit(1)
    )
    if engaged is not None:
        return False
    account.set_archived(True, reason="icp_size_band")  # archived_at column + legacy JSON mirror
    await ts.flush()
    logger.info(
        "icp re-screen archived account %s (%s): headcount %s outside band",
        account.id, account.name, account.employee_count,
    )
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

    # Tell the search backend which companies we already track so it returns NET-NEW ones instead of
    # re-surfacing the same top-N every run (the reason daily discovery dried up after a few days).
    # Column-projected (no ORM hydration) and capped to stay within the backend's request limit.
    tracked = {
        d.lower()
        for d in (
            await ts.session.scalars(
                select(Account.domain).where(
                    Account.tenant_id == ts.tenant_id, Account.domain.isnot(None)
                )
            )
        ).all()
        if d
    }
    exclude = sorted(tracked)[:_EXCLUDE_CAP] if tracked else None

    icp = _profile_to_search_icp(profile)
    try:
        candidates = await search(icp, limit=pool_limit, exclude_domains=exclude)
    except Exception as exc:  # a search outage must not crash the heartbeat
        logger.warning("icp auto-discovery search failed: %r", exc)
        candidates = []

    relevance = get_relevance_engine()
    settings = get_settings()
    fallback_industry = icp["industries"][0] if icp["industries"] else None
    fallback_country = icp["geo"][0] if icp["geo"] else None

    # 1) Build distinct, net-new candidate accounts (transient — not persisted yet). Dedup in-memory
    #    against everything we already track (the `tracked` set) + within-run dups, so a company is
    #    never re-surfaced. Building is cheap, so we build the whole pool; cost is bounded at enrich.
    built: list[Account] = []
    seen: set[str] = set()
    for cand in candidates:
        # Normalised on both sides, or the same company is discovered again every day under a
        # slightly different spelling and the rep's "net-new" list is mostly duplicates.
        from nexus.accounts.dedupe import normalise_on_write

        domain = normalise_on_write(cand.domain) or ""
        if not domain or domain in tracked or domain in seen:
            continue
        seen.add(domain)
        built.append(
            Account(
                tenant_id=ts.tenant_id,
                name=(cand.name or domain).strip(),
                domain=domain,
                industry=cand.industry or fallback_industry,
                country=cand.country or fallback_country,
                employee_count=cand.employee_count,
                source="auto_discovery",
            )
        )

    # 2) Crawl firmographics for the top candidates BEFORE scoring so the ICP-fit score can actually
    #    rank them (search gives industry/geo but not headcount/tech). Gated + bounded + best-effort;
    #    offline/CI it's a no-op, so the strict-match logic below is unchanged.
    enrich = settings.icp_discovery_enrich_candidates
    if enrich is None:
        enrich = settings.account_enrich_enabled
    if enrich and built:
        await _enrich_candidates(
            ts,
            built[: settings.icp_discovery_enrich_max],
            concurrency=settings.icp_discovery_enrich_concurrency,
        )

    # 3) Hard size-band gate (now using crawled headcount) + strict ICP-fit; persist the matches up
    #    to target_count. Sub-threshold candidates are never persisted.
    account_ids: list[str] = []
    screened = 0
    for account in built:
        if len(account_ids) >= target_count:
            break
        if not _within_size_band(account.employee_count, profile.icp):
            continue
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
