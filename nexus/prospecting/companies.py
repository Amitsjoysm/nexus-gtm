"""ICP companies, from the cheapest source that has them: the shared database, then LinkedIn, then web
search (decided with the product owner 2026-09-24).

1. **Database.** Every company any workspace's LinkedIn search has read lives in the shared
   ``companies`` table with its industry code, size band and office countries. Those matching the
   ICP that THIS workspace does not already hold are offered first and cost nothing to find. "Does
   not already hold" is an SQL anti-join against the workspace's accounts, so it scales with the
   workspace. The discovery it replaces sent at most 256 held domains to the search as exclusions,
   so a workspace holding 3,500 accounts was shown the same top results every day and nearly all
   were duplicates.
2. **LinkedIn** (``linkedin_company_search``) for the shortfall. Pages are read from a cursor
   shared by every workspace (``prospect_cursors``): each page read is stored in the shared table,
   so the next workspace with the same ICP gets those companies from step 1 rather than paying to
   read the page again. Every company on a page is stored, including the ones this request did not
   need. LinkedIn serves at most 1,000 results for one query, so a bigger result splits by size
   band, then country, then industry, each part with its own cursor. A cursor that reached the end
   is read again after ``REOPEN_AFTER_DAYS``, because new companies appear.
3. **Web search** only when LinkedIn delivered nothing or could not be asked. Its results pass the
   same gates: a real company website, the ICP's size and country, and not already held.

**No domain, no company.** A row without its own website is counted as ``no_website`` and is
neither delivered nor stored: the domain is the identity of an account and of a shared company.

Nothing here creates accounts or charges credits; the caller does both with what is returned, and
reports ``discarded`` so a shortfall has a reason a person can read.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Awaitable, Callable

from sqlalchemy import exists, or_, select, update
from sqlalchemy.exc import IntegrityError

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.prospecting import linkedin as li
from nexus.prospecting.industries import descendants, map_industries

logger = logging.getLogger("nexus.prospecting.companies")

#: Pages one request may buy. ``1 + need/10`` of them, so a request for 20 reads at most three pages
#: (150 companies, $0.60 at $0.004 each) and the ones it does not need wait in the database.
MAX_PAGES_PER_RUN = 10
#: A cursor that reached the end is read again after this long: LinkedIn's results change.
REOPEN_AFTER_DAYS = 30
#: Deepest a query is split past the 1,000-result ceiling: size band, country, industry.
MAX_SPLIT_DEPTH = 3
_CEILING = li.PAGE_SIZE * li.MAX_PAGES

WebSearch = Callable[..., Awaitable[list]]


@dataclass(frozen=True)
class ProspectQuery:
    """One LinkedIn company search, and the filters the database step applies for it."""

    industry_ids: tuple[int, ...] = ()
    #: ISO codes, for the database step.
    countries: tuple[str, ...] = ()
    #: Names, for LinkedIn, which filters on location text.
    locations: tuple[str, ...] = ()
    sizes: tuple[str, ...] = ()
    employee_min: int | None = None
    employee_max: int | None = None

    def key(self) -> str:
        canonical = json.dumps({
            "i": sorted(self.industry_ids), "l": sorted(x.lower() for x in self.locations),
            "s": sorted(self.sizes),
        }, sort_keys=True)
        return hashlib.sha1(canonical.encode("utf-8")).hexdigest()

    def as_json(self) -> dict:
        return {"industry_ids": list(self.industry_ids), "locations": list(self.locations),
                "sizes": list(self.sizes)}

    def split(self) -> list[ProspectQuery]:
        """Narrower queries that together cover this one: by size band, then location, then
        industry. Empty when there is nothing left to split on."""
        if len(self.sizes) != 1:
            bands = self.sizes or tuple(label for label, _, _ in li.SIZE_BANDS)
            return [replace(self, sizes=(band,)) for band in bands]
        if len(self.locations) > 1:
            return [replace(self, locations=(loc,)) for loc in self.locations]
        if len(self.industry_ids) > 1:
            return [replace(self, industry_ids=(i,)) for i in self.industry_ids]
        return []


@dataclass
class Candidate:
    """A company ready to become an account, and where it came from."""

    domain: str
    name: str
    source: str                      # database | linkedin | web
    company_id: str | None = None
    industry: str | None = None
    country: str | None = None
    employee_count: int | None = None
    linkedin_url: str = ""


@dataclass
class ChainResult:
    candidates: list[Candidate] = field(default_factory=list)
    discarded: Counter = field(default_factory=Counter)
    #: Why a source did not contribute: {"linkedin": "not_configured" | "failed" | ...}.
    notes: dict[str, str] = field(default_factory=dict)
    #: Companies read from LinkedIn and stored for later, beyond what this request needed.
    stored: int = 0

    @property
    def sources(self) -> dict[str, int]:
        return dict(Counter(c.source for c in self.candidates))


# ---- countries ---------------------------------------------------------------------------------

def _country_labels() -> dict[str, str]:
    from nexus.contacts.phone import _COUNTRY_NAMES

    labels: dict[str, str] = {}
    for name, code in _COUNTRY_NAMES.items():
        # The first name listed for a code is its plain English name ("united states", not "usa").
        labels.setdefault(code, name.title())
    return labels


def country_label(code: str | None) -> str | None:
    """A readable country name for an ISO code, the form accounts store; the code itself if the
    name is not known."""
    if not code:
        return None
    return _country_labels().get(code.upper(), code.upper())


async def query_for_icp(icp: dict) -> ProspectQuery:
    """The LinkedIn search an ICP describes. Industry codes are read from the ICP when saving it
    stored them, and mapped now otherwise."""
    from nexus.contacts.phone import region_for

    ids = [int(i) for i in (icp.get("linkedin_industry_ids") or []) if str(i).strip().isdigit()]
    if not ids and icp.get("industries"):
        ids = await map_industries(icp.get("industries"))
    names = [str(c).strip() for c in (icp.get("countries") or []) if str(c).strip()]
    codes = [code for code in (region_for(n) for n in names) if code]
    locations = [country_label(region_for(n)) or n for n in names]
    lo, hi = icp.get("employee_min"), icp.get("employee_max")
    return ProspectQuery(
        industry_ids=tuple(dict.fromkeys(ids)),
        countries=tuple(dict.fromkeys(codes)),
        locations=tuple(dict.fromkeys(locations)),
        sizes=tuple(li.size_bands(lo, hi)),
        employee_min=lo, employee_max=hi,
    )


# ---- the shared store --------------------------------------------------------------------------

async def store_companies(companies) -> dict[str, str]:
    """Write LinkedIn companies into the shared store. Returns ``{domain: company_id}``.

    Only rows with a domain are stored. An existing row keeps what it has: a blank is filled, and a
    LinkedIn page is attached only to a row that has none, so a subsidiary's page on its parent's
    domain never replaces the parent's. The domain is the identity; the page is a fact about it.
    """
    from nexus.companies.resolution import company_id_for, resolve_company
    from nexus.models.company import CompanyCountry

    wanted = [c for c in companies if c is not None and c.domain]
    ids: dict[str, str] = {}
    if not wanted:
        return ids
    now = utcnow()
    async with get_platform_sessionmaker()() as s:
        for c in wanted:
            for attempt in range(2):
                try:
                    async with s.begin_nested():
                        row = await resolve_company(
                            s, domain=c.domain, name=c.name, source="linkedin",
                            industry=c.industry_name or None,
                            country=country_label(c.hq_country),
                            employee_count=c.employee_count,
                        )
                        if row is None:
                            break
                        await s.flush()
                        if not row.linkedin_url or row.linkedin_url == c.linkedin_url:
                            row.linkedin_url = c.linkedin_url or row.linkedin_url
                            row.linkedin_id = c.linkedin_id or row.linkedin_id
                            row.linkedin_industry_id = c.industry_id
                            row.employee_range_min = c.range_min
                            row.employee_range_max = c.range_max
                            row.hq_country_code = c.hq_country or row.hq_country_code
                            row.description = c.description or row.description
                            if c.similar:
                                row.similar_linkedin = c.similar
                            row.linkedin_fetched_at = now
                        have = set((await s.scalars(
                            select(CompanyCountry.country_code)
                            .where(CompanyCountry.company_id == row.id))).all())
                        if row.linkedin_url == c.linkedin_url:
                            for code in c.countries:
                                if code not in have:
                                    s.add(CompanyCountry(company_id=row.id, country_code=code))
                        await s.flush()
                        ids[c.domain] = row.id
                    break
                except IntegrityError:
                    # Another worker inserted the same company first. Its id is deterministic, so
                    # the second attempt reads that row and fills blanks instead.
                    s.expire_all()
                    if attempt:
                        logger.warning("could not store %s", c.domain)
                        ids.setdefault(c.domain, company_id_for(c.domain))
        await s.commit()
    return ids


# ---- step 1: the database ----------------------------------------------------------------------

async def _from_database(ts, q: ProspectQuery, limit: int, taken: set[str]) -> list[Candidate]:
    from nexus.models.account import Account
    from nexus.models.company import Company, CompanyCountry

    if not q.industry_ids or limit <= 0:
        return []
    stmt = (
        select(Company)
        .where(Company.linkedin_fetched_at.isnot(None))
        .where(Company.linkedin_industry_id.in_(sorted(descendants(q.industry_ids))))
        .where(~exists(select(Account.id).where(
            Account.tenant_id == ts.tenant_id, Account.domain == Company.domain)))
    )
    if q.countries:
        stmt = stmt.where(exists(select(CompanyCountry.company_id).where(
            CompanyCountry.company_id == Company.id,
            CompanyCountry.country_code.in_(q.countries))))
    if q.employee_min is not None:
        stmt = stmt.where(or_(Company.employee_range_max.is_(None),
                              Company.employee_range_max >= q.employee_min))
    if q.employee_max is not None:
        stmt = stmt.where(or_(Company.employee_range_min.is_(None),
                              Company.employee_range_min <= q.employee_max))
    if taken:
        stmt = stmt.where(Company.domain.notin_(sorted(taken)))
    stmt = stmt.order_by(Company.linkedin_fetched_at.desc(), Company.id).limit(limit)
    rows = (await ts.session.scalars(stmt)).all()
    return [
        Candidate(domain=r.domain, name=r.name or r.domain, source="database", company_id=r.id,
                  industry=r.industry, country=country_label(r.hq_country_code) or r.country,
                  employee_count=r.employee_count, linkedin_url=r.linkedin_url or "")
        for r in rows
    ]


async def _held(ts, domains) -> set[str]:
    from nexus.models.account import Account

    domains = sorted({d for d in domains if d})
    if not domains:
        return set()
    rows = await ts.session.scalars(select(Account.domain).where(
        Account.tenant_id == ts.tenant_id, Account.domain.in_(domains)))
    return {d for d in rows.all() if d}


# ---- step 2: LinkedIn --------------------------------------------------------------------------

async def _cursor(q: ProspectQuery):
    from nexus.models.prospecting import ProspectCursor

    async with get_platform_sessionmaker()() as s:
        return await s.get(ProspectCursor, q.key())


async def _claim_page(q: ProspectQuery) -> int | None:
    """Take the next unread page of this query, or None when there is none to read.

    A compare-and-set on ``next_page``, so two workers never read the same page.
    """
    from nexus.models.prospecting import ProspectCursor

    key = q.key()
    async with get_platform_sessionmaker()() as s:
        for _ in range(3):
            cursor = await s.get(ProspectCursor, key)
            if cursor is None:
                s.add(ProspectCursor(query_key=key, query=q.as_json(), next_page=1,
                                     exhausted=False))
                try:
                    await s.commit()
                except IntegrityError:
                    await s.rollback()
                continue
            stale = cursor.last_read_at is not None and \
                cursor.last_read_at < utcnow() - timedelta(days=REOPEN_AFTER_DAYS)
            if cursor.exhausted and stale:
                cursor.exhausted, cursor.next_page = False, 1
                await s.commit()
            if cursor.exhausted or cursor.next_page > li.MAX_PAGES:
                return None
            page = cursor.next_page
            moved = await s.execute(
                update(ProspectCursor)
                .where(ProspectCursor.query_key == key, ProspectCursor.next_page == page)
                .values(next_page=page + 1, last_read_at=utcnow()))
            await s.commit()
            if moved.rowcount == 1:
                return page
            s.expire_all()
    return None


async def _release_page(q: ProspectQuery, page: int) -> None:
    """Give back a page that was claimed and never read, so it is not skipped forever."""
    from nexus.models.prospecting import ProspectCursor

    try:
        async with get_platform_sessionmaker()() as s:
            await s.execute(
                update(ProspectCursor)
                .where(ProspectCursor.query_key == q.key(), ProspectCursor.next_page == page + 1)
                .values(next_page=page))
            await s.commit()
    except Exception:
        logger.warning("could not release page %s of %s", page, q.key(), exc_info=True)


async def _record_page(q: ProspectQuery, page: int, info: li.PageInfo, rows: int) -> None:
    from nexus.models.prospecting import ProspectCursor

    async with get_platform_sessionmaker()() as s:
        cursor = await s.get(ProspectCursor, q.key())
        if cursor is None:
            return
        if info.total_pages:
            cursor.total_pages = info.total_pages
        if info.total_results:
            cursor.total_results = info.total_results
        last = min(info.total_pages or page, li.MAX_PAGES)
        if rows == 0 or page >= last:
            cursor.exhausted = True
        cursor.last_read_at = utcnow()
        await s.commit()


def _over_ceiling(total: int | None) -> bool:
    return bool(total and total > _CEILING)


async def _from_linkedin(ts, q: ProspectQuery, need: int, result: ChainResult,
                         taken: set[str], client) -> int:
    """Read pages until ``need`` companies are found or the page budget is spent. Returns how many
    were delivered. Raises the Apify errors; the caller turns them into a note."""
    pages_left = min(MAX_PAGES_PER_RUN, 1 + math.ceil(need / 10))
    delivered = 0
    queue: list[tuple[ProspectQuery, int]] = [(q, 0)]
    while queue and need > delivered and pages_left > 0:
        current, depth = queue.pop(0)
        state = await _cursor(current)
        kids = current.split() if depth < MAX_SPLIT_DEPTH else []
        if state is not None and _over_ceiling(state.total_results) and kids:
            queue[0:0] = [(k, depth + 1) for k in kids]
            continue
        while need > delivered and pages_left > 0:
            page = await _claim_page(current)
            if page is None:
                break
            try:
                companies, info = await li.search_companies(
                    industry_ids=current.industry_ids, locations=current.locations,
                    sizes=current.sizes, page=page, client=client)
            except BaseException:
                await _release_page(current, page)
                raise
            pages_left -= 1
            await _record_page(current, page, info, len(companies))
            delivered += await _take(ts, companies, need - delivered, result, taken)
            if _over_ceiling(info.total_results) and kids:
                queue[0:0] = [(k, depth + 1) for k in kids]
                break
    return delivered


async def _take(ts, companies, want: int, result: ChainResult, taken: set[str]) -> int:
    """Store a page, then deliver up to ``want`` of it that this workspace does not hold."""
    usable = []
    for c in companies:
        if not c.domain:
            result.discarded["no_website"] += 1
        else:
            usable.append(c)
    ids = await store_companies(usable)
    held = await _held(ts, [c.domain for c in usable])
    delivered = 0
    for c in usable:
        if c.domain in held:
            result.discarded["already_held"] += 1
        elif c.domain in taken:
            result.discarded["duplicate"] += 1
        elif delivered < want:
            taken.add(c.domain)
            delivered += 1
            result.candidates.append(Candidate(
                domain=c.domain, name=c.name or c.domain, source="linkedin",
                company_id=ids.get(c.domain), industry=c.industry_name or None,
                country=country_label(c.hq_country), employee_count=c.employee_count,
                linkedin_url=c.linkedin_url))
        else:
            result.stored += 1
    return delivered


# ---- step 3: web search ------------------------------------------------------------------------

async def _from_web(ts, icp: dict, q: ProspectQuery, need: int, result: ChainResult,
                    taken: set[str], web_search: WebSearch | None, product_context: str) -> None:
    from nexus.discovery.auto import _within_geo, _within_size_band

    if web_search is None:
        from nexus.integrations.registry import build_registry_from_settings

        web_search = build_registry_from_settings().company_search
    search_icp = {
        "industries": list(icp.get("industries") or []),
        "geo": list(icp.get("countries") or []),
        "company_size": {"min": icp.get("employee_min"), "max": icp.get("employee_max")},
        "icp_description": product_context,
    }
    try:
        found = await web_search(search_icp, limit=max(need * 3, 10), exclude_domains=None)
    except Exception as exc:
        logger.warning("web company search failed: %r", exc)
        result.notes["web"] = "failed"
        return
    gated = []
    for cand in found or []:
        domain = li.company_domain(cand.domain or cand.url)
        if not domain:
            result.discarded["no_website"] += 1
        elif not (_within_size_band(cand.employee_count, icp) and _within_geo(cand.country, icp)):
            result.discarded["outside_icp"] += 1
        else:
            gated.append((domain, cand))
    held = await _held(ts, [d for d, _ in gated])
    for domain, cand in gated:
        if domain in held:
            result.discarded["already_held"] += 1
        elif domain in taken:
            result.discarded["duplicate"] += 1
        elif need > 0:
            taken.add(domain)
            need -= 1
            result.candidates.append(Candidate(
                domain=domain, name=(cand.name or domain).strip(), source="web",
                industry=cand.industry, country=cand.country,
                employee_count=cand.employee_count))


# ---- the chain ---------------------------------------------------------------------------------

async def find_icp_companies(
    ts, icp: dict, n: int, *, client=None, web_search: WebSearch | None = None,
    product_context: str = "",
) -> ChainResult:
    """Up to ``n`` companies matching the ICP that this workspace does not hold. Never raises for
    a source failing: that source is noted and the next one is asked."""
    from nexus.integrations.apify import ApifyError, ApifyNotConfigured

    result = ChainResult()
    if n <= 0:
        return result
    q = await query_for_icp(icp or {})
    taken: set[str] = set()

    for cand in await _from_database(ts, q, n, taken):
        taken.add(cand.domain)
        result.candidates.append(cand)

    need = n - len(result.candidates)
    linkedin_delivered = 0
    if need > 0:
        if not q.industry_ids:
            result.notes["linkedin"] = "no_industry_codes"
        else:
            try:
                linkedin_delivered = await _from_linkedin(ts, q, need, result, taken, client)
                if not linkedin_delivered:
                    result.notes["linkedin"] = "nothing_new"
            except ApifyNotConfigured:
                result.notes["linkedin"] = "not_configured"
            except ApifyError as exc:
                logger.warning("LinkedIn company search failed: %s", exc)
                result.notes["linkedin"] = "failed"
            except Exception as exc:
                logger.warning("LinkedIn company search failed: %r", exc)
                result.notes["linkedin"] = "failed"

    need = n - len(result.candidates)
    if need > 0 and not linkedin_delivered:
        await _from_web(ts, icp or {}, q, need, result, taken, web_search, product_context)
    return result
