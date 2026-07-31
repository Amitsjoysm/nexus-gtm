"""Signal sources and the built-in signal library.

A ``SignalSource`` yields ``RawSignal`` objects for an account. The ``IngestionService``
normalizes, dedupes, and persists them. Real 3rd-party sources (G2, web-visitor pixels, CRM)
implement the same interface; ``DemoSignalSource`` keeps the system runnable with no network.

Three live sources ship, and they are complementary rather than alternatives:

* ``WebNewsSource``   — one broad OR-query per account. Catches events nobody indexed under a
  recognisable phrase.
* ``DorkedSearchSource`` — one high-precision query per signal *kind* (``dorks.py``), preferring
  recent results. Catches the events a single relevance-ranked result set crowds out.
* ``RssSignalSource`` — the company's own feed. No name-matching needed, and it works for companies
  too small for anyone else to cover.

All three route their dedupe through :func:`event_dedupe_key`, so when two of them find the same
funding round it becomes one signal rather than three.
"""
from __future__ import annotations

import abc
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from nexus.core.db import utcnow
from nexus.models.account import Account

logger = logging.getLogger("nexus.ingestion.sources")

# Name tokens too generic to identify an account in a news headline.
_GENERIC_NAME_TOKENS = frozenset({
    "inc", "llc", "ltd", "corp", "the", "and", "co", "company", "group", "holdings",
    "technologies", "technology", "tech", "solutions", "systems", "global",
    "international", "services", "labs", "io", "app", "ai",
})

# Headlines that are real buying signals -> (signal kind, strength). First match wins. A headline
# that mentions the account but matches none of these is a low-strength "news" mention (kept for
# the timeline, but on its own it won't create an Inbox task).
_NEWS_PATTERNS: tuple[tuple[str, tuple[str, ...], float], ...] = (
    ("funding", ("raises", "raised", "funding round", "series a", "series b", "series c",
                 "series d", "seed round", "venture", "valuation", "secures $", "raises $"), 0.85),
    ("hiring", ("appoints", "names new", "hires", "joins as", "new ceo", "new cfo", "new cro",
                "new chief", "promoted to", "is hiring", "headcount"), 0.6),
    ("news", ("acquires", "acquisition", "merger", "partners with", "partnership", "launches",
              "expands", "expansion", "opens new", "ipo", "goes public"), 0.6),
)


def _account_keys(account: Account) -> set[str]:
    """Identifying tokens that must appear in a headline for it to be 'about' this account — the
    main name token(s) plus the domain root. Drops generic corp-suffix words."""
    keys: set[str] = set()
    name = (account.name or "").strip().lower()
    if name:
        keys.add(name)
        for tok in re.split(r"[^a-z0-9]+", name):
            if len(tok) >= 3 and tok not in _GENERIC_NAME_TOKENS:
                keys.add(tok)
    root = (account.domain or "").lower().split(".")[0].strip()
    if len(root) >= 3 and root not in _GENERIC_NAME_TOKENS:
        keys.add(root)
    return keys


# Words that mean the event did not happen, or has not happened yet. Checked within the *clause*
# that contains the matched needle, because a bare substring match scored "Acme raised no new
# funding" as a real round at 0.85 — the strongest class in the system, so it created an Inbox task
# and could trigger a play, and a rep only discovered otherwise by opening the article.
#
# Deliberately short and high-precision. Every entry here can suppress a true positive, so words
# that merely *co-occur* with real rounds ("expected", "seeking", "aims") are left out: missing a
# real round is as costly as inventing one.
_NEGATION_CUES = frozenset({
    # the event did not happen
    "no", "not", "never", "without", "denies", "denied", "deny",
    "isn't", "isnt", "doesn't", "doesnt", "didn't", "didnt", "won't", "wont",
    "declined", "refused",
    # ...or has not happened yet. Speculation is not a buying signal: a rep cannot congratulate
    # someone on a round they have not raised.
    "rumored", "rumoured", "reportedly", "allegedly", "may", "might", "could",
    "plans", "planning",
})

# Clause boundaries. Negation scopes to a clause, not a headline: "Acme raises $40M Series B with
# no participation from existing investors" is a real round whose "no" belongs to a different
# clause entirely. Splitting here is what keeps the guard from eating true positives.
_CLAUSE_BOUNDARY = re.compile(
    r"[,;:—–]|\s+(?:and|with|but|despite|after|before|that|while|whilst|though|although|because|"
    r"as|since|unless)\s+"
)
_WORD = re.compile(r"[a-z']+")


def _is_negated(clause: str) -> bool:
    """Whether a clause negates or merely speculates about the event it mentions.

    Word-level, not substring: "notable" is not "not", and "nowhere" is not "no".
    """
    return any(w in _NEGATION_CUES for w in _WORD.findall(clause))


