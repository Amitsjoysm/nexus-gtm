"""The harvestapi LinkedIn actors: asking them, reading their rows, and the domain gate.

Three actors, registered in ``nexus/integrations/apify.ACTORS``:

* ``linkedin_company_search`` — companies by industry code, location and size band, 50 a page, at
  most 20 pages (LinkedIn's 1,000-result ceiling per query). Full mode returns the website.
* ``linkedin_company`` — one company's page by LinkedIn URL (or by name), in bulk. Used to resolve
  what search cannot give: a similar company's website, an account's own page.
* ``linkedin_company_employees`` — people at one company page, filtered by job title.

**The domain defines the account** (product owner, 2026-09-24). A row becomes a company only
through :func:`company_domain` — its own website, never a social profile, directory, app store or
link-in-bio page. And a LinkedIn page is about an account only when :func:`page_is_for` says the
page's website IS the account's domain. Measured: ``linkedin.com/company/vanta`` is *VANTA -
Chauffeurs* (vantaexec.co.uk); Vanta the security company is ``vanta-security``. Asking the
employees actor about the first returned a fleet coordinator at a leasing firm, which is what an
unverified page does to a contact list.

Parsers read by key and tolerate anything missing. Actor output is not a contract, and a row that
cannot be read is skipped, never guessed at.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from nexus.companies.resolution import normalise_domain

logger = logging.getLogger("nexus.prospecting.linkedin")

PAGE_SIZE = 50
#: LinkedIn serves at most 20 pages of 50 for one query, whatever the total says.
MAX_PAGES = 20
#: An actor run is a scraper session. Apify's sync endpoint itself stops at 300s.
RUN_TIMEOUT_S = 290.0

#: The company-search actor's size filter, with the headcount each band covers.
SIZE_BANDS: tuple[tuple[str, int, int | None], ...] = (
    ("1-10", 1, 10), ("11-50", 11, 50), ("51-200", 51, 200), ("201-500", 201, 500),
    ("501-1000", 501, 1000), ("1001-5000", 1001, 5000), ("5001-10000", 5001, 10000),
    ("10001+", 10001, None),
)

#: Hosts that are never one company's own website: a profile, a listing or a page anyone can make.
#: Added to the discovery list (`integrations/company_search._NON_COMPANY_HOSTS`), which already
#: covers the social networks, directories and app stores.
_NOT_A_COMPANY_SITE = frozenset({
    "tiktok.com", "threads.net", "sites.google.com", "docs.google.com", "drive.google.com",
    "github.com", "gitlab.com", "linktr.ee", "beacons.ai", "bio.link", "lnk.bio", "about.me",
    "calendly.com", "wa.me", "t.me", "linkedin.cn", "lnkd.in", "bit.ly", "fb.com", "fb.me",
    "youtu.be", "notion.so",
})


def _non_company_hosts() -> frozenset[str]:
    from nexus.integrations.company_search import _NON_COMPANY_HOSTS

    return _NON_COMPANY_HOSTS | _NOT_A_COMPANY_SITE


def company_domain(website: str | None) -> str:
    """The company's own domain from a website field, or "" when the field is not a company site.

    ``normalise_domain`` refuses free mail, reserved names and shorteners; this also refuses any
    host that is a profile, directory, app store or link-in-bio page, because each of those is a
    domain every company shares and would merge unrelated companies into one account.
    """
    domain = normalise_domain(website)
    if not domain:
        return ""
    if any(domain == bad or domain.endswith("." + bad) for bad in _non_company_hosts()):
        return ""
    return domain


_COMPANY_PATH = re.compile(r"^/(?:company|showcase|school)/([^/?#]+)", re.I)


def canonical_company_url(url: str | None) -> str:
    """``https://www.linkedin.com/company/<slug>/`` for any spelling of a company page, else "".

    Country subdomains, query strings, trailing sub-pages and case all vary between the search
    output, the details output and what a person pastes; one spelling is what makes them comparable.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return ""
    match = _COMPANY_PATH.match(parts.path or "")
    if not match:
        return ""
    return f"https://www.linkedin.com/company/{match.group(1).lower()}/"


@dataclass(frozen=True)
class LinkedInCompany:
    linkedin_url: str
    linkedin_id: str
    name: str
    website: str
    #: :func:`company_domain` of the website; "" means this row cannot become an account.
    domain: str
    industry_id: int | None = None
    industry_name: str = ""
    employee_count: int | None = None
    range_min: int | None = None
    range_max: int | None = None
    hq_country: str = ""
    countries: list[str] = field(default_factory=list)
    description: str = ""
    similar: list[dict] = field(default_factory=list)
    #: What the actor was asked when it produced this row, when it says (details runs do).
    asked: str = ""


