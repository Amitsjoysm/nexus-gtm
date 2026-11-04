"""AI-drafted ICP from a company's own website.

A founder/SDR pastes their website URL; this gathers what the company does (web search) and asks
the LLM to infer the ideal customer profile of the businesses THEY SELL TO — industries, company
size, geography, required tech, buyer titles, value props, and product context. The result is a
DRAFT the user reviews and edits in the Relevance engine before saving; nothing is persisted here.

Offline (stub search/LLM) yields an empty draft — never fabricated. Never raises across the boundary.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger("nexus.relevance.website_icp")

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)

# Models wrap JSON in ```json ... ``` fences even when told not to — measured against Claude on a
# live deployment, with "Output ONLY a JSON object — no prose, no code fences" in the system
# prompt. The greedy `\{.*\}` above survives a fence, but only when the object CLOSES; strip them
# first so a truncated-inside-a-fence response fails for the one reason that is actually true.
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def domain_of(url: str) -> str:
    """Best-effort registrable host from a pasted URL/domain. '' when unparseable."""
    u = (url or "").strip()
    if u and "://" not in u:
        u = "https://" + u
    parsed = urlparse(u)
    host = (parsed.netloc or parsed.path).lower().strip("/")
    if host.startswith("www."):
        host = host[4:]
    return host.split("/")[0]


def _empty_draft() -> dict:
    return {"icp": {}, "value_props": [], "product_context": ""}


# ---- a service's own page (W8, 2026-10-29) --------------------------------------------------------
# A company selling several services has a page per service (`acme.com/services/managed-it`), and
# the ICP of one service is not the ICP of the company. A pasted route used to be cut to its domain
# and researched as the whole company, so every service drafted the same ICP.

_PAGE_MAX_BYTES = 300_000
_PAGE_MAX_HOPS = 5
_PAGE_TIMEOUT_S = 10
_PAGE_TEXT_CHARS = 4000
_USER_AGENT = "NexusGTM/1.0 (+icp-draft)"
_DROP = re.compile(r"<(script|style|svg|noscript|template)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def service_path(url: str) -> str:
    """The path of a pasted URL when it names a page rather than the site: ``/services/abc``, or
    "" for the homepage."""
    u = (url or "").strip()
    if u and "://" not in u:
        u = "https://" + u
    path = (urlparse(u).path or "").strip()
    return "" if path in ("", "/") else path


def _path_words(path: str) -> str:
    """``/services/managed-it-support`` -> "managed it support": the last meaningful segment, the
    words a search engine indexes that page under."""
    segments = [s for s in path.split("/") if s and s.lower() not in ("services", "service",
                                                                       "solutions", "products",
                                                                       "en", "us", "uk")]
    last = segments[-1] if segments else ""
    last = re.sub(r"\.(html?|php|aspx?)$", "", last, flags=re.I)
    return " ".join(w for w in re.split(r"(?:%20|[-_+])+", last) if w).strip()


def page_text(html_text: str) -> str:
    """The page's title and readable text, case kept, capped at ``_PAGE_TEXT_CHARS``."""
    import html as html_lib

    title = _TITLE.search(html_text or "")
    body = _TAG.sub(" ", _DROP.sub(" ", html_text or ""))
    text = html_lib.unescape(re.sub(r"\s+", " ", body)).strip()
    head = html_lib.unescape(re.sub(r"\s+", " ", title.group(1))).strip() if title else ""
    return (f"{head}\n{text}" if head else text)[:_PAGE_TEXT_CHARS]


