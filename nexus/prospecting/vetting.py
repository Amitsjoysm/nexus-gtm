"""Is a LinkedIn row's website that company's own, and is the row a company at all? (2026-10-28)

**An acquired or subsidiary company's LinkedIn page lists its owner's website.** Measured on a copy
of the local database, 6 of 16 accounts a Populate added carried another company's domain: Hess
Corporation as chevron.com, Marathon Oil as conocophillips.com, Diamond Offshore as noblecorp.com,
Parker Wellbore as nabors.com, Intermoor as acteon.com, Pioneer as pxd.com (which lands on
ExxonMobil). Each passed every ICP gate on the acquired company's own size and HQ, and the shared
store kept chevron.com as "Hess Corporation", 4,337 staff, for every workspace. Across the copy's
261 LinkedIn-read companies (about forty industries) the same check found link shorteners
(kfrc.co for Kforce, hubs.ly) and pages that are no company ("Stealth Startup", 37,852 staff).

Decided with the product owner 2026-10-28:

* :func:`names_company`: a domain that names the company (a distinctive word of the name, the name
  run together, or its initials) is kept with no request. 94% of the 261.
* :func:`homepage_names`: otherwise the homepage must name it. The title (or ``og:site_name``) must
  hold the name's FIRST distinctive word or the whole name run together, or the site must land on a
  domain that names it. One word is not enough: Nabors' title sells "Wellbore Services".
* A doubtful site that refuses (400+) or does not load is refused: missing beats wrong.
* Refused rows are neither delivered nor stored (`companies.store_companies` vets by default).
* :func:`is_association`: discovery leaves out associations, institutes, societies, councils and
  federations. A plain word rule, chosen knowing it also leaves out a firm named, say, "Council Oak".

Fetching follows the mail-domain reader (`enrichment/mail_domain.fetch_site`): the company's own
site, an honest user agent, every redirect hop checked by the SSRF guard, a capped read, never
raises. Nothing fetched leaves this module but a yes or no.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from nexus.companies.resolution import normalise_domain

logger = logging.getLogger("nexus.prospecting.vetting")

#: Words that say what kind of company, not which one. They never count as naming it on their own.
_GENERIC = frozenset({
    "inc", "corp", "corporation", "co", "company", "llc", "lp", "llp", "ltd", "limited", "plc",
    "gmbh", "ag", "sa", "nv", "bv", "the", "and", "of", "for", "group", "holdings", "holding",
    "international", "intl", "global", "energy", "oil", "gas", "resources", "services", "service",
    "solutions", "technologies", "technology", "systems", "partners", "capital", "software", "labs",
    "health", "financial", "industries", "enterprises", "worldwide", "america", "americas", "usa",
    "us", "uk",
})
_STOP = frozenset({"of", "and", "the", "for", "a", "an"})
#: Second-level labels under which the company's own name is the third label (acme.co.uk).
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc", "or", "ne"})
#: Registries sold as second-level names (acme.us.com).
_SHARED_SUFFIXES = frozenset({"us.com", "uk.com", "eu.com", "gb.net", "za.com", "us.org"})

_ASSOCIATION = re.compile(r"\b(association|institute|society|council|federation)\b", re.I)

_FETCH_TIMEOUT_S = 8.0
_MAX_HOPS = 5
_MAX_BYTES = 300_000
_USER_AGENT = "NexusGTM/1.0 (+company-check)"
_CONCURRENCY = 6
#: Verdicts on homepages that answered, so a page re-read the same day is not fetched again.
_CACHE_TTL_S = 24 * 3600
_CACHE_MAX = 4096
_cache: dict[tuple[str, str], tuple[bool, float]] = {}

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_SITE_NAME = (
    re.compile(r"<meta[^>]+property=[\"']og:site_name[\"'][^>]*content=[\"']([^\"']+)", re.I),
    re.compile(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*property=[\"']og:site_name", re.I),
)


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", (text or "").lower().replace("&", " ")) if w]


def _label(domain: str) -> str:
    """The part of a domain a company chooses: ``acme`` in www.acme.com, acme.co.uk, acme.us.com."""
    host = normalise_domain(domain) or (domain or "").lower().strip(".")
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 3 and (".".join(parts[-2:]) in _SHARED_SUFFIXES or parts[-2] in _SECOND_LEVEL):
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def names_company(domain: str, name: str) -> bool:
    """Does ``domain`` name ``name``? A distinctive word of the name inside the domain, the name run
    together, or its initials (cbi.com is CB&I, spe.org the Society of Petroleum Engineers)."""
    label = re.sub(r"[^a-z0-9]", "", _label(domain))
    words = _words(name)
    if not label or not words:
        return False
    distinctive = [w for w in words if w not in _GENERIC]
    if any(len(w) >= 3 and w in label for w in distinctive):
        return True
    run = "".join(words)
    if len(label) >= 3 and (run.startswith(label) or label.startswith(run)):
        return True
    for initials in ("".join(w[0] for w in words if w not in _STOP),
                     "".join(w[0] for w in distinctive if w not in _STOP)):
        if len(initials) < 2 or len(label) < 2:
            continue
        # Two letters match only exactly: "co" (Cabot Oil) is the start of coterra.com, the
        # company that bought it (2026-10-29).
        if label == initials or (len(initials) >= 3 and len(label) >= 3 and (
                initials.startswith(label) or label.startswith(initials))):
            return True
    return False


def _page_names(text: str, name: str) -> bool:
    """Does a homepage's title text name the company: its first distinctive word, or the whole
    name run together? One later word is not enough ("Wellbore" in Nabors' title)."""
    seen = _words(text)
    words = _words(name)
    distinctive = [w for w in words if w not in _GENERIC and len(w) >= 3]
    if distinctive and distinctive[0] in set(seen):
        return True
    run = "".join(words)
    return len(run) >= 5 and run in "".join(seen)


def _title_text(page: str) -> str:
    parts = [m.group(1) for m in [_TITLE.search(page)] if m]
    for pattern in _SITE_NAME:
        parts += [m.group(1) for m in [pattern.search(page)] if m]
    return html.unescape(" ".join(re.sub(r"\s+", " ", p) for p in parts)).strip()


async def _fetch_homepage(domain: str) -> tuple[str, int, str]:
    """``(final host, status, title text)`` of the company's homepage. Raises when it cannot be
    read, or when any hop is a private or reserved host."""
    from urllib.parse import urljoin, urlsplit

    import httpx

    from nexus.sources.safety import _is_blocked_host

    url = f"https://{domain}/"
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S, follow_redirects=False,
                                 headers={"User-Agent": _USER_AGENT}) as client:
        for _ in range(_MAX_HOPS + 1):
            host = (urlsplit(url).hostname or "").lower()
            # The guard resolves the name, which blocks: off the event loop, as `has_mx` does.
            blocked, why = await asyncio.to_thread(_is_blocked_host, host, allow_private=False)
            if blocked:
                raise PermissionError(f"not fetching {host}: {why}")
            async with client.stream("GET", url) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    url = urljoin(url, resp.headers["location"])
                    continue
                body, size = [], 0
                async for chunk in resp.aiter_bytes():
                    body.append(chunk)
                    size += len(chunk)
                    if size >= _MAX_BYTES:
                        break
                page = b"".join(body).decode(resp.encoding or "utf-8", errors="replace")
                return host, resp.status_code, _title_text(page)
    raise OSError(f"more than {_MAX_HOPS} redirects from {domain}")


