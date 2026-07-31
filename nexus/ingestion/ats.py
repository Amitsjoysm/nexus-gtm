# nexus/ingestion/ats.py
"""Applicant-tracking-system job boards: discovery and fetch.

An open requisition is the strongest hiring signal available. It is the company's own words, it is
current by definition (closed reqs come down), it names the team that has budget, and — unlike a
scanner-derived headcount estimate — it says *what* they are building.

**No API key anywhere in this file.** Greenhouse, Lever and Ashby all serve their public job boards
as unauthenticated JSON. The key-dependent layer in this project is web *search*; boards are not
part of it.

**Discovery is the hard part, and guessing does not work.** The board token is not the domain root:
``vanta`` and ``ramp`` 404 on Greenhouse because both are on Ashby, and Linear's Ashby token is
capitalised ``Linear``. A wrong guess returns 404, which is indistinguishable from "not hiring" —
so a guess-only strategy fails silently and forever.

What works is reading the token out of the company's **own careers page**, which embeds its ATS.
Measured against live sites: vanta.com → ``ashby:vanta``, ramp.com → ``ashby:ramp``, linear.app →
``ashby:Linear``, figma.com → ``greenhouse:figma``. Stripe runs a bespoke careers site with no token
in the HTML, and is recovered by the domain-root guess against Greenhouse.

The reverse — scraping the careers page for the *postings* — is deliberately not done. Ramp's
careers page is 3.7 MB of JavaScript shell whose listings come from Ashby regardless; reverse
engineering a SPA to obtain data that is one unauthenticated GET away is strictly worse.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("nexus.ingestion.ats")

_UA = "NexusGTM/1.0 (+https://github.com/nexus-gtm; signals)"

# Token patterns as they appear embedded in a careers page. Ordered by how specific they are: a
# posting URL (jobs.lever.co/acme/<id>) is stronger evidence than a bare board reference.
_TOKEN_PATTERNS: tuple[tuple[str, str], ...] = (
    ("greenhouse", r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([A-Za-z0-9_-]+)"),
    ("ashby", r"(?:jobs\.ashbyhq\.com|api\.ashbyhq\.com/posting-api/job-board)/([A-Za-z0-9_-]+)"),
)

# Paths a careers page actually lives at, most conventional first.
_CAREERS_PATHS = ("/careers", "/jobs", "/company/careers", "/about/careers", "/careers/")

# Tokens that appear in these patterns but are never a real board name — the pattern would happily
# match a documentation link or an embed helper.
_TOKEN_BLOCKLIST = frozenset({"embed", "job_board", "js", "www", "api", "static", "assets"})


@dataclass(slots=True)
class BoardRef:
    """Which board a company uses, and how we came to believe that."""

    provider: str
    token: str
    # "careers_page" | "domain_guess" | "configured" — kept because a guessed token that starts
    # 404ing should be re-discovered, while a configured one should be left alone.
    via: str = "careers_page"


@dataclass(slots=True)
class Posting:
    """One open requisition, normalised across providers."""

    external_id: str
    title: str
    url: str = ""
    department: str = ""
    location: str = ""
    published_at: str = ""
    body: str = ""


@dataclass(slots=True)
class BoardResult:
    """Outcome of one board fetch.

    ``found`` is not the same as ``ok``. A board that returns HTTP 200 with an empty list exists and
    has nothing open — real information about a company that has stopped hiring. A 404 means they
    are not on this ATS at all. Collapsing the two would turn "stopped hiring" into "we cannot see
    them", which is the difference between a signal and a blind spot.
    """

    provider: str = ""
    token: str = ""
    outcome: str = "empty"          # ok | empty | not_found | error
    postings: list[Posting] = field(default_factory=list)
    error: str = ""
    # How the board was found, so the caller can cache it and skip discovery next time.
    ref: "BoardRef | None" = None


async def _get(url: str, fetch=None) -> tuple[int, str]:
    """(status, body). Never raises — a dead board must not break ingestion.

    The guard covers the injected transport too, not just the live one. It wrapped only httpx at
    first, which made the contract true in production and false under test — precisely backwards,
    since the test is where the failure path is meant to be provable.
    """
    try:
        if fetch is not None:
            return await fetch(url)
        import httpx

        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True, headers={"User-Agent": _UA}
        ) as client:
            resp = await client.get(url)
            return resp.status_code, resp.text
    except Exception as exc:
        logger.warning("ATS fetch failed for %s: %r", url, exc)
        return 0, ""


def extract_board_ref(html: str) -> BoardRef | None:
    """Find the ATS token embedded in a careers page.

    Returns the first provider whose pattern matches. Ordering in ``_TOKEN_PATTERNS`` decides ties,
    which matters for companies migrating between systems — a stale Greenhouse link can linger in a
    footer long after the live board moved.
    """
    for provider, pattern in _TOKEN_PATTERNS:
        for match in re.finditer(pattern, html or "", re.I):
            token = (match.group(1) or "").strip()
            # Case is preserved on purpose: Linear's Ashby token is `Linear`, and lowercasing it
            # produces a 404.
            if token and token.lower() not in _TOKEN_BLOCKLIST:
                return BoardRef(provider=provider, token=token, via="careers_page")
    return None


async def discover_board(domain: str, *, fetch=None) -> BoardRef | None:
    """Find which board a company uses, by reading its careers page.

    Tries the conventional careers paths until one returns HTML containing a token. Stops at the
    first hit — a company has one live board, and continuing would only find stale footer links.
    """
    domain = (domain or "").strip().lower().lstrip("@").rstrip("/")
    if not domain:
        return None
    for path in _CAREERS_PATHS:
        status, body = await _get(f"https://{domain}{path}", fetch)
        if status != 200 or not body:
            continue
        ref = extract_board_ref(body)
        if ref is not None:
            logger.info("discovered %s board %r for %s via %s", ref.provider, ref.token,
                        domain, path)
            return ref
    return None


# Providers that publish the board owner's name, so a guessed token can be proved to belong to this
# company. Only these may be guessed at all — see `guess_refs`.
_OWNER_ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}",
}


def guess_refs(domain: str) -> list[BoardRef]:
    """Fallback candidates from the domain root, tried only when the careers page yields nothing.

    Restricted to providers that expose the board **owner's name**, because an unverified guess
    attaches another company's jobs to this account. Measured: ``example.com`` guesses the Greenhouse
    token ``example``, which is a real board with 21 open roles belonging to a company called
    "Democorp". Every one of those would have become a hiring signal on the wrong account.

    Ashby and Lever publish no owner metadata, so a guess there cannot be checked — and an
    unverifiable guess is worse than no guess, because it produces confident, specific, wrong
    signals rather than an obvious gap. They are discovered from the careers page or not at all.
    """
    root = (domain or "").strip().lower().lstrip("@").split(".")[0]
    if not root:
        return []
    return [BoardRef(provider=p, token=root, via="domain_guess") for p in _OWNER_ENDPOINTS]


def _owner_matches(board_name: str, account_name: str, domain: str) -> bool:
    """Whether a board's stated owner is plausibly this account.

    Token overlap rather than equality: "Stripe" vs "Stripe, Inc." and "Figma" vs "Figma Inc" must
    match, while "Democorp" vs "Example" must not.
    """
    from nexus.ingestion.sources import _GENERIC_NAME_TOKENS

    def tokens(value: str) -> set[str]:
        return {
            t for t in re.split(r"[^a-z0-9]+", (value or "").lower())
            if len(t) >= 3 and t not in _GENERIC_NAME_TOKENS
        }

    board = tokens(board_name)
    if not board:
        return False
    wanted = tokens(account_name) | tokens((domain or "").split(".")[0])
    return bool(board & wanted)


async def verify_owner(ref: BoardRef, *, account_name: str, domain: str, fetch=None) -> bool:
    """Confirm a guessed board belongs to this company. Discovered tokens skip this.

    A token read off the company's own careers page is already evidence of ownership — the company
    put it there. A guessed one is evidence of nothing.
    """
    import json

    if ref.via != "domain_guess":
        return True
    endpoint = _OWNER_ENDPOINTS.get(ref.provider)
    if endpoint is None:
        return False            # cannot verify => cannot guess
    status, body = await _get(endpoint.format(t=ref.token), fetch)
    if status != 200 or not body:
        return False
    try:
        name = str((json.loads(body) or {}).get("name") or "")
    except Exception:
        return False
    ok = _owner_matches(name, account_name, domain)
    if not ok:
        logger.info(
            "rejected guessed %s board %r: owned by %r, not %r",
            ref.provider, ref.token, name, account_name or domain,
        )
    return ok


# ---- per-provider fetch + normalisation -------------------------------------------------------

def _text(value) -> str:
    return (value or "").strip() if isinstance(value, str) else ""


def _parse_greenhouse(data: dict) -> list[Posting]:
    out = []
    for j in (data.get("jobs") or []):
        if not isinstance(j, dict):
            continue
        offices = j.get("location") or {}
        out.append(
            Posting(
                external_id=str(j.get("id") or j.get("internal_job_id") or ""),
                title=_text(j.get("title")),
                url=_text(j.get("absolute_url")),
                # The list endpoint carries no department unless ?content=true is requested; the
                # extra payload is not worth it for a signal that keys on title and count.
                department=_text((j.get("departments") or [{}])[0].get("name"))
                if j.get("departments") else "",
                location=_text(offices.get("name")) if isinstance(offices, dict) else "",
                published_at=_text(j.get("updated_at")),
            )
        )
    return out


def _parse_lever(data: list) -> list[Posting]:
    out = []
    for j in data or []:
        if not isinstance(j, dict):
            continue
        cats = j.get("categories") or {}
        out.append(
            Posting(
                external_id=str(j.get("id") or ""),
                title=_text(j.get("text")),
                url=_text(j.get("hostedUrl")),
                department=_text(cats.get("team") or cats.get("department")),
                location=_text(cats.get("location")),
                published_at=str(j.get("createdAt") or ""),
            )
        )
    return out


def _parse_ashby(data: dict) -> list[Posting]:
    out = []
    for j in (data.get("jobs") or []):
        if not isinstance(j, dict):
            continue
        # `isListed` false means the req exists but is not public; treating it as an open role
        # would report hiring the company has not announced.
        if j.get("isListed") is False:
            continue
        out.append(
            Posting(
                external_id=str(j.get("id") or ""),
                title=_text(j.get("title")),
                url=_text(j.get("jobUrl") or j.get("applyUrl")),
                department=_text(j.get("department") or j.get("team")),
                location=_text(j.get("location")),
                published_at=_text(j.get("publishedAt")),
            )
        )
    return out


_PROVIDERS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{t}/jobs", _parse_greenhouse),
    "lever": ("https://api.lever.co/v0/postings/{t}?mode=json", _parse_lever),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{t}", _parse_ashby),
}


async def fetch_board(ref: BoardRef, *, fetch=None) -> BoardResult:
    """Fetch one board. Never raises; every failure mode is a named outcome."""
    import json

    spec = _PROVIDERS.get(ref.provider)
    if spec is None:
        return BoardResult(provider=ref.provider, token=ref.token, outcome="error",
                           error=f"unknown provider {ref.provider!r}")
    url_tmpl, parse = spec
    status, body = await _get(url_tmpl.format(t=ref.token), fetch)
    result = BoardResult(provider=ref.provider, token=ref.token)
    if status == 404:
        result.outcome = "not_found"     # not on this ATS — distinct from "nothing open"
        return result
    if status != 200 or not body:
        result.outcome = "error"
        result.error = f"HTTP {status}"
        return result
    try:
        data = json.loads(body)
    except Exception as exc:
        result.outcome = "error"
        result.error = f"invalid JSON: {exc}"
        return result
    try:
        result.postings = parse(data)
    except Exception as exc:          # a shape change must degrade, not crash ingestion
        result.outcome = "error"
        result.error = f"parse failed: {type(exc).__name__}: {exc}"
        return result
    result.outcome = "ok" if result.postings else "empty"
    return result


async def resolve_and_fetch(
    domain: str, *, account_name: str = "", configured: BoardRef | None = None, fetch=None
) -> BoardResult:
    """The waterfall: configured token → careers-page discovery → verified domain-root guess.

    Returns the first board that actually answers, and the ``ref`` it came from so the caller can
    cache it. A ``not_found`` is not a failure — it is how we learn the company is not on that ATS —
    so the search continues to the next candidate.
    """
    if configured is not None:
        result = await fetch_board(configured, fetch=fetch)
        if result.outcome in ("ok", "empty"):
            result.ref = configured
            return result

    discovered = await discover_board(domain, fetch=fetch)
    if discovered is not None:
        result = await fetch_board(discovered, fetch=fetch)
        if result.outcome in ("ok", "empty"):
            result.ref = discovered
            return result

    for ref in guess_refs(domain):
        # Verify BEFORE fetching: a board that fails the ownership check must not contribute
        # postings even once.
        if not await verify_owner(ref, account_name=account_name, domain=domain, fetch=fetch):
            continue
        result = await fetch_board(ref, fetch=fetch)
        if result.outcome in ("ok", "empty"):
            result.ref = ref
            return result
    return BoardResult(outcome="not_found")
