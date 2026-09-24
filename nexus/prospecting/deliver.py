"""Turning chain candidates into accounts: the step shared by "populate N now" and the daily sweep.

* **LinkedIn and database candidates are ICP matches by construction** (LinkedIn filtered them on
  the ICP's industry codes, countries and size bands), so they are delivered without the fit
  threshold. They are still scored, so the Accounts list shows a Fit badge at once.
* **Web candidates are not**: a web search returns whatever ranks for the words. They are enriched
  (unbilled, as the daily sweep always did), re-checked against the ICP's size and country with the
  enriched values, and must clear ``min_fit``.
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


async def deliver(
    ts, profile, candidates: list[Candidate], *, limit: int, source: str, min_fit: int,
    discarded: Counter, owner_user_id: str | None = None,
) -> tuple[list[str], int]:
    """Create up to ``limit`` accounts. Returns ``(account_ids, screened)``."""
    from nexus.accounts.dedupe import find_existing_account
    from nexus.core.config import get_settings
    from nexus.discovery.auto import _enrich_candidates, _within_geo, _within_size_band
    from nexus.models.account import Account
    from nexus.models.intelligence import AccountScore
    from nexus.relevance import get_relevance_engine

    icp = profile.icp or {}
    relevance = get_relevance_engine()
    settings = get_settings()

    built: list[tuple[Candidate, Account]] = []
    for cand in candidates:
        built.append((cand, Account(
            tenant_id=ts.tenant_id, name=(cand.name or cand.domain).strip(), domain=cand.domain,
            # Only what a source reported. Never the ICP's own values: a stamped industry is
            # permanent (enrichment fills blanks only) and the fit score would then credit the ICP
            # for matching itself.
            industry=cand.industry, country=cand.country, employee_count=cand.employee_count,
            source=source, owner_user_id=owner_user_id, company_id=cand.company_id,
        )))

    web = [account for cand, account in built if cand.source == "web"]
    enrich = settings.icp_discovery_enrich_candidates
    if enrich is None:
        enrich = settings.account_enrich_enabled
    if enrich and web:
        await _enrich_candidates(ts, web[: settings.icp_discovery_enrich_max],
                                 concurrency=settings.icp_discovery_enrich_concurrency)

    # The persist loop: the size and geography gates run here, beside the fit threshold, on the
    # enriched values.
    account_ids: list[str] = []
    screened = 0
    for cand, account in built:
        if len(account_ids) >= limit:
            break
        if cand.source == "web" and not (
            _within_size_band(account.employee_count, icp) and _within_geo(account.country, icp)
        ):
            discarded["outside_icp"] += 1
            continue
        fit = relevance.score_icp_fit(profile, account)
        screened += 1
        if cand.source == "web" and fit.score < min_fit:
            discarded["low_fit"] += 1
            continue
        if await find_existing_account(ts, domain=account.domain) is not None:
            discarded["already_held"] += 1
            continue
        ts.add(account)
        await ts.flush()
        ts.add(AccountScore(tenant_id=ts.tenant_id, account_id=account.id,
                            composite=round(fit.score)))
        await ts.flush()
        account_ids.append(account.id)
    return account_ids, screened