@dataclass(frozen=True)
class PageInfo:
    number: int
    total_pages: int
    total_results: int


@dataclass(frozen=True)
class LinkedInPerson:
    linkedin_url: str
    first_name: str
    last_name: str
    title: str
    company_name: str
    location: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _country(location: dict) -> str:
    parsed = location.get("parsed") if isinstance(location.get("parsed"), dict) else {}
    code = str(parsed.get("countryCode") or location.get("country") or "").strip().upper()
    return code if len(code) == 2 and code.isalpha() else ""


def _first_industry(value) -> tuple[int | None, str]:
    for industry in value or []:
        if isinstance(industry, dict):
            iid = _int(industry.get("id"))
            if iid is not None:
                return iid, str(industry.get("name") or industry.get("title") or "").strip()
    return None, ""


def _similar(value) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for org in value or []:
        if not isinstance(org, dict):
            continue
        url = canonical_company_url(org.get("linkedinUrl"))
        if not url or url in seen:
            continue
        seen.add(url)
        industry_id, _ = _first_industry(org.get("industries"))
        out.append({"name": str(org.get("name") or "").strip(), "linkedin_url": url,
                    "industry_id": industry_id})
    return out


def parse_company(item) -> LinkedInCompany | None:
    """One actor row, or None when it names neither a LinkedIn page nor a company."""
    if not isinstance(item, dict):
        return None
    url = canonical_company_url(item.get("linkedinUrl"))
    if not url and item.get("universalName"):
        url = canonical_company_url(f"linkedin.com/company/{item['universalName']}")
    name = str(item.get("name") or "").strip()
    if not url and not name:
        return None

    website = str(item.get("website") or "").strip()
    industry_id, industry_name = _first_industry(item.get("industries"))
    band = item.get("employeeCountRange") if isinstance(item.get("employeeCountRange"), dict) else {}
    locations = [loc for loc in item.get("locations") or [] if isinstance(loc, dict)]
    countries = list(dict.fromkeys(c for c in (_country(loc) for loc in locations) if c))
    hq = next((_country(loc) for loc in locations if loc.get("headquarter") and _country(loc)), "")
    query = item.get("originalQuery") if isinstance(item.get("originalQuery"), dict) else {}

    return LinkedInCompany(
        linkedin_url=url,
        linkedin_id=str(item.get("id") or "").strip(),
        name=name,
        website=website,
        domain=company_domain(website),
        industry_id=industry_id,
        industry_name=industry_name,
        employee_count=_int(item.get("employeeCount")),
        range_min=_int(band.get("start")),
        range_max=_int(band.get("end")),
        hq_country=hq or (countries[0] if countries else ""),
        countries=countries,
        description=" ".join(str(item.get("description") or "").split()),
        similar=_similar(item.get("similarOrganizations")),
        asked=str(query.get("search") or "").strip(),
    )


def page_is_for(company: LinkedInCompany | None, account_domain: str | None) -> bool:
    """Is this LinkedIn page the account's own? Only when the page's website is the account's domain.

    A name match is not proof: two companies called Vanta have LinkedIn pages, and the one at the
    obvious URL is a chauffeur firm.
    """
    wanted = normalise_domain(account_domain)
    return bool(company and wanted and company.domain == wanted)


def page_info(items: list[dict]) -> PageInfo:
    """The pagination the actor reports on its first row."""
    for item in items:
        meta = item.get("_meta") if isinstance(item, dict) else None
        pagination = meta.get("pagination") if isinstance(meta, dict) else None
        if isinstance(pagination, dict):
            return PageInfo(
                number=_int(pagination.get("pageNumber")) or 0,
                total_pages=_int(pagination.get("totalPages")) or 0,
                total_results=_int(pagination.get("totalResultCount"))
                or _int(pagination.get("totalElements")) or 0,
            )
    return PageInfo(number=0, total_pages=0, total_results=0)


def size_bands(employee_min: int | None, employee_max: int | None) -> list[str]:
    """Every size band an ICP's headcount range overlaps. No range, no filter."""
    if employee_min is None and employee_max is None:
        return []
    lo = employee_min if employee_min is not None else 0
    hi = employee_max
    out = []
    for label, band_lo, band_hi in SIZE_BANDS:
        if hi is not None and band_lo > hi:
            continue
        if band_hi is not None and band_hi < lo:
            continue
        out.append(label)
    return out


def _client(client):
    if client is not None:
        return client
    from nexus.integrations.apify import get_apify_client

    return get_apify_client()