async def fetch_page(url: str) -> tuple[int, str]:
    """``(status, html)`` of a public page. Every redirect hop is checked against the SSRF guard
    (a pasted address is the tenant's to choose, and a metadata or private host is not ours to
    read for them); at most 300 KB is read. Raises when it cannot be read."""
    import asyncio
    from urllib.parse import urljoin, urlsplit

    import httpx

    from nexus.sources.safety import _is_blocked_host

    async with httpx.AsyncClient(timeout=_PAGE_TIMEOUT_S, follow_redirects=False,
                                 headers={"User-Agent": _USER_AGENT}) as client:
        for _ in range(_PAGE_MAX_HOPS + 1):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https"):
                raise PermissionError(f"not fetching {parts.scheme}: URLs")
            host = (parts.hostname or "").lower()
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
                    if size >= _PAGE_MAX_BYTES:
                        break
                return resp.status_code, b"".join(body).decode(resp.encoding or "utf-8",
                                                                errors="replace")
    raise OSError(f"more than {_PAGE_MAX_HOPS} redirects from {url}")


async def read_service_page(url: str, *, fetch=None) -> str:
    """The readable text of a service page, or "" when it cannot be read. Never raises."""
    u = (url or "").strip()
    if u and "://" not in u:
        u = "https://" + u
    try:
        status, body = await (fetch or fetch_page)(u)
    except Exception as exc:
        logger.info("website_icp: could not read %s: %r", u, exc)
        return ""
    return page_text(body) if status == 200 and body else ""


def _strlist(x, cap: int = 20) -> list[str]:
    return [str(v).strip() for v in (x or []) if str(v).strip()][:cap]


def _intn(x) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _coerce(data: dict) -> dict:
    """Normalize the LLM's JSON into the RelevanceProfileIn shape, dropping anything malformed."""
    icp_in = data.get("icp") or {}
    icp = {
        "industries": _strlist(icp_in.get("industries")),
        "countries": _strlist(icp_in.get("countries")),
        "required_tech": _strlist(icp_in.get("required_tech")),
        "employee_min": _intn(icp_in.get("employee_min")),
        "employee_max": _intn(icp_in.get("employee_max")),
        "buyer_titles": _strlist(icp_in.get("buyer_titles"), cap=10),
    }
    value_props = []
    for vp in (data.get("value_props") or [])[:6]:
        if isinstance(vp, dict) and str(vp.get("name", "")).strip():
            value_props.append({
                "name": str(vp["name"]).strip()[:120],
                "description": str(vp.get("description", "")).strip()[:400],
                "pains_solved": _strlist(vp.get("pains_solved"), cap=8),
            })
    return {
        "icp": icp,
        "value_props": value_props,
        "product_context": str(data.get("product_context", "")).strip()[:1000],
    }


