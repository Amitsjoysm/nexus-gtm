"""Turning chain candidates into accounts: the step shared by "populate N now" and the daily sweep.

* **LinkedIn and database candidates are ICP matches by construction** (LinkedIn filtered them on
  the ICP's industry codes, countries and size bands), so they are delivered without the fit
  threshold. They are still scored, so the Accounts list shows a Fit badge at once.
* **Web candidates are not**: a web search returns whatever ranks for the words. They are enriched
  (unbilled, as the daily sweep always did), re-checked against the ICP's size and country with the
  enriched values, and must clear ``min_fit``.
* **Shared candidates are not either** (2026-10-02): their codes were read off the row's own words.
  The chain only offered the ones ``fit_screen`` passed, and they face the same gates and threshold
  here, without the enrichment, since the shared row already carries what it knows.
* **Parallel candidates were checked** (industry, identity, and the size and country it could
  answer), so no fit threshold; a size or country the check could not answer is enriched and
  screened as a web find's is (2026-10-29).
* **Mandatory ICP fields bind every source** (`relevance.gate`, 2026-10-26), LinkedIn included:
  a row with no data on one is enriched first and, still without it, not delivered.
* **``require_buyer`` makes discovery contact-first** (D2): a company that passed everything else is
  added only once the shared pool or InfoJoy has someone in the ICP's buyer titles there
  (`infojoy_contacts.buyers_at`), checked a batch at a time until ``limit`` are confirmed.
* **The workspace check runs again at insert** (``find_existing_account``), the backstop for account
  domains stored before domains were normalised, which the chain's SQL anti-join compares exactly.

Nothing here charges credits; the caller charges for ``len(account_ids)`` in the same transaction,
so accounts and their charge commit or roll back together.
"""
from __future__ import annotations

import logging
from collections import Counter

from nexus.prospecting.companies import Candidate

logger = logging.getLogger("nexus.prospecting.deliver")

#: Candidates whose ICP match is a claim to check rather than a filter LinkedIn applied.
SCREENED_SOURCES = frozenset({"web", "shared"})


def _unchecked(cand: Candidate) -> bool:
    """A Parallel candidate whose check could not answer its size or country (2026-10-29): those
    are looked up and screened like a web find's. Its industry was checked, so no fit threshold."""
    return cand.source == "parallel" and bool((cand.evidence or {}).get("unchecked"))


def _account_for(ts_tenant_id, cand: Candidate, *, source: str, owner_user_id=None):
    from nexus.integrations.provenance import record, record_present
    from nexus.models.account import Account

    account = Account(
        tenant_id=ts_tenant_id, name=(cand.name or cand.domain).strip(), domain=cand.domain,
        # Only what a source reported. Never the ICP's own values: a stamped industry is
        # permanent (enrichment fills blanks only) and the fit score would then credit the ICP
        # for matching itself.
        industry=cand.industry, country=cand.country, employee_count=cand.employee_count,
        source=source, owner_user_id=owner_user_id, company_id=cand.company_id,
    )
    # Where each value came from, so an SDR can check it (2026-10-26).
    if cand.source in ("linkedin", "database"):
        record_present(account, "linkedin", ref=cand.linkedin_url or None)
    elif cand.source == "shared":
        record_present(account, "shared_record",
                       detail="found by another workspace; labels read off its own text")
    elif cand.source == "parallel":
        # Checked with cited evidence (2026-10-29): the industry and country cite the page that
        # showed them, and the employee band answers a size question with no number invented.
        evidence = cand.evidence or {}
        record(account, ["name", "domain"], "parallel_search")
        record(account, [f for f in ("industry", "country") if getattr(account, f, None)],
               "parallel", ref=evidence.get("ref") or None, detail=evidence.get("detail"))
        if account.employee_count is not None:
            record(account, ["employee_count"], "parallel",
                   detail="headcount from the company record the search returned")
        band = evidence.get("band")
        if band:
            account.custom_fields = {**(account.custom_fields or {}), "employee_band": {
                "min": band[0], "max": band[1], "source": "parallel",
                "ref": evidence.get("ref") or None}}
    else:
        # A web hit says only where it was: what the company does is enrichment's to read.
        record(account, ["name", "domain"], "web_search", ref=cand.url or None)
    return account


