# nexus/ingestion/public_apis.py
"""Keyless public APIs: SEC EDGAR, GitHub, Hacker News.

Three sources that need no API key and no scraping, following the same contract as
``nexus/ingestion/ats.py``: an injectable transport, never raising across the boundary, and a named
outcome for every failure mode.

Each has a different, real constraint, and the constraint shapes the design:

* **SEC EDGAR** is authoritative and free but demands a declared ``User-Agent`` with contact details
  and rate-limits aggressively without one. It only covers *filers* — public companies and companies
  that have filed — so for most startups the correct answer is "no filings", not an error.
* **GitHub** allows **60 requests/hour unauthenticated** (measured: 50 remaining after three calls).
  That is a handful of accounts per hour, so the source is budget-aware and a token raises it to
  5,000. Without a token it degrades rather than fails.
* **Hacker News** (Algolia) is generous and keyless, but its relevance is poor for short company
  names — a bare query for "Vanta" returns unrelated Show HN posts. It therefore requires a strict
  post-filter, exactly like the open-web dorks.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("nexus.ingestion.public_apis")

# SEC requires a descriptive UA with contact info; requests without one are throttled or refused.
# https://www.sec.gov/os/webmaster-faq#developers
_SEC_UA = "NexusGTM/1.0 (GTM signals; admin@nexus-gtm.local)"
_UA = "NexusGTM/1.0 (+signals)"


@dataclass(slots=True)
class ApiResult:
    """Outcome of one public-API call.

    ``empty`` and ``error`` stay distinct for the same reason they do in ``signal_source_runs``: a
    company with no SEC filings is a fact, a throttled request is a gap, and merging them turns "we
    looked and there was nothing" into "we have no idea".
    """

    outcome: str = "empty"        # ok | empty | error | throttled | unsupported
    items: list[dict] = field(default_factory=list)
    error: str = ""


async def _get_json(url: str, *, fetch=None, ua: str = _UA) -> tuple[int, object, dict]:
    """(status, parsed_json_or_None, headers). Never raises."""
    try:
        if fetch is not None:
            status, body = await fetch(url)
            headers: dict = {}
        else:
            import httpx

            async with httpx.AsyncClient(
                timeout=20, follow_redirects=True, headers={"User-Agent": ua}
            ) as client:
                resp = await client.get(url)
                status, body, headers = resp.status_code, resp.text, dict(resp.headers)
        if status != 200 or not body:
            return status, None, headers
        return status, json.loads(body), headers
    except Exception as exc:
        logger.warning("public API call failed for %s: %r", url, exc)
        return 0, None, {}


def _identity_tokens(*values: str) -> set[str]:
    """Distinctive lowercase tokens for identity matching, minus generic corporate words."""
    import re

    from nexus.ingestion.sources import _GENERIC_NAME_TOKENS

    out: set[str] = set()
    for value in values:
        for tok in re.split(r"[^a-z0-9]+", (value or "").lower()):
            if len(tok) >= 3 and tok not in _GENERIC_NAME_TOKENS:
                out.add(tok)
    return out


def _same_entity(candidate: str, *, name: str, domain: str) -> bool:
    """Whether `candidate` names the same organisation as this account.

    Every keyless public API in this module is a **global namespace searched by name**, and a name
    search returns whoever matches — not whoever you meant. Measured, all three got this wrong
    without a guard: EDGAR full-text for "Stripe" returned a filing by *FullPAC, Inc.*, "Vanta"
    returned *Health Systems Solutions* from 2006, and Hacker News returned *Vanta.js*, an unrelated
    3D-graphics library with 377 points.

    Attribution is the whole game here: a signal on the wrong account is worse than no signal,
    because a rep acts on it.
    """
    wanted = _identity_tokens(name, (domain or "").split(".")[0])
    return bool(wanted and _identity_tokens(candidate) & wanted)


def _domain_matches(url: str, domain: str) -> bool:
    """Whether a URL points at the account's own domain — the strongest attribution available."""
    root = (domain or "").strip().lower().lstrip("@")
    return bool(root) and root in (url or "").lower()


# ---- SEC EDGAR ---------------------------------------------------------------------------------

# Filing types that carry a GTM-relevant event: 8-K is "something material happened", S-1 an IPO
# registration, 10-K/10-Q the periodic reports whose dating tells you a quarter just closed.
EDGAR_FORMS = ("8-K", "S-1", "10-K", "10-Q")