async def analyze_website_to_icp(url: str, *, search, llm, fetch=None) -> dict:
    """Draft an ICP from a website URL. Returns ``{icp, value_props, product_context}`` (empty on
    failure or offline). ``search`` is the registry search callable; ``llm`` an LLM provider.

    A URL with a path (``acme.com/services/abc``) is ONE service the company sells: that page is
    read (``fetch`` is the test seam), the searches name the service, and the model is asked for
    the buyers of that service rather than of the whole company (W8)."""
    domain = domain_of(url)
    if not domain:
        return _empty_draft()
    path = service_path(url)
    service_words = _path_words(path) if path else ""
    page = await read_service_page(url, fetch=fetch) if path else ""

    # 1) Gather what the company does from public web results.
    queries = (
        (f"{domain} {service_words} service customers",
         f"{domain} {service_words} case studies industries served")
        if service_words else
        (f"{domain} product what they do customers",
         f"{domain} pricing case studies industries served")
    )
    snippets: list[str] = []
    for query in queries:
        try:
            hits = await search(query, limit=5) or []
        except Exception:  # a flaky search must not break the draft
            hits = []
        for h in hits:
            title = getattr(h, "title", "") or ""
            snippet = getattr(h, "snippet", "") or ""
            link = getattr(h, "url", "") or ""
            snippets.append(f"- {title} :: {snippet} ({link})")
    blob = "\n".join(snippets[:12]).strip()
    if page:
        # The service's own words first: they say what this service is, the search what the
        # company around it does.
        blob = f"THE SERVICE PAGE ({url.strip()}):\n{page}\n\nWEB RESEARCH:\n{blob}".strip()
    if not blob:
        # The THIRD way this returns an empty draft, and the only one that never reaches the LLM —
        # so none of the logging below can report it. A dead or unkeyed search provider produces
        # exactly the same blank form and the same "couldn't analyze that site" toast as a genuinely
        # uninformative website, which is how a provider outage reads as a product defect.
        logger.warning(
            "website_icp: no web results for %s, so the ICP draft is empty and no LLM call was "
            "made. Check the configured search provider has a working key.",
            domain,
        )
        return _empty_draft()

    # 2) LLM infers the ICP of who they SELL TO.
    from nexus.agents.llm import LLMMessage

    system = (
        "You are a GTM strategist. From a company's web presence, infer the ideal customer "
        "profile (ICP) of the businesses THEY SELL TO — not the company itself. "
        "Output ONLY a JSON object — no prose, no code fences."
    )
    scope = ""
    if path:
        system += (
            " The page given describes ONE service the company sells. Describe the buyers of THAT "
            "service: its industries, company sizes and buyer titles, its value propositions and "
            "the pains it solves. Other services the company sells are not part of this ICP."
        )
        scope = f"Service page: {url.strip()}\n"
    user = (
        f"Company domain: {domain}\n{scope}Web research:\n{blob}\n\n"
        'Return a JSON object with keys: "icp": {"industries": [..], "countries": [..], '
        '"required_tech": [..], "employee_min": int|null, "employee_max": int|null, '
        '"buyer_titles": [up to 8 job titles]}, "value_props": [{"name","description",'
        '"pains_solved":[..]}], "product_context": "1-3 sentences on what the company does and '
        'its differentiators". Base every field on the research; if unknown use [] or null.'
    )
    try:
        resp = await llm.complete(
            [LLMMessage("system", system), LLMMessage("user", user)],
            # 800 was NOT enough and the failure was invisible. Measured against Claude on a live
            # deployment: the requested object — industries, countries, tech, 8 buyer titles, 6
            # value props each with a description and a pains list, plus product context — ran to
            # ~2,996 characters (~750 tokens) and stopped AT the cap, mid-string.
            #
            # A truncated object has no closing brace, so `_JSON_OBJ` matches nothing and the
            # function returns an empty draft. The user sees "Couldn't analyze that site" after a
            # perfectly successful, fully billed LLM call.
            temperature=0.1, max_tokens=2000, purpose="website_icp",
        )
    except Exception as exc:  # provider isolation
        logger.warning("website_icp llm failed for %s: %r", domain, exc)
        return _empty_draft()

    # EVERY FAILURE BELOW LOGS. They used to return an empty draft silently, which is
    # indistinguishable from "this site has no discoverable ICP" — so a truncated response, a
    # fenced response and a genuinely uninformative website all produced the same blank form and
    # the same unhelpful toast. The response length is included because it is what identifies
    # truncation: a value at or just under the token cap is the tell.
    raw = _FENCE.sub("", (resp.text or "").strip())
    match = _JSON_OBJ.search(raw)
    if not match:
        logger.warning(
            "website_icp: no JSON object in LLM response for %s (len=%d, likely truncated at "
            "max_tokens or prose-only). head=%r",
            domain, len(raw), raw[:200],
        )
        return _empty_draft()
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "website_icp: LLM response for %s was not valid JSON (len=%d): %s. tail=%r",
            domain, len(raw), exc, raw[-200:],
        )
        return _empty_draft()
    if not isinstance(data, dict):
        logger.warning("website_icp: LLM returned %s, not an object, for %s", type(data).__name__, domain)
        return _empty_draft()

    draft = _coerce(data)
    if not draft["icp"].get("industries") and not draft["product_context"]:
        # Parsed cleanly but every field the UI checks is empty — almost always a key-name
        # mismatch (the model answering with `geographies`/`roles`/`company_size` instead of the
        # requested `countries`/`buyer_titles`/`employee_min`). Worth seeing the keys it DID send.
        logger.warning(
            "website_icp: parsed JSON for %s but coerced to an empty draft. "
            "top-level keys=%s icp keys=%s",
            domain, sorted(data.keys()), sorted((data.get("icp") or {}).keys()),
        )
    return draft