def fit_screen(profile, min_fit: int):
    """``find_icp_companies``'s ``screen``: would this candidate clear ``min_fit`` here?

    The same deterministic score `deliver` applies, on the same fields, so a candidate the chain
    lets through is not then discarded for a reason the chain could have seen."""
    from nexus.relevance import get_relevance_engine

    relevance = get_relevance_engine()

    def passes(cand: Candidate) -> bool:
        account = _account_for(None, cand, source="screen")
        return relevance.score_icp_fit(profile, account).score >= min_fit

    return passes


#: Sources whose website was never checked for landing on another company. LinkedIn rows are
#: vetted when they are taken, Parallel rows before their check.
_UNVETTED_SOURCES = frozenset({"web", "shared"})


async def _drop_moved_sites(built, discarded: Counter):
    """Web finds and shared rows whose homepage now lands on another company's domain
    (`vetting.lands_elsewhere`, 2026-10-29: cabotog.com lands on coterra.com) are acquired or gone,
    and are not delivered. One homepage read each, cached a day; a site that does not answer stays."""
    import asyncio

    from nexus.prospecting.vetting import lands_elsewhere

    todo = [(c, a) for c, a in built if c.source in _UNVETTED_SOURCES and c.domain]
    if not todo:
        return built
    gate = asyncio.Semaphore(6)

    async def moved(cand, account) -> bool:
        async with gate:
            return await lands_elsewhere(cand.domain, cand.name or account.name or "")

    verdicts = await asyncio.gather(*(moved(c, a) for c, a in todo))
    gone = {c.domain for (c, _), elsewhere in zip(todo, verdicts) if elsewhere}
    if gone:
        discarded["lands_elsewhere"] += len(gone)
    return [(c, a) for c, a in built if c.domain not in gone]


async def _industry_from_evidence(built, icp: dict, settings) -> set[str]:
    """Apply `companies.industry_evidence` per ``industry_evidence_mode``. Returns the domains whose
    own activity is not one of the ICP's industries (``on`` only), which are not delivered.

    ``on``: a verdict becomes the account's industry, with the page and the quote as its source;
    what the company called itself is kept as ``custom_fields.industry_self_label``. A supplier
    (its self-label is in the ICP, its activity is not) is delivered only when the ICP includes
    suppliers. No verdict keeps the source's label: an unreadable site is no evidence either way.
    ``shadow``: the verdict is recorded on the account for measuring; nothing else changes."""
    mode = getattr(settings, "industry_evidence_mode", "off")
    if mode not in ("shadow", "on") or not built:
        return set()
    from nexus.companies import industry_evidence as ev
    from nexus.integrations.provenance import record
    from nexus.relevance.engine import _same_linkedin_industry

    verdicts = await ev.classify_many([(c.domain, c.name) for c, _ in built])
    codes = icp.get("linkedin_industry_ids") or []
    wrong: set[str] = set()
    for cand, account in built:
        verdict = verdicts.get(cand.domain)
        if verdict is None:
            continue
        custom = dict(account.custom_fields or {})
        if mode == "shadow":
            custom["industry_evidence"] = {"code": verdict.code, "label": verdict.label,
                                           "url": verdict.url, "quote": verdict.quote}
            account.custom_fields = custom
            continue
        said = account.industry
        account.industry = verdict.label
        record(account, "industry", "website", ref=verdict.url, detail=f'"{verdict.quote}"')
        if said and said != verdict.label:
            custom["industry_self_label"] = said
        account.custom_fields = custom
        if codes and not ev.matches_icp(verdict, icp):
            supplier = bool(icp.get("include_suppliers")) and _same_linkedin_industry(said, codes)
            if not supplier:
                wrong.add(cand.domain)
    return wrong