def _clause_bounds(low: str) -> list[tuple[int, int]]:
    """Clause ranges over the ORIGINAL string, as (start, end) index pairs.

    Ranges rather than split fragments: some needles contain a connective themselves
    (``"partners with"``), and splitting the text would tear them in half so they never match
    again. The text is matched whole; clauses only scope the negation check.
    """
    bounds: list[tuple[int, int]] = []
    cursor = 0
    for m in _CLAUSE_BOUNDARY.finditer(low):
        if m.start() > cursor:
            bounds.append((cursor, m.start()))
        cursor = m.end()
    bounds.append((cursor, len(low)))
    return bounds


def _classify_news(text: str) -> tuple[str, float]:
    """Classify a headline into (kind, strength). First match wins — the ordering in
    ``_NEWS_PATTERNS`` is deliberate, so a headline that is both a round and an acquisition is a
    funding signal first.

    A needle inside a negated or speculative clause does not count as a match. The headline still
    lands as a weak "news" mention: the information is real even when the event is not, and at 0.4
    it stays on the timeline without creating an Inbox task.
    """
    low = text.lower()
    bounds = _clause_bounds(low)
    for kind, needles, strength in _NEWS_PATTERNS:
        for needle in needles:
            start = low.find(needle)
            while start != -1:
                end = start + len(needle)
                for c_start, c_end in bounds:
                    if c_start <= start < c_end:
                        # Extend to cover the needle itself when it straddles a boundary, so the
                        # words inside a matched phrase are always part of what is examined.
                        if not _is_negated(low[c_start:max(c_end, end)]):
                            return kind, strength
                        break
                start = low.find(needle, start + 1)
    return "news", 0.4


def names_account(text: str, account: Account) -> bool:
    """Whether `text` identifies THIS account, strictly enough for a precision source.

    Stricter than :func:`_account_keys` membership, which accepts any single token. That is fine
    for a broad news query but fails on multi-word names built from common words: the LinkedIn job
    title "Included Health - Member Care Advocate (MCA)" was attributed to *Advocate Health Care*
    because it shares "advocate", "health" and "care". Observed live.

    Accepts three kinds of evidence, all of which are actually distinctive:

    * the **domain root** ("advocatehealth"), which is unique by construction;
    * the **full name as a contiguous phrase**, so word order and adjacency must match;
    * for a **single-token name** (Ramp, Vanta, Stripe), that token — there is nothing stronger
      available, and such names are distinctive precisely because they are one word.
    """
    hay = (text or "").lower()
    if not hay:
        return False

    root = (account.domain or "").lower().split(".")[0].strip()
    if len(root) >= 4 and root in hay:
        return True

    name = (account.name or "").strip().lower()
    if not name:
        return False
    if name in hay:                      # full phrase, adjacency preserved
        return True

    tokens = [
        t for t in re.split(r"[^a-z0-9]+", name)
        if len(t) >= 3 and t not in _GENERIC_NAME_TOKENS
    ]
    # One distinctive word is the whole name: nothing stronger exists, and it is not generic.
    return len(tokens) == 1 and tokens[0] in hay


def event_dedupe_key(kind: str, anchor: str, strength: float, now: datetime) -> str:
    """Event-bucketed dedupe key, NOT per-URL.

    The same funding round gets re-covered by many outlets under fresh URLs for weeks, and per-URL
    keys re-alerted completed accounts on every refresh (observed: 9 distinct "funding" URLs for one
    account in two weeks). One event-class alert per account per window: funding/hiring monthly,
    other news weekly. The URL still lands on the signal itself for the timeline.

    Shared by every search-backed source. Duplicating this was the alternative, and two copies of a
    bucketing rule drift — at which point one source starts re-alerting and the other looks broken.
    """
    if kind in ("funding", "hiring"):
        return f"{kind}:{anchor}:{now:%Y-%m}"
    if kind in ("job_posting", "tech_install"):
        # A req stays open for weeks and an engineering blog post never changes; monthly keeps one
        # alert per hiring push rather than one per refresh.
        return f"{kind}:{anchor}:{now:%Y-%m}"
    # Separate buckets for real events (acquires/partners/launches, >=0.5) vs weak mentions, so a
    # Monday press mention can't shadow a Wednesday acquisition.
    iso = now.isocalendar()
    tier = "evt" if strength >= 0.5 else "mention"
    return f"news:{anchor}:{iso[0]}-W{iso[1]:02d}:{tier}"