def forget() -> None:
    """Drop remembered verdicts (tests, and a re-run after a site was fixed)."""
    _cache.clear()


async def homepage_names(domain: str, name: str, *, fetch=None) -> bool:
    """Does the homepage at ``domain`` name ``name``? Never raises; a site that does not answer is
    a no."""
    key = (domain, " ".join(_words(name)))
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[1] < _CACHE_TTL_S:
        return hit[0]
    try:
        final_host, status, text = await (fetch or _fetch_homepage)(domain)
    except Exception as exc:
        logger.info("homepage of %s could not be read: %r", domain, exc)
        return False
    verdict = status < 400 and (_page_names(text, name) or names_company(final_host, name))
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (verdict, time.monotonic())
    return verdict


def _same_site(host: str, domain: str) -> bool:
    host = normalise_domain(host) or (host or "").lower()
    domain = normalise_domain(domain) or (domain or "").lower()
    return bool(host and domain) and (
        host == domain or host.endswith("." + domain) or domain.endswith("." + host))


async def lands_elsewhere(domain: str, name: str, *, fetch=None) -> bool:
    """Does the homepage at ``domain`` now land on another site, one whose domain does not name
    ``name``? Never raises.

    Measured on Parallel's first real search (2026-10-29): cabotog.com lands on coterra.com,
    marathonoil.com on conocophillips.com, swn.com on expandenergy.com, pxd.com on exxonmobil.com,
    trianglepetroleum.com on a gambling site. The clearest free sign a company was bought or is
    gone, and one Parallel's own check missed for Cabot. A site that does not answer is no evidence
    either way (Devon's and Diamondback's did not answer from here), so it is a no."""
    key = ("elsewhere:" + domain, " ".join(_words(name)))
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[1] < _CACHE_TTL_S:
        return hit[0]
    try:
        final_host, _status, _text = await (fetch or _fetch_homepage)(domain)
    except Exception as exc:
        logger.info("homepage of %s could not be read: %r", domain, exc)
        return False
    verdict = (bool(final_host) and not _same_site(final_host, domain)
               and not names_company(final_host, name))
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (verdict, time.monotonic())
    return verdict


async def vet(rows, *, fetch=None) -> tuple[list, list]:
    """Split LinkedIn rows into ``(kept, refused)``: refused rows carry a website that is not
    theirs. Only rows whose domain does not name them cost a request. A row with no domain is kept
    for the caller to drop as it always has."""
    rows = list(rows)
    doubtful = [r for r in rows if r.domain and not names_company(r.domain, r.name or "")]
    if not doubtful:
        return rows, []
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def check(row) -> bool:
        async with gate:
            return await homepage_names(row.domain, row.name or "", fetch=fetch)

    verdicts = await asyncio.gather(*(check(r) for r in doubtful))
    refused_ids = {id(r) for r, ok in zip(doubtful, verdicts) if not ok}
    refused = [r for r in rows if id(r) in refused_ids]
    if refused:
        logger.info("refused %d LinkedIn rows whose website is not theirs: %s", len(refused),
                    ", ".join(f"{r.name} -> {r.domain}" for r in refused[:10]))
    return [r for r in rows if id(r) not in refused_ids], refused


def is_association(name: str | None) -> bool:
    """An association, institute, society, council or federation, by a whole word of its name."""
    return bool(_ASSOCIATION.search(name or ""))