async def search_companies(
    *, industry_ids, locations, sizes, page: int, keywords: str = "", client=None,
) -> tuple[list[LinkedInCompany], PageInfo]:
    """One page (50 companies) of a company search, in full mode so every row carries its website.

    Raises the Apify client's errors unchanged; the chain decides what a failure means.
    """
    body: dict = {"scraperMode": "full", "industryIds": [str(i) for i in industry_ids]}
    if keywords:
        body["searchQuery"] = keywords
    if locations:
        body["locations"] = list(locations)
    if sizes:
        body["companySize"] = list(sizes)
    body.update({"startPage": int(page), "takePages": 1, "maxItems": PAGE_SIZE})
    items = await _client(client).run_actor(
        "linkedin_company_search", body, timeout=RUN_TIMEOUT_S)
    companies = [c for c in (parse_company(i) for i in items) if c is not None]
    return companies, page_info(items)


async def company_details(urls, *, client=None) -> dict[str, LinkedInCompany]:
    """Company pages by LinkedIn URL, in one run, keyed by the canonical URL that was asked.

    Keyed by what was ASKED, from the row's ``originalQuery``, because a page can redirect to
    another slug; a row that does not say what it answered is keyed by its own page.
    """
    wanted = list(dict.fromkeys(u for u in (canonical_company_url(x) for x in urls or []) if u))
    if not wanted:
        return {}
    items = await _client(client).run_actor(
        "linkedin_company", {"companies": wanted}, timeout=RUN_TIMEOUT_S)
    out: dict[str, LinkedInCompany] = {}
    for item in items:
        company = parse_company(item)
        if company is None:
            continue
        key = canonical_company_url(company.asked) or company.linkedin_url
        if key in wanted and key not in out:
            out[key] = company
    return out


async def find_company_page(name: str, *, client=None) -> LinkedInCompany | None:
    """LinkedIn's best match for a company name. A CANDIDATE only: the caller must prove it with
    :func:`page_is_for` before using it for anything."""
    if not (name or "").strip():
        return None
    items = await _client(client).run_actor(
        "linkedin_company", {"searches": [name.strip()]}, timeout=RUN_TIMEOUT_S)
    return next((c for c in (parse_company(i) for i in items) if c is not None), None)


def parse_employee(item) -> LinkedInPerson | None:
    if not isinstance(item, dict):
        return None
    url = str(item.get("linkedinUrl") or "").strip()
    if not url and item.get("id"):
        url = f"https://www.linkedin.com/in/{item['id']}"
    if not url:
        return None
    positions = [p for p in item.get("currentPositions") or [] if isinstance(p, dict)]
    current = next((p for p in positions if p.get("current")), positions[0] if positions else {})
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    return LinkedInPerson(
        linkedin_url=url,
        first_name=str(item.get("firstName") or "").strip(),
        last_name=str(item.get("lastName") or "").strip(),
        title=" ".join(str(current.get("title") or "").split()),
        company_name=str(current.get("companyName") or "").strip(),
        location=str(location.get("linkedinText") or "").strip(),
    )


async def company_employees(
    company_url: str, titles, *, limit: int, client=None,
) -> list[LinkedInPerson]:
    """People at one company page with one of these titles, short mode (no email search: our own
    verified finder supplies the address)."""
    url = canonical_company_url(company_url)
    if not url:
        return []
    body: dict = {"companies": [url]}
    if titles:
        body["jobTitles"] = list(titles)
    body.update({"profileScraperMode": "Short ($4 per 1k)", "maxItems": int(limit)})
    items = await _client(client).run_actor(
        "linkedin_company_employees", body, timeout=RUN_TIMEOUT_S)
    return [p for p in (parse_employee(i) for i in items) if p is not None]


def employee_fits(person: LinkedInPerson, company_name: str, targets) -> tuple[bool, str]:
    """Does this row prove it is a buyer at THIS company? ``(ok, reason_if_not)``.

    The company must be named exactly (after legal suffixes) — the actor is scoped to one page, so
    any other name is a row that leaked from somewhere else. The title must fit the ICP when the ICP
    names titles, and must not be one it excludes.
    """
    from nexus.accounts.dedupe import normalise_name
    from nexus.lookalike.contacts import _is_excluded, icp_title_fit

    if not normalise_name(person.company_name) \
            or normalise_name(person.company_name) != normalise_name(company_name):
        return False, "wrong_company"
    if _is_excluded(person.title, targets):
        return False, "wrong_title"
    if targets.wanted:
        fit, _ = icp_title_fit(person.title, targets)
        if fit <= 0:
            return False, "wrong_title"
    return True, ""