# Built-in signal library — the catalogue NEXUS ships with out of the box.
SIGNAL_LIBRARY: dict[str, dict] = {
    "funding": {"category": "3rd-party", "default_strength": 0.8, "desc": "Raised a round"},
    "news": {"category": "3rd-party", "default_strength": 0.4, "desc": "Press / announcement"},
    "job_posting": {"category": "3rd-party", "default_strength": 0.6, "desc": "Relevant hiring"},
    "hiring": {"category": "3rd-party", "default_strength": 0.5, "desc": "Headcount growth"},
    "g2_intent": {"category": "3rd-party", "default_strength": 0.9, "desc": "G2 category intent"},
    "tech_install": {"category": "3rd-party", "default_strength": 0.6, "desc": "Adopted a tech"},
    "job_switch": {"category": "3rd-party", "default_strength": 0.8, "desc": "Champion moved"},
    "web_visit": {"category": "1st-party", "default_strength": 0.7, "desc": "Visited our site"},
    "product_usage": {"category": "1st-party", "default_strength": 0.8, "desc": "Used product"},
    "call": {"category": "1st-party", "default_strength": 0.6, "desc": "Call / meeting"},
    "website_change": {"category": "3rd-party", "default_strength": 0.6,
                       "desc": "Watched page changed"},
}


@dataclass(slots=True)
class RawSignal:
    kind: str
    source: str
    title: str
    dedupe_key: str
    body: str | None = None
    url: str | None = None
    strength: float | None = None  # falls back to library default
    occurred_at: datetime = field(default_factory=utcnow)
    contact_id: str | None = None

    def resolved_strength(self) -> float:
        if self.strength is not None:
            return self.strength
        return SIGNAL_LIBRARY.get(self.kind, {}).get("default_strength", 0.5)


class SignalSource(abc.ABC):
    name: str

    @abc.abstractmethod
    async def fetch(self, account: Account) -> list[RawSignal]: ...


class DemoSignalSource(SignalSource):
    """**Offline test double.** Deterministic synthetic signals, for tests and local demos only.

    Mirrors ``nexus/network/connectors/fixture.py``: importable, injectable, and never selectable in
    a live deployment. Two independent guards, because this fabricates events that a rep would
    otherwise act on — someone would call an account about a funding round that never happened.

    * ``demo_signals_active`` is hard-false in staging/prod regardless of the flag.
    * ``Settings._reject_synthetic_signals_in_production`` refuses to start at all if
      ``NEXUS_SIGNAL_SOURCES`` names ``demo`` there.

    The second exists because the first is silent: an operator who asked for demo signals and got
    none has no way to tell that from a broken pipeline. Until M16 this was the *default* source, so
    an out-of-the-box deployment scored and alerted on fabricated events.
    """

    name = "demo"

    async def fetch(self, account: Account) -> list[RawSignal]:
        out: list[RawSignal] = []
        domain = account.domain or account.name.lower().replace(" ", "")
        if (account.employee_count or 0) >= 100:
            out.append(
                RawSignal(
                    kind="funding",
                    source=self.name,
                    title=f"{account.name} announced new funding",
                    dedupe_key=f"funding:{domain}",
                    strength=0.8,
                )
            )
        out.append(
            RawSignal(
                kind="job_posting",
                source=self.name,
                title=f"{account.name} is hiring in a relevant function",
                dedupe_key=f"job_posting:{domain}",
            )
        )
        return out


def _parse_feed(xml_text: str) -> list[dict]:
    """Parse an RSS or Atom feed into ``[{title, link, summary}]``. Namespace-tolerant, never
    raises. Handles RSS ``<item>`` (link is text) and Atom ``<entry>`` (link is an href attr)."""
    import xml.etree.ElementTree as ET

    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    items: list[dict] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        d: dict[str, str] = {}
        for child in el:
            lt = _local(child.tag)
            if lt == "title" and child.text:
                d["title"] = child.text.strip()
            elif lt == "link":
                href = child.get("href")
                link = (href or child.text or "").strip()
                if link and "link" not in d:
                    d["link"] = link
            elif lt in ("description", "summary", "content") and child.text and "summary" not in d:
                d["summary"] = child.text.strip()
        if d.get("title"):
            items.append(d)
    return items