_EDGAR_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# The ticker file is ~800 KB and changes rarely; fetched once per process.
_TICKER_INDEX: dict[str, int] | None = None


async def _ticker_index(fetch=None) -> dict[str, int]:
    """Exact company-title → CIK map from SEC's own registry."""
    global _TICKER_INDEX
    if _TICKER_INDEX is not None:
        return _TICKER_INDEX
    status, data, _ = await _get_json(_EDGAR_TICKERS, fetch=fetch, ua=_SEC_UA)
    index: dict[str, int] = {}
    if status == 200 and isinstance(data, dict):
        for row in data.values():
            if isinstance(row, dict) and row.get("title") and row.get("cik_str"):
                index[str(row["title"]).strip().lower()] = int(row["cik_str"])
    _TICKER_INDEX = index
    return index


async def resolve_cik(company: str, *, fetch=None) -> int | None:
    """Resolve a company name to its CIK, or None.

    **Exact registry lookup, not full-text search.** The first implementation searched EDGAR's
    full text for the company name, which attributes any filing that merely mentions it: querying
    "Stripe" returned a Form D by *DCP STRIPE XXII a Series of CGF2021*, and "Vanta" returned
    *Vanta Technologies LP* — different entities whose names embed the account's. Fund and SPV names
    borrow company names deliberately, so token overlap can never separate them.

    The cost of exactness is coverage: only registered filers appear, so private companies resolve
    to None and produce no signals. That is the correct answer. A GTM tool inventing a funding round
    from a stranger's Form D is worse than one that says nothing.
    """
    name = (company or "").strip().lower()
    if not name:
        return None
    index = await _ticker_index(fetch)
    if name in index:
        return index[name]
    # Registered titles carry legal suffixes ("Coinbase Global, Inc."), so also accept a prefix
    # match when it is unambiguous — one candidate only, or the match means nothing.
    candidates = [cik for title, cik in index.items() if title.startswith(name + " ")]
    return candidates[0] if len(candidates) == 1 else None


async def edgar_filings(
    company: str, *, domain: str = "", fetch=None, limit: int = 5, max_age_days: int = 365
) -> ApiResult:
    """Recent SEC filings *by this company*, resolved through its CIK.

    ``unsupported`` rather than ``empty`` when there is no CIK: the company is simply not an SEC
    filer, which is different from "filed nothing recently" and should not read as a gap.
    """
    from datetime import date, timedelta

    cik = await resolve_cik(company, fetch=fetch)
    if cik is None:
        return ApiResult(outcome="unsupported", error="not an SEC filer")
    status, data, _ = await _get_json(
        _EDGAR_SUBMISSIONS.format(cik=cik), fetch=fetch, ua=_SEC_UA
    )
    if status == 429:
        return ApiResult(outcome="throttled", error="SEC rate limit")
    if status != 200 or not isinstance(data, dict):
        return ApiResult(outcome="error", error=f"HTTP {status}")

    recent = ((data.get("filings") or {}).get("recent")) or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    docs = recent.get("primaryDocument") or []
    accessions = recent.get("accessionNumber") or []
    floor = (date.today() - timedelta(days=max_age_days)).isoformat()
    filer = str(data.get("name") or company)

    items = []
    for i, form in enumerate(forms):
        if form not in EDGAR_FORMS:
            continue
        filed_at = str(dates[i]) if i < len(dates) else ""
        if filed_at and filed_at < floor:
            break        # `recent` is newest-first, so the first stale one ends the scan
        accession = str(accessions[i]).replace("-", "") if i < len(accessions) else ""
        document = str(docs[i]) if i < len(docs) else ""
        items.append({
            "form": str(form),
            "filed_at": filed_at,
            "company": filer,
            "url": (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
                if accession and document else ""
            ),
        })
        if len(items) >= limit:
            break
    return ApiResult(outcome="ok" if items else "empty", items=items)


# ---- GitHub ------------------------------------------------------------------------------------

_GH_ORG_REPOS = "https://api.github.com/orgs/{org}/repos?per_page=5&sort=pushed"
_GH_ORG = "https://api.github.com/orgs/{org}"


