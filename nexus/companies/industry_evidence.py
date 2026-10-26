"""What a company DOES, read off its own website, with the sentence that shows it (2026-10-26).

SDR feedback: "we chose Oil & Gas; it pulled Manufacturing companies and labelled them Oil & Gas".
Measured on the live Exa API, most oilfield equipment makers call THEMSELVES Oil and Gas, in Exa's
company profile and on LinkedIn, so a label a source reported still files a supplier under the
industry its customers are in. Decided with the owner: the industry comes from evidence and is
traceable (D1), and what the company does decides it (D12). "Include suppliers to this industry"
on the ICP takes suppliers back.

Two questions, each answered from a list and with a quote:

1. **Which of LinkedIn's 20 sectors** describes the company's own activity.
2. **Which code inside that sector**, from its own subtree.

An answer counts only when its id was on the list offered AND its quote is a sentence that is on the
page (whitespace and case aside). No quote, an id not offered, or a page with too little text is
no verdict, never a guess: the caller then keeps what the source said. Never raises.

Off by default (``industry_evidence_mode``): a hand-checked sample must clear the precision bar
before it decides anything (`scripts/measure_industry_evidence.py`). ``shadow`` records verdicts on
the account for that measurement and changes nothing.
"""
from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger("nexus.companies.industry_evidence")

#: The pages read, in order: what a company says it does is on its homepage or its About page.
SITE_PATHS = ("/", "/about", "/about-us", "/company")
MAX_PAGES = 2
MAX_TEXT = 6000
#: What the model is shown: the opening of the pages, where a company says what it does. Measured
#: 2026-10-26, a 6,000-character page and all 91 Manufacturing codes made a 14,000-character prompt
#: that Groq rate-limited within minutes, so nothing was answered at all.
PROMPT_TEXT = 3000
#: Codes offered in the second question: the sector's own children always, then those whose words
#: are on the page.
MAX_OPTIONS = 30
MIN_TEXT = 200
MIN_QUOTE = 20
FETCH_TIMEOUT_S = 8.0
MAX_PAGE_BYTES = 1_000_000
#: Verdicts are kept per process this long: the same company is offered to many workspaces.
CACHE_TTL_S = 7 * 24 * 3600

Ask = Callable[[str], Awaitable[str]]
Fetch = Callable[[str], Awaitable[tuple[str, str]]]


@dataclass(frozen=True)
class IndustryVerdict:
    code: int
    label: str
    sector: int
    url: str
    quote: str


def label_of(code: int) -> str:
    from nexus.prospecting.industries import all_industries

    industry = all_industries().get(int(code))
    return industry.label if industry else str(code)


def _roots() -> list:
    from nexus.prospecting.industries import all_industries

    return sorted((i for i in all_industries().values() if ">" not in i.hierarchy),
                  key=lambda i: i.label)


def _subtree(sector: int) -> list:
    from nexus.prospecting.industries import all_industries, descendants

    return sorted((all_industries()[i] for i in descendants([sector]) if i in all_industries()),
                  key=lambda i: (i.depth, i.label))


# ---- reading the site ----------------------------------------------------------------------------

_DROP = re.compile(r"<(script|style|svg|noscript|template)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def visible_text(page: str) -> str:
    """The words a visitor reads: script, style and svg BODIES dropped before tags are stripped
    (the order matters, as in `ingestion/webwatch.normalise`), entities decoded, case kept."""
    text = _DROP.sub(" ", page or "")
    text = _TAG.sub(" ", text)
    text = _html.unescape(text)
    return _SPACE.sub(" ", text).strip()


async def site_text(domain: str) -> tuple[str, str]:
    """``(the first page read, the text of up to two pages)``, or ``("", "")``. Never raises and
    never fetches a private, loopback, link-local or metadata host."""
    from nexus.sources.safety import _is_blocked_host

    host = (domain or "").strip().lower()
    if not host:
        return "", ""
    try:
        blocked, why = _is_blocked_host(host, allow_private=False)
    except Exception:
        blocked, why = True, "unresolvable"
    if blocked:
        logger.info("not reading %s: %s", host, why)
        return "", ""

    import httpx

    first, texts = "", []
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S, follow_redirects=True,
            headers={"User-Agent": "NexusGTM/1.0 (+industry-check)"},
        ) as client:
            for path in SITE_PATHS:
                if len(texts) >= MAX_PAGES:
                    break
                try:
                    resp = await client.get(f"https://{host}{path}")
                except Exception:
                    continue
                final = (resp.url.host or "").lower()
                # A redirect is followed, so the page it LANDED on is re-checked.
                if resp.status_code >= 400 or (final and _is_blocked_host(final, allow_private=False)[0]):
                    continue
                if "html" not in resp.headers.get("content-type", "html"):
                    continue
                text = visible_text(resp.text[:MAX_PAGE_BYTES])
                if text:
                    first = first or str(resp.url)
                    texts.append(text)
    except Exception as exc:
        logger.info("reading %s failed: %r", host, exc)
    return first, " ".join(texts)[:MAX_TEXT]


# ---- asking -----------------------------------------------------------------------------------

_JSON = re.compile(r"\{.*?\}", re.S)