class RssSignalSource(SignalSource):
    """Live source: read a company's own RSS/Atom feed (blog / newsroom / press releases).

    The feed URL comes from ``account.custom_fields['rss_feed']`` when set, else common conventions
    on the account domain. Entries are the company's *own* posts, so — unlike open-web search —
    they need no name-matching. OFF by default: activated by adding ``rss`` to
    ``NEXUS_SIGNAL_SOURCES``. The HTTP fetch is injectable so the parser runs offline with canned
    feed XML. Never raises across the boundary.
    """

    name = "rss"

    def __init__(self, fetch=None, *, max_items: int = 5) -> None:
        self._fetch = fetch  # async (url) -> str | None ; None = real httpx
        self._max = max_items

    async def _http_get(self, url: str) -> str | None:
        if self._fetch is not None:
            return await self._fetch(url)
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "NexusGTM/1.0 (+signals)"})
                if resp.status_code == 200 and resp.text:
                    return resp.text
        except Exception:  # a flaky feed host must never break ingestion
            return None
        return None

    def _feed_urls(self, account: Account) -> list[str]:
        cf = getattr(account, "custom_fields", None) or {}
        explicit = str(cf.get("rss_feed") or "").strip()
        if explicit:
            return [explicit]
        domain = (account.domain or "").strip().lower().lstrip("@")
        if not domain:
            return []
        return [
            f"https://{domain}/feed",
            f"https://{domain}/rss",
            f"https://{domain}/blog/rss.xml",
            f"https://{domain}/news/rss",
        ]

    async def fetch(self, account: Account) -> list[RawSignal]:
        anchor = (account.domain or account.name or "").strip().lower().replace(" ", "")
        for url in self._feed_urls(account):
            xml = await self._http_get(url)
            if not xml:
                continue
            items = _parse_feed(xml)
            if not items:
                continue
            out: list[RawSignal] = []
            for it in items[: self._max]:
                title = (it.get("title") or "").strip()
                if not title:
                    continue
                link = (it.get("link") or "").strip()
                summary = (it.get("summary") or "").strip()
                kind, strength = _classify_news(f"{title} {summary}")
                out.append(
                    RawSignal(
                        kind=kind,
                        source=self.name,
                        title=title[:380],
                        body=summary or None,
                        url=link or None,
                        strength=strength,
                        dedupe_key=f"rss:{anchor}:{link or title}"[:200],
                    )
                )
            if out:
                return out
        return []


class WebNewsSource(SignalSource):
    """Live source: searches the web for recent news about the account."""

    name = "web_news"

    def __init__(self, browser):
        self.browser = browser

    async def fetch(self, account: Account) -> list[RawSignal]:
        name = (account.name or "").strip()
        if not name:
            return []
        keys = _account_keys(account)
        if not keys:
            return []
        # Bias the query toward buying-event news, and include the industry so a generic name
        # ("Bill", "Increase") doesn't pull unrelated results.
        industry = (account.industry or "").strip()
        query = f'{name} {industry} (funding OR hiring OR launches OR partnership OR acquisition)'
        try:
            hits = await self.browser.search(query.strip(), limit=6) or []
        except Exception:  # a flaky search must not break ingestion
            hits = []

        out: list[RawSignal] = []
        seen: set[str] = set()
        now = utcnow()
        anchor = (account.domain or account.name or "").strip().lower().replace(" ", "")
        for h in hits:
            title = (h.get("title") or "").strip()
            snippet = (h.get("snippet") or "").strip()
            url = h.get("url") or ""
            hay = f"{title} {snippet}".lower()
            # The result must actually NAME this account, else it's a generic news-site index
            # ("Banking News and Analysis | Banking Dive") — the exact junk that polluted the inbox.
            if not any(k in hay for k in keys):
                continue
            if not title:
                continue
            kind, strength = _classify_news(f"{title} {snippet}")
            dedupe_key = event_dedupe_key(kind, anchor, strength, now)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            out.append(
                RawSignal(
                    kind=kind,
                    source=self.name,
                    title=title[:380],
                    body=snippet or None,
                    url=url,
                    strength=strength,
                    dedupe_key=dedupe_key,
                )
            )
        return out[:4]