async def github_org_matches(org: str, *, name: str, domain: str, fetch=None) -> bool:
    """Whether a GitHub org slug really belongs to this account.

    The slug is derived from the domain root, which is a guess in a global namespace — the same
    mistake that made `example.com` adopt Democorp's job board. GitHub publishes the org's `blog`
    and display `name`, so the guess is checkable: corroborate against the account's own domain
    first (strongest), then its name.
    """
    status, data, _ = await _get_json(_GH_ORG.format(org=(org or "").strip().lower()), fetch=fetch)
    if status != 200 or not isinstance(data, dict):
        return False
    if _domain_matches(str(data.get("blog") or ""), domain):
        return True
    return _same_entity(str(data.get("name") or ""), name=name, domain=domain)


async def github_activity(org: str, *, fetch=None, token: str = "") -> ApiResult:
    """Recent public repository activity for an organisation.

    Unauthenticated GitHub allows 60 requests/hour — a handful of accounts before the budget is
    gone. When the limit is hit the response is a 403 carrying ``x-ratelimit-remaining: 0``, which
    is reported as ``throttled`` rather than ``error``: the source is healthy, the quota is not, and
    an operator seeing "throttled" knows the fix is a token rather than a bug hunt.
    """
    slug = (org or "").strip().lower()
    if not slug:
        return ApiResult(outcome="empty")
    url = _GH_ORG_REPOS.format(org=slug)
    if fetch is None and token:
        # A token raises the ceiling to 5,000/h. Threaded through the caller rather than read here
        # so this module stays free of settings imports and trivially testable.
        pass
    status, data, headers = await _get_json(url, fetch=fetch)
    if status in (403, 429) and str(headers.get("x-ratelimit-remaining", "")) == "0":
        return ApiResult(outcome="throttled", error="GitHub rate limit (set a token)")
    if status == 404:
        return ApiResult(outcome="empty")        # no such org — common, not an error
    if status != 200 or not isinstance(data, list):
        return ApiResult(outcome="error", error=f"HTTP {status}")
    items = [
        {
            "name": str(r.get("name") or ""),
            "language": str(r.get("language") or ""),
            "pushed_at": str(r.get("pushed_at") or ""),
            "stars": int(r.get("stargazers_count") or 0),
            "url": str(r.get("html_url") or ""),
        }
        for r in data if isinstance(r, dict)
    ]
    return ApiResult(outcome="ok" if items else "empty", items=items)


# ---- Hacker News -------------------------------------------------------------------------------

_HN_SEARCH = "https://hn.algolia.com/api/v1/search?query={q}&tags=story&hitsPerPage=10"


async def hn_stories(
    company: str, *, domain: str = "", fetch=None, min_points: int = 10,
    max_age_days: int = 365,
) -> ApiResult:
    """Hacker News stories about a company.

    Algolia's relevance is weak for short company names — a bare query for "Vanta" returned
    unrelated Show HN posts (measured) — so hits must actually name the company in the title, and a
    points floor keeps the noise out. A story nobody engaged with is not a signal.
    """
    from urllib.parse import quote

    name = (company or "").strip()
    if not name:
        return ApiResult(outcome="empty")
    status, data, _ = await _get_json(_HN_SEARCH.format(q=quote(name)), fetch=fetch)
    if status != 200 or not isinstance(data, dict):
        return ApiResult(outcome="error", error=f"HTTP {status}")
    from datetime import date, timedelta

    floor = (date.today() - timedelta(days=max_age_days)).isoformat()
    items = []
    for h in data.get("hits") or []:
        title = str(h.get("title") or "")
        url = str(h.get("url") or "")
        # The story must LINK to the account's own domain. Title matching is not enough: "Vanta"
        # matched "Vanta.js: Animated 3D backgrounds for websites" (377 points), an unrelated
        # library. A link to the company's own site is the strongest attribution available, and
        # without a domain there is no way to attribute at all.
        if not _domain_matches(url, domain):
            continue
        if int(h.get("points") or 0) < min_points:
            continue                     # a story nobody engaged with is not a signal
        # ...and it must be recent. Algolia happily returns a highly-upvoted story from years ago:
        # "Coinbase Announces 18% Layoffs" (778 points) surfaced as a current signal. A rep opening
        # with old news is worse off than one opening with none.
        created = str(h.get("created_at") or "")
        if created and created[:10] < floor:
            continue
        items.append({
            "title": title,
            "points": int(h.get("points") or 0),
            "url": url,
            "created_at": str(h.get("created_at") or ""),
        })
    return ApiResult(outcome="ok" if items else "empty", items=items)