async def deliver(
    ts, profile, candidates: list[Candidate], *, limit: int, source: str, min_fit: int,
    discarded: Counter, owner_user_id: str | None = None, industry_client=None,
) -> tuple[list[str], int]:
    """Create up to ``limit`` accounts. Returns ``(account_ids, screened)``. ``industry_client``
    replaces the Parallel client the industry check uses (tests)."""
    from nexus.accounts.dedupe import find_existing_account
    from nexus.core.config import get_settings
    from nexus.discovery.auto import _enrich_candidates, _within_geo, _within_size_band
    from nexus.models.account import Account
    from nexus.models.intelligence import AccountScore
    from nexus.relevance import get_relevance_engine
    from nexus.relevance.gate import icp_gate, mandatory_fields

    icp = profile.icp or {}
    relevance = get_relevance_engine()
    settings = get_settings()

    built: list[tuple[Candidate, Account]] = [
        (cand, _account_for(ts.tenant_id, cand, source=source, owner_user_id=owner_user_id))
        for cand in candidates
    ]
    built = await _drop_moved_sites(built, discarded)

    # What each company DOES, from its own site (2026-10-26). Before web enrichment, so a quoted
    # verdict is never overwritten by enrichment's looser guess (it fills blanks only).
    wrong_industry = await _industry_from_evidence(built, icp, settings)
    # Every company Parallel did not find itself: LinkedIn first, then one cited check of which
    # ICP industries it operates in (2026-10-29). Inert with no Parallel key.
    from nexus.prospecting.industry_check import check_industries

    checked = await check_industries(built, icp, client=industry_client)
    wrong_industry |= checked.wrong
    if checked.decisions:
        from nexus.prospecting.parallel_companies import record_costs

        await record_costs(ts, searches=0, decisions=checked.decisions)

    # Web rows are always enriched. Any other row is enriched only when a MANDATORY field has no
    # data on it, because no data is no match (D3): a LinkedIn company with no tech stack would
    # otherwise be dropped for something one lookup could answer.
    to_enrich = [account for cand, account in built if cand.source == "web" or _unchecked(cand)]
    if mandatory_fields(icp):
        to_enrich += [account for cand, account in built
                      if cand.source != "web" and not _unchecked(cand)
                      and icp_gate(icp, account).unknown]
    enrich = settings.icp_discovery_enrich_candidates
    if enrich is None:
        enrich = settings.account_enrich_enabled
    if enrich and to_enrich:
        await _enrich_candidates(ts, to_enrich[: settings.icp_discovery_enrich_max],
                                 concurrency=settings.icp_discovery_enrich_concurrency)

    # Contact-first (D2, 2026-10-26): with ``require_buyer`` a company is added only once someone in
    # the ICP's buyer titles is found there. Every other gate runs first, so no buyer check is
    # bought for a company that would have been discarded anyway.
    require_buyer = bool(icp.get("require_buyer")) and bool(icp.get("buyer_titles"))

    account_ids: list[str] = []
    eligible: list[tuple[Account, object]] = []

    async def insert(account, fit) -> None:
        ts.add(account)
        await ts.flush()
        ts.add(AccountScore(tenant_id=ts.tenant_id, account_id=account.id,
                            composite=round(fit.score)))
        await ts.flush()
        account_ids.append(account.id)

    # The persist loop: the size and geography gates run here, beside the fit threshold, on the
    # enriched values.
    screened = 0
    for cand, account in built:
        if len(account_ids) >= limit:
            break
        if cand.domain in wrong_industry:
            discarded["wrong_industry"] += 1
            continue
        # Mandatory fields hold for EVERY source: LinkedIn's filter is on its own labels, and
        # those are what the SDRs found wrong.
        if not icp_gate(icp, account).passed:
            discarded["fails_mandatory"] += 1
            continue
        if (cand.source in SCREENED_SOURCES or _unchecked(cand)) and not (
            _within_size_band(account.employee_count, icp) and _within_geo(account.country, icp)
        ):
            discarded["outside_icp"] += 1
            continue
        fit = relevance.score_icp_fit(profile, account)
        screened += 1
        if cand.source in SCREENED_SOURCES and fit.score < min_fit:
            discarded["low_fit"] += 1
            continue
        if await find_existing_account(ts, domain=account.domain) is not None:
            discarded["already_held"] += 1
            continue
        if require_buyer:
            eligible.append((account, fit))
            continue
        await insert(account, fit)

    if eligible:
        from nexus.companies.resolution import normalise_domain
        from nexus.prospecting.infojoy_contacts import buyers_at
        from nexus.prospector.fetch import record_browse_cost

        bought = 0
        rest = eligible
        while rest and len(account_ids) < limit:
            want = limit - len(account_ids)
            batch, rest = rest[:want], rest[want:]
            found, rows = await buyers_at([(a.domain, a.name) for a, _ in batch], icp)
            bought += rows
            for account, fit in batch:
                if found.get(normalise_domain(account.domain)):
                    await insert(account, fit)
                else:
                    # Not added, and not lost: a company read from LinkedIn stays in the shared
                    # store and is offered again on a later day, when a buyer may be on record.
                    discarded["no_buyer"] += 1
        await record_browse_cost(ts, rows=bought, user_id=None, view="discovery_buyer_check")
    return account_ids, screened