class DorkedSearchSource(SignalSource):
    """Live source: one high-precision search per signal kind (see ``nexus/ingestion/dorks.py``).

    Runs *alongside* ``WebNewsSource`` rather than replacing it. They fail differently and that is
    the point: the broad query catches an event nobody indexed under a recognisable phrase, and the
    dorks catch the ones a single relevance-ranked result set crowds out. Where both find the same
    event they collapse into one signal, because both use ``event_dedupe_key``.

    Three things it does that a broad query cannot:

    * **Budgets each signal kind separately**, so a funding round is never displaced by three press
      mentions competing for the same six slots.
    * **Prefers recent results.** Goes through ``search_recent``, so a keyed Exa deployment gets a
      real published-date floor; every other provider falls back to plain search and relies on the
      dork's lexical year constraint. Old news that outranks new news on link authority is the
      normal failure of relevance-ranked search, not an edge case.
    * **Trusts provenance over headlines** where provenance is stronger. A hit on an ATS board is a
      job posting whatever its title says, and a post on the company's own domain is about that
      company even when the snippet never names it — so those skip the name-match gate that the
      open-web dorks still need.

    Never raises across the boundary; a dead search provider yields no signals, not an exception.
    """

    name = "dork"

    def __init__(self, search=None, *, max_queries: int = 4, per_query: int = 4,
                 recency_days: int = 120, max_signals: int = 6, pace_s: float = 0.0) -> None:
        # Injectable so the whole thing runs offline against canned hits.
        self._search = search
        # The budget that matters: each dork is one billed search call, so this multiplies the cost
        # of every account refresh. Four is enough for funding + hiring + exec + one event class.
        self._max_queries = max_queries
        self._per_query = per_query
        self._recency_days = recency_days
        self._max_signals = max_signals
        # Seconds between queries. The keyless DuckDuckGo backend scrapes an HTML endpoint and
        # starts returning 403 after roughly ten rapid requests (measured), which a single account
        # refresh can reach on its own. Spacing them is the difference between a working keyless
        # deployment and one that silently reports "no signals" for every account. 0 for keyed
        # engines, which are rate-limited by contract rather than by anti-bot heuristics.
        self._pace_s = pace_s
        # Own budget, honoured by IngestionService. This source makes `max_queries` requests and
        # sleeps between them; the shared 8s default assumes a single request, so it would kill
        # this source mid-run on every account — and a killed source reports nothing, which looks
        # exactly like "this account has no signals".
        self.timeout_s = max(12.0, (max_queries * 4.0) + (max(0, max_queries - 1) * pace_s))
        # Read by IngestionService after each fetch and stored on the crawl-history row.
        self.last_provenance: dict = {}

    def _provider(self):
        """The backend this source searches with.

        ``NEXUS_SIGNAL_SEARCH_PROVIDER`` selects one for signals ALONE; empty means "whatever the
        rest of the app uses", so the default is unchanged. The separation matters because
        ``search_provider`` is global and several features depend on capabilities only Exa
        implements — ``find_similar`` (lookalikes) and ``search_companies`` (company discovery, ICP
        auto-discovery). Diversifying signal collection by repointing the global setting would take
        those down silently: the base ``find_similar`` returns ``[]``, so lookalikes would report
        "no results" with nothing in the logs to explain why.
        """
        if self._search is None:
            from nexus.core.config import get_settings
            from nexus.integrations.search.provider import (
                build_search_provider,
                get_search_provider,
            )

            choice = (get_settings().signal_search_provider or "").strip()
            # build_search_provider, not the global singleton: this must not replace the provider
            # the rest of the app resolved.
            self._search = build_search_provider(choice) if choice else get_search_provider()
        return self._search

    async def _run(self, query: str, include: tuple, exclude: tuple) -> list[dict] | None:
        """Hits for one dork, or None if the provider itself failed.

        None is not the same as no results, and the caller treats it differently: the keyless
        DuckDuckGo backend starts returning 403 after roughly ten rapid queries, and continuing to
        fire the rest of the batch only deepens the block while returning nothing.
        """
        provider = self._provider()
        try:
            hits = await provider.search_recent(
                query,
                limit=self._per_query,
                days=self._recency_days,
                include_domains=include,
                exclude_domains=exclude,
            )
        except TypeError:
            # An injected double or an older provider without the structured-domain kwargs. The
            # keyword dialect already carries the domains inline, so this loses nothing there.
            try:
                hits = await provider.search_recent(
                    query, limit=self._per_query, days=self._recency_days
                )
            except Exception:
                logger.warning("dork search failed: %s", query, exc_info=True)
                return None
        except Exception:
            logger.warning("dork search failed: %s", query, exc_info=True)
            return None
        out = []
        for h in hits or []:
            # SearchHit dataclass or a plain dict, depending on the seam an injected double uses.
            out.append(h.as_dict() if hasattr(h, "as_dict") else dict(h))
        return out

    async def fetch(self, account: Account) -> list[RawSignal]:
        from nexus.ingestion.dorks import select_dorks

        name = (account.name or "").strip()
        if not name:
            return []
        domain = (account.domain or "").strip().lower().lstrip("@")
        now = utcnow()
        anchor = (account.domain or account.name or "").strip().lower().replace(" ", "")

        provider = self._provider()
        # "plain" when a provider has not declared support: over-claiming costs every result,
        # under-claiming costs only some precision.
        dialect = getattr(provider, "query_dialect", "plain")
        # Provenance for the crawl-history row. Without the rendered queries, "why did this find
        # nothing?" is unanswerable after the fact — the query depends on the account, the date and
        # the provider's dialect, none of which are recoverable from the result.
        self.last_provenance = {
            "provider": getattr(provider, "name", "unknown"),
            "dialect": dialect,
            "queries": [],
        }
        out: list[RawSignal] = []
        seen: set[str] = set()
        for index, dork in enumerate(select_dorks(has_domain=bool(domain), limit=self._max_queries)):
            if index and self._pace_s > 0:
                import asyncio

                await asyncio.sleep(self._pace_s)
            query = dork.render(
                name=name, domain=domain, industry=(account.industry or "").strip(), now=now,
                dialect=dialect,
            )
            include, exclude = dork.domains(domain=domain, dialect=dialect)
            self.last_provenance["queries"].append({"dork": dork.slug, "query": query})
            hits = await self._run(query, include, exclude)
            if hits is None:
                self.last_provenance["queries"][-1]["failed"] = True
                # The provider failed, not the query. Stop: the rest of the batch would fail too,
                # and on a rate-limited backend each extra request extends the block.
                break
            for hit in hits:
                signal = self._to_signal(dork, hit, account=account, anchor=anchor, now=now,
                                         seen=seen)
                if signal is not None:
                    out.append(signal)
                    if len(out) >= self._max_signals:
                        return out
        return out

    def _to_signal(self, dork, hit: dict, *, account: Account, anchor: str,
                   now: datetime, seen: set[str]) -> RawSignal | None:
        title = (hit.get("title") or "").strip()
        if not title:
            return None
        snippet = (hit.get("snippet") or "").strip()
        url = (hit.get("url") or "").strip()
        hay = f"{title} {snippet}".lower()

        # URL shape first: it is the cheapest and most certain filter. A LinkedIn *jobs* dork also
        # surfaces company pages — `linkedin.com/company/vantaesports` passed the name gate for an
        # unrelated esports org — and no amount of text analysis fixes a result that is the wrong
        # kind of page.
        if dork.require_url and dork.require_url not in url.lower():
            return None

        if not dork.self_evident:
            # The name must be in the TITLE, not merely somewhere in the page.
            #
            # Matching on title+snippet (as WebNewsSource does) let an industry round-up through:
            # "Cybersecurity Startup Investors Pulled Back In Q3" scored funding 0.90 for Vanta
            # because the article mentions them in passing. A story that is genuinely *about* a
            # company's funding round names the company in the headline; a market survey that
            # happens to list it does not. Observed against live Firecrawl results.
            if not names_account(title, account):
                return None
            # And it must carry the vocabulary of the event, or a company page that merely ranks for
            # the name passes as a funding round.
            if dork.require_any and not any(t in hay for t in dork.require_any):
                return None

        if dork.trust_kind:
            kind, strength = dork.kind, dork.strength
        else:
            # Classify on the TITLE alone, not title+snippet.
            #
            # A headline states what happened; a page body mentions everything the company has ever
            # done. Classifying on both scored "Vanta Delivers: Vanta control framework" as funding
            # 0.85 — a product page whose text recalls an earlier round. The snippet still feeds
            # `require_any` above, because a relevance floor and an event determination are
            # different questions. Observed against live results.
            kind, strength = _classify_news(title)
            # Take the dork's strength only when it agrees on the kind — otherwise a "funding" dork
            # that surfaced a partnership would report 0.9 for a 0.6 event.
            if kind == dork.kind:
                strength = max(strength, dork.strength)

        dedupe_key = event_dedupe_key(kind, anchor, strength, now)
        if dedupe_key in seen:
            return None
        seen.add(dedupe_key)
        return RawSignal(
            kind=kind,
            source=self.name,
            title=title[:380],
            body=snippet or None,
            url=url or None,
            strength=strength,
            dedupe_key=dedupe_key,
        )


