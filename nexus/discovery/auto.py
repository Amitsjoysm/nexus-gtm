"""Daily ICP Auto-Discovery driver.

For one tenant, find net-new companies via the company-search waterfall, **crawl their firmographics
from the web**, then add ONLY the ones that **strictly** match the saved ICP: a hard size-band
filter, then an ICP-fit score that must clear ``min_fit``. Sub-threshold candidates are never
persisted, so the SDR's list fills with high-fit accounts and nothing else. Dedup is by domain
across all accounts (incl. archived), so a company is never surfaced twice.

Pipeline: the prospecting chain (shared database → LinkedIn → web search, in
``nexus/prospecting/companies.py``) → for web candidates only, **enrich (our web crawler)** → score →
keep (``nexus/prospecting/deliver.py``).
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
from collections import Counter
from typing import Awaitable, Callable

from sqlalchemy import select

from nexus.core.tenancy import TenantSession
from nexus.integrations.company_search import CompanyCandidate
from nexus.models.account import Account
from nexus.relevance.engine import get_profile

logger = logging.getLogger("nexus.discovery.auto")

Search = Callable[..., Awaitable[list[CompanyCandidate]]]

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

    # Database first, then LinkedIn, then web search (nexus/prospecting/companies.py). The web
    # step is the one this sweep used to be, and keeps its pool: its candidates must still clear
    # the fit threshold after enrichment, so it needs more than it will deliver.
    from collections import Counter

    from nexus.prospecting.companies import find_icp_companies
    from nexus.prospecting.deliver import deliver

    chain = await find_icp_companies(
        ts, profile.icp, target_count, web_search=search, web_pool=pool_limit,
        product_context=getattr(profile, "product_context", "") or "",
    )
    discarded = Counter(chain.discarded)
    account_ids, screened = await deliver(
        ts, profile, chain.candidates, limit=target_count, source="auto_discovery",
        min_fit=min_fit, discarded=discarded,
    )
    await _meter_discovered(ts, len(account_ids))
    return {"discovered": len(account_ids), "screened": screened, "account_ids": account_ids,
            "sources": chain.sources, "discarded": dict(discarded), "notes": chain.notes}


# ---- the daily number (2026-09-30) ----------------------------------------------------------------
# A workspace sets how many new ICP accounts it wants a day. One pass rarely found that many (the
# strict gates discard most web candidates, a source can fail), and nothing topped it up or said
# why. Each pass is now a `ProspectRun(kind="daily")`, so "how many today, and why short" is a read
# of what happened, and the heartbeat can decide whether another pass is worth its cost.

DAILY_KIND = "daily"

#: A source note that means "this could go better on a later pass": a failure or a rate limit,
#: as opposed to "LinkedIn had nothing new", which a retry an hour later would repeat.
_RETRYABLE_NOTES = {"failed", "rate_limited", "timeout"}

_NOTE_TEXT = {
    ("linkedin", "not_configured"): "LinkedIn search is not set up",
    ("linkedin", "failed"): "LinkedIn search failed",
    ("linkedin", "no_industry_codes"): "your ICP industries match no LinkedIn industry",
    ("linkedin", "nothing_new"): "LinkedIn had no new companies",
    ("web", "failed"): "web search failed",
}

_DISCARD_TEXT = {
    "low_fit": "{n} below your fit threshold",
    "outside_icp": "{n} outside your size or countries",
    "already_held": "{n} you already have",
    "no_website": "{n} without a company website",
    "duplicate": None,
}


def shortfall_reason(notes: dict, discarded: dict) -> str:
    """One sentence a person can act on: what failed, then where the candidates went."""
    parts = [_NOTE_TEXT.get((src, note), f"{src} search: {note}")
             for src, note in sorted((notes or {}).items())]
    for key, n in sorted((discarded or {}).items(), key=lambda kv: -int(kv[1] or 0)):
        text = _DISCARD_TEXT.get(key, f"{{n}} {key.replace('_', ' ')}")
        if text and n:
            parts.append(text.format(n=n))
    return "; ".join(parts)


def pass_is_worth_repeating(run) -> bool:
    """Would another pass later today plausibly find more? Yes after progress or a failing source;
    no when a clean pass found nothing new, because it would find the same nothing again."""
    if run.status == "failed":
        return True
    if (run.delivered or 0) > 0:
        return True
    return any(v in _RETRYABLE_NOTES for v in (run.notes or {}).values())


async def runs_since(ts: TenantSession, since) -> list:
    from nexus.models.prospecting import ProspectRun

    if since is None:
        return []
    stmt = ts.select(ProspectRun, ProspectRun.kind == DAILY_KIND,
                     ProspectRun.created_at >= since).order_by(ProspectRun.created_at)
    return list((await ts.session.scalars(stmt)).all())


async def today_summary(ts: TenantSession, tenant, *, target: int) -> dict | None:
    """What the current interval delivered against the workspace's number. None before any pass."""
    runs = await runs_since(ts, tenant.icp_discovery_last_run_at)
    if not runs:
        return None
    delivered = sum(r.delivered or 0 for r in runs)
    last = runs[-1]
    notes: dict = {}
    discarded: Counter = Counter()
    for r in runs:
        notes.update(r.notes or {})
        discarded.update({k: int(v or 0) for k, v in (r.discarded or {}).items()})
    reason = ""
    if delivered < target:
        reason = ("the last pass failed: " + (last.error or "unknown error")[:200]
                  if last.status == "failed" else shortfall_reason(notes, dict(discarded)))
    return {
        "window_started_at": tenant.icp_discovery_last_run_at,
        "target": target, "delivered": delivered, "attempts": len(runs),
        "last_attempt_at": last.created_at, "short_reason": reason,
    }