def _parse(text: str) -> dict:
    match = _JSON.search(text or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _squash(text: str) -> str:
    return _SPACE.sub(" ", (text or "").lower()).strip(" .\"'")


def _quoted(quote: str, page: str) -> bool:
    q = _squash(quote)
    return len(q) >= MIN_QUOTE and q in _squash(page)


_RULE = ("You classify a company by what it DOES itself: what it makes, extracts, sells or "
         "provides, not by the industry its customers are in. A company that manufactures "
         "drilling equipment for oil producers is a machinery manufacturer; one that builds "
         "software for hospitals is a software company; one that drills and produces oil is an "
         "oil and gas company. Use only the page text below.")


def _options_for(sector: int, page: str) -> list:
    """The codes worth offering inside a sector: its direct children, then the deeper ones whose
    words are on the page, at most `MAX_OPTIONS`. Offering all 91 Manufacturing codes was the
    prompt that never got answered."""
    from nexus.prospecting.industries import _tokens

    subtree = _subtree(sector)
    if len(subtree) <= MAX_OPTIONS:
        return subtree
    # A word most labels in the sector share ("manufacturing") says nothing about which one.
    counts: dict[str, int] = {}
    for i in subtree:
        for w in _tokens(i.label):
            counts[w] = counts.get(w, 0) + 1
    common = {w for w, n in counts.items() if n > len(subtree) // 4}
    words = _tokens(page[:PROMPT_TEXT]) - common
    top = min(i.depth for i in subtree)
    near = [i for i in subtree if i.depth <= top + 1]
    deeper = sorted((i for i in subtree if i.depth > top + 1 and _tokens(i.label) & words),
                    key=lambda i: (-len(_tokens(i.label) & words), i.depth, i.label))
    return (near + deeper)[:MAX_OPTIONS]


def _sector_prompt(name: str, page: str) -> str:
    options = "\n".join(f"{i.id}: {i.label}" for i in _roots())
    page = page[:PROMPT_TEXT]
    return (f"{_RULE}\n\nCompany: {name}\nPage text:\n\"\"\"\n{page}\n\"\"\"\n\n"
            f"Which ONE sector describes this company's own activity?\n{options}\n\n"
            'Answer ONLY with JSON: {"sector": <id from the list>, "quote": "<one sentence '
            'copied exactly from the page that shows it>"}. If the page does not say what the '
            'company does, answer {"sector": null, "quote": ""}.')


def _code_prompt(name: str, page: str, options: list) -> str:
    def where(i) -> str:
        path = [p.strip() for p in i.hierarchy.split(">")]
        return f" (in {path[-2]})" if len(path) > 2 else ""

    lines = "\n".join(f"{i.id}: {i.label}{where(i)}" for i in options)
    page = page[:PROMPT_TEXT]
    return (f"{_RULE}\n\nCompany: {name}\nPage text:\n\"\"\"\n{page}\n\"\"\"\n\n"
            f"Which ONE of these describes this company's own activity most specifically?\n"
            f"{lines}\n\n"
            'Answer ONLY with JSON: {"id": <id from the list>, "quote": "<one sentence copied '
            'exactly from the page that shows it>"}.')


async def _default_ask(prompt: str) -> str:
    from nexus.agents.llm import LLMMessage, get_llm_provider

    resp = await get_llm_provider().complete(
        [LLMMessage(role="user", content=prompt)], purpose="industry_evidence",
        max_tokens=220, temperature=0.0)
    if getattr(resp, "is_stub", False):
        return ""          # the offline stub reads nothing; that is no verdict, not a guess
    return resp.text


_cache: dict[str, tuple[float, IndustryVerdict | None]] = {}


async def classify(domain: str, name: str = "", *, fetch: Fetch | None = None,
                   ask: Ask | None = None) -> IndustryVerdict | None:
    """The company's industry from its own site, with the quote, or None. Never raises."""
    key = (domain or "").strip().lower()
    if not key:
        return None
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic() and fetch is None and ask is None:
        return hit[1]
    try:
        verdict = await _classify(key, name or key, fetch or site_text, ask or _default_ask)
    except Exception:
        logger.warning("industry evidence for %s failed", key, exc_info=True)
        return None
    if fetch is None and ask is None:
        _cache[key] = (time.monotonic() + CACHE_TTL_S, verdict)
    return verdict


async def _classify(domain: str, name: str, fetch: Fetch, ask: Ask) -> IndustryVerdict | None:
    url, page = await fetch(domain)
    if len(page) < MIN_TEXT:
        return None
    roots = {i.id for i in _roots()}
    first = _parse(await ask(_sector_prompt(name, page)))
    sector = first.get("sector")
    if not isinstance(sector, int) or sector not in roots or not _quoted(
            str(first.get("quote") or ""), page):
        return None
    options = _options_for(sector, page)
    if len(options) <= 1:
        return IndustryVerdict(code=sector, label=label_of(sector), sector=sector, url=url,
                               quote=str(first["quote"]).strip())
    second = _parse(await ask(_code_prompt(name, page, options)))
    code = second.get("id")
    quote = str(second.get("quote") or "")
    if not isinstance(code, int) or code not in {i.id for i in options} or not _quoted(quote, page):
        return None
    return IndustryVerdict(code=code, label=label_of(code), sector=sector, url=url,
                           quote=quote.strip())


async def classify_many(cands, *, concurrency: int = 5,
                        ask: Ask | None = None) -> dict[str, IndustryVerdict | None]:
    """``{domain: verdict}`` for ``(domain, name)`` pairs, a few at a time. Network and LLM only:
    no database session is touched, so this is safe beside an open tenant transaction."""
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(domain: str, name: str):
        async with gate:
            return domain, await classify(domain, name, ask=ask)

    return dict(await asyncio.gather(*(one(d, n) for d, n in cands)))


def matches_icp(verdict: IndustryVerdict, icp: dict) -> bool:
    """Is the company's own activity one of the ICP's industries (a child code counts)?"""
    from nexus.prospecting.industries import descendants

    codes = [int(c) for c in (icp.get("linkedin_industry_ids") or []) if str(c).isdigit()]
    return bool(codes) and verdict.code in descendants(codes)