class AtsSignalSource(SignalSource):
    """Live source: the account's own ATS job board (``nexus/ingestion/ats.py``).

    The strongest hiring evidence available and entirely keyless — Greenhouse, Lever and Ashby serve
    public JSON. Discovery reads the board token off the company's careers page; see the ats module
    for why guessing it does not work.

    **Aggregated, not one signal per requisition.** Vanta has 100 open roles and Stripe 542; emitting
    one signal each would bury every other signal in the inbox and tell a rep nothing they could act
    on. One signal per account per month carries the count, the departments hiring, and a sample of
    titles — which is the thing a rep actually opens with.

    **Growth is computed from the crawl history**, not guessed: the previous run's ``items_found``
    for this account is already recorded in ``signal_source_runs`` (M16), so "60 → 100 open roles" is
    a real comparison rather than a heuristic. A first sighting has nothing to compare against and
    says so instead of inventing a baseline.
    """

    name = "ats"

    # Discovery costs several requests; a board fetch is one. Both are slow relative to the shared
    # 8s budget, and a source killed mid-run reports nothing.
    timeout_s = 25.0

    def __init__(self, *, fetch=None, max_titles: int = 6, session=None) -> None:
        self._fetch = fetch          # injectable transport, so the whole path runs offline
        self._max_titles = max_titles
        self._session = session      # for the growth lookup; None resolves per call
        self.last_provenance: dict = {}

    async def fetch(self, account: Account) -> list[RawSignal]:
        from nexus.ingestion.ats import BoardRef, resolve_and_fetch

        domain = (account.domain or "").strip().lower()
        if not domain:
            return []

        cached = (getattr(account, "custom_fields", None) or {}).get("ats_board") or {}
        configured = None
        if cached.get("provider") and cached.get("token"):
            configured = BoardRef(
                provider=str(cached["provider"]), token=str(cached["token"]), via="configured"
            )

        result = await resolve_and_fetch(
            domain, account_name=account.name or "", configured=configured, fetch=self._fetch
        )
        self.last_provenance = {
            "provider": result.provider,
            "token": result.token,
            "outcome": result.outcome,
            "via": result.ref.via if result.ref else "",
            "postings": len(result.postings),
        }
        # Cache the discovery so the careers-page crawl happens once per account, not per refresh.
        if result.ref is not None and hasattr(account, "custom_fields"):
            cf = dict(account.custom_fields or {})
            cf["ats_board"] = {"provider": result.ref.provider, "token": result.ref.token}
            account.custom_fields = cf

        if result.outcome != "ok" or not result.postings:
            # `empty` (board exists, nothing open) is real information, but it is not an event —
            # there is nothing for a rep to act on, and a signal saying "they are not hiring" every
            # month would be noise. The run row records it either way.
            return []

        return [self._to_signal(account, result)]

    def _to_signal(self, account, result) -> RawSignal:
        from collections import Counter

        postings = result.postings
        departments = Counter(p.department for p in postings if p.department)
        top = ", ".join(f"{d} ({n})" for d, n in departments.most_common(4))
        titles = "; ".join(p.title for p in postings[: self._max_titles] if p.title)

        body = f"{len(postings)} open roles on {result.provider}."
        if top:
            body += f" Hiring in: {top}."
        if titles:
            body += f" Recent: {titles}."

        anchor = (account.domain or account.name or "").strip().lower().replace(" ", "")
        now = utcnow()
        # Strength rises with volume because a company with 100 open reqs is a materially different
        # prospect from one with 3 — but stays under a funding round, which is a sharper event.
        strength = 0.55 if len(postings) < 10 else (0.65 if len(postings) < 50 else 0.75)
        return RawSignal(
            kind="hiring",
            source=self.name,
            title=f"{account.name} has {len(postings)} open roles",
            body=body,
            url=postings[0].url or "",
            strength=strength,
            dedupe_key=event_dedupe_key("hiring", anchor, strength, now),
        )


