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

    NOT billed. These are candidates the sweep enriches in order to RANK them; most fail the ICP
    gate and are discarded, so charging `enrich.account` for the batch bills the customer for work
    they never receive. Measured on a 200-credit free plan: 40 candidates at 3 credits took 120
    credits — 60% of the monthly balance — and delivered 20 accounts.

    `discovery.account_added` is the line that covers this, priced at 5 credits with the COGS note
    "exa pool + enrich amortized" — the cost of the candidates that did not make it, spread over
    the ones that did. It is charged in ``auto_discover_for_tenant`` once the survivors are known."""
    from nexus.enrichment.account import get_account_enricher

    await get_account_enricher().enrich_batch(
        ts, accounts, concurrency=concurrency, meter=False
    )


async def _entitled_to_discover(ts: TenantSession) -> bool:
    """Does this tenant's plan include ICP discovery?

    Resolves the entitlement WITHOUT metering — nothing has been delivered yet, so there is
    nothing to charge for. Only a hard `disabled` stops the sweep; every other outcome runs it,
    matching the engine's own bias that anything unresolvable means allow.

    Failing open on an error is deliberate and matches the rest of the billing seam: an
    entitlement lookup that breaks must not silently switch off a customer's daily account feed.
    """
    try:
        from nexus.billing.entitlements import resolve_entitlement

        ent = await resolve_entitlement(ts, "discovery.account_added")
        return ent.mode != "disabled"
    except Exception:
        logger.warning("discovery entitlement check failed; running the sweep", exc_info=True)
        return True


async def _meter_discovered(ts: TenantSession, added: int) -> None:
    """Charge `discovery.account_added` for the accounts this sweep actually delivered.

    After the sweep, not before: how many candidates clear the ICP gate is not knowable until they
    have been enriched and scored. That is the same shape as the bulk verifier in
    `routers/contacts.py` — enforcement applies to the NEXT run rather than guessing at this one.

    A sweep that added nothing is not billed; it sold nothing.

    Never raises. This runs in the automation heartbeat, the accounts are already persisted, and a
    billing failure must not take down a background job or roll back work the customer can see.
    """
    if added <= 0:
        return
    try:
        from nexus.billing.meter import metered

        async with metered(
            ts, "discovery.account_added", quantity=added, source="worker",
            attrs={"sweep": True},
        ):
            pass
    except Exception:
        logger.warning("discovery billing failed for %s accounts", added, exc_info=True)


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


# Country names an enricher actually writes, folded to one form. Not a geography database — just
# enough that "USA" and "United States" are not treated as different places, because the gate below
# EXCLUDES on a mismatch and getting this wrong drops a good US company from a US-only ICP. That is
# the direction a hard gate least can afford to fail in.
_COUNTRY_ALIASES: dict[str, str] = {
    "usa": "united states", "us": "united states",
    "united states of america": "united states",
    "america": "united states", "uk": "united kingdom",
    "great britain": "united kingdom", "britain": "united kingdom",
    "england": "united kingdom", "scotland": "united kingdom", "wales": "united kingdom",
    "uae": "united arab emirates", "republic of india": "india", "bharat": "india",
    "republic of korea": "south korea", "korea": "south korea",
    "russian federation": "russia", "czechia": "czech republic",
    "holland": "netherlands", "deutschland": "germany",
}


def _norm_country(value: str) -> str:
    # Periods removed entirely rather than stripped from the end: "U.S." and "U.S.A." are written
    # both ways and a trailing-only strip leaves "u.s", which matches nothing.
    v = " ".join((value or "").replace(".", "").strip().lower().split())
    return _COUNTRY_ALIASES.get(v, v)


def _within_geo(country: str | None, icp: dict) -> bool:
    """Hard geography gate: a known country outside the stated set is a definitive non-match.

    Identical shape to :func:`_within_size_band`, deliberately — the argument for headcount is the
    argument for geography, and two gates with different semantics would be two rules to hold in
    your head.

    Scoring alone was not enough. Geo carries weight 0.15 against industry 0.35, size 0.30 and tech
    0.20, so a UK company against a `["United States"]` ICP scores **75** at default weights and
    reads as a decent fit. Measured on the live engine with a real account.

    Unknown country is KEPT, not excluded. Search rarely returns one, and discarding a candidate
    for a field we have not fetched would throw away good companies on missing data — the same call
    the size gate makes, and the same one `score_icp_fit` makes when it treats a NULL as neutral.
    """
    wanted = {_norm_country(c) for c in (icp.get("countries") or []) if str(c).strip()}
    if not wanted:
        return True                       # no stated geography: everything passes, as before
    got = _norm_country(country or "")
    if not got:
        return True                       # unknown: ranked, not excluded
    return got in wanted


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
    # Discovery's gates run on incomplete data — search rarely returns headcount OR country, and
    # both are (correctly) admitted when unknown. Once the refresh crawl fills them in, a candidate
    # can prove out-of-band or out-of-geo, and without this it lingers in the rep's list forever.
    if _within_size_band(account.employee_count, profile.icp) and _within_geo(
        account.country, profile.icp
    ):
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

    if not await _entitled_to_discover(ts):
        # The plan does not include discovery. Checked BEFORE the search, not after: a sweep costs
        # real search and enrichment spend, and running it for a tenant we cannot invoice means
        # paying to deliver a feature they did not buy.
        #
        # This was invisible until the charge moved to `discovery.account_added`. While the sweep
        # billed `enrich.account` — which `free` DOES include — the work was paid for through the
        # wrong door and the entitlement was never consulted. Measured live: a `free` workspace
        # ran a sweep, took 5 accounts, and was charged nothing.
        logger.info("icp discovery skipped for %s: plan does not include it", ts.tenant_id)
        return {"discovered": 0, "screened": 0, "account_ids": [], "skipped": "not_entitled"}

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
                # Left BLANK when the search backend did not report one, never defaulted to
                # the ICP's own first industry/country. `apply()` fills blanks only (so a
                # tenant's CRM data is never overwritten), which means a stamped value is
                # permanent — and `score_icp_fit` would then award the industry and geo weights
                # for values the ICP supplied itself. Measured: a steel supplier and a
                # steelmaker were both stored as "Software & SaaS" and scored 80/100 against a
                # SaaS ICP, with "industry 'Software & SaaS' is in ICP" given as the reason.
                industry=cand.industry,
                country=cand.country,
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
        # A country the ICP excludes is a definitive non-match, not a low score. Without this a UK
        # company against a USA-only ICP scored 75 and was persisted as a discovery result.
        if not _within_geo(account.country, profile.icp):
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

    await _meter_discovered(ts, len(account_ids))
    return {"discovered": len(account_ids), "screened": screened, "account_ids": account_ids}