class PublicApiSignalSource(SignalSource):
    """Live source: SEC EDGAR, GitHub and Hacker News (``nexus/ingestion/public_apis.py``).

    All three are keyless and none is scraped. They are grouped into one source because they share
    the property that makes them dangerous: each is a **global namespace searched by name**, so the
    binding risk — attributing a stranger's data to this account — is identical, and the guard
    belongs in one place. Measured before the guards existed: EDGAR gave Stripe a filing by
    "DCP STRIPE XXII", and Hacker News gave Vanta a story about Vanta.js, a 3D-graphics library.

    Each sub-source degrades independently: GitHub running out of its 60/hour budget must not stop
    EDGAR from reporting a 10-Q.
    """

    name = "public_api"
    timeout_s = 30.0

    def __init__(self, *, fetch=None, github_token: str = "") -> None:
        self._fetch = fetch
        self._github_token = github_token
        self.last_provenance: dict = {}

    async def fetch(self, account: Account) -> list[RawSignal]:
        from nexus.ingestion.public_apis import (
            edgar_filings,
            github_activity,
            github_org_matches,
            hn_stories,
        )

        name = (account.name or "").strip()
        domain = (account.domain or "").strip().lower()
        if not name:
            return []
        anchor = (domain or name).lower().replace(" ", "")
        now = utcnow()
        out: list[RawSignal] = []
        prov: dict = {}

        filings = await edgar_filings(name, domain=domain, fetch=self._fetch)
        prov["edgar"] = filings.outcome
        if filings.outcome == "ok":
            top = filings.items[0]
            out.append(RawSignal(
                kind="news", source=self.name,
                title=f"{name} filed a {top['form']} with the SEC",
                body=", ".join(f"{i['form']} on {i['filed_at']}" for i in filings.items[:4]),
                url=top.get("url") or "",
                # An 8-K is "something material happened" and is worth a look; it is not the sharp,
                # specific event a funding headline is.
                strength=0.6,
                dedupe_key=f"sec:{anchor}:{top['form']}:{top['filed_at']}",
            ))

        stories = await hn_stories(name, domain=domain, fetch=self._fetch)
        prov["hn"] = stories.outcome
        if stories.outcome == "ok":
            top = max(stories.items, key=lambda s: s["points"])
            out.append(RawSignal(
                kind="news", source=self.name,
                title=f"{top['title']} ({top['points']} points on Hacker News)",
                url=top["url"], strength=0.5,
                dedupe_key=event_dedupe_key("news", anchor, 0.5, now),
            ))

        # GitHub last: it has the tightest budget, so when the hour's quota is gone the other two
        # have already produced their signals.
        if domain:
            org = domain.split(".")[0]
            if await github_org_matches(
                org, name=name, domain=domain, fetch=self._fetch
            ):
                repos = await github_activity(org, fetch=self._fetch,
                                              token=self._github_token)
                prov["github"] = repos.outcome
                if repos.outcome == "ok":
                    langs = sorted({r["language"] for r in repos.items if r["language"]})
                    if langs:
                        out.append(RawSignal(
                            kind="tech_install", source=self.name,
                            title=f"{name} is building in {', '.join(langs[:4])}",
                            body="Active public repos: "
                                 + ", ".join(f"{r['name']} ({r['stars']}★)"
                                             for r in repos.items[:4]),
                            url=repos.items[0].get("url") or "",
                            strength=0.55,
                            dedupe_key=f"tech:{anchor}:{now:%Y-%m}",
                        ))
            else:
                # Not an error: most companies' GitHub org is not their domain root, and an
                # unverified slug is exactly how another org's repos would be attributed here.
                prov["github"] = "unverified"

        self.last_provenance = prov
        return out


class WebsiteWatchSignalSource(SignalSource):
    """Live source: watch a company's own pages and report when they change (M20).

    Entirely ours — no third party, no key, no scraping of anything non-public. The value is that a
    pricing change is a *strategy* change, and it is the one thing a rep can open a conversation
    about the same week it happens.

    **Unlike every other source, this one needs the database.** A change is only meaningful against
    a stored baseline, so ``fetch`` takes the tenant session through ``bind_session`` rather than
    being a pure function of the account. The first sighting of a page establishes the baseline and
    deliberately emits nothing — "this company has a pricing page" is not news.

    Normalisation is the load-bearing part; see ``nexus/ingestion/webwatch.py`` for why raw HTML
    hashing reports a change on every single run.
    """

    name = "website"
    # Up to four pages, each possibly trying two or three paths.
    timeout_s = 30.0

    def __init__(self, *, fetch=None, page_kinds: tuple[str, ...] | None = None) -> None:
        self._fetch = fetch
        self._kinds = page_kinds
        self._ts = None
        self.last_provenance: dict = {}

    def bind_session(self, ts):
        """Give the source the tenant session for this run. Called by IngestionService."""
        self._ts = ts
        return self

    async def fetch(self, account: Account) -> list[RawSignal]:
        from nexus.ingestion.webwatch import WATCHED_PAGES, check_page, summarise_change
        from nexus.models.page_snapshot import PageSnapshot

        domain = (account.domain or "").strip().lower().lstrip("@")
        if not domain or self._ts is None:
            # No session means no baseline, and without a baseline every page looks new. Emitting
            # then would announce a "change" for every page of every account on first run.
            return []

        ts = self._ts
        now = utcnow()
        out: list[RawSignal] = []
        prov: dict = {}
        pages = [p for p in WATCHED_PAGES if self._kinds is None or p[0] in self._kinds]

        for page_kind, paths in pages:
            check = await check_page(domain, page_kind, paths, fetch=self._fetch)
            prov[page_kind] = check.outcome
            if check.outcome != "ok":
                continue

            snapshot = await ts.first(
                PageSnapshot,
                PageSnapshot.account_id == account.id,
                PageSnapshot.page_kind == page_kind,
            )
            if snapshot is None:
                ts.add(PageSnapshot(
                    tenant_id=ts.tenant_id, account_id=account.id, page_kind=page_kind,
                    url=check.url, content_hash=check.digest, content=check.text,
                    last_checked_at=now,
                ))
                prov[page_kind] = "baseline"
                continue

            snapshot.last_checked_at = now
            if snapshot.content_hash == check.digest:
                continue

            summary = summarise_change(snapshot.content, check.text)
            snapshot.content_hash = check.digest
            snapshot.content = check.text
            snapshot.url = check.url
            snapshot.last_change_summary = summary
            snapshot.change_count = (snapshot.change_count or 0) + 1
            snapshot.last_changed_at = now
            prov[page_kind] = "changed"

            out.append(RawSignal(
                kind="website_change", source=self.name,
                title=f"{account.name} changed its {page_kind} page",
                body=summary,
                url=check.url,
                # Pricing outranks the rest: it is a commercial decision, not a copy edit.
                strength=0.75 if page_kind == "pricing" else 0.55,
                # Keyed on the page kind and the digest, so re-running the sweep does not re-alert,
                # but a genuinely new change does.
                dedupe_key=f"web:{page_kind}:{domain}:{check.digest[:16]}",
            ))

        await ts.flush()
        self.last_provenance = prov
        return out
