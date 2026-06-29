# nexus/integrations/registry.py
"""The DataSourceRegistry — one composition spine in front of every external-data capability.

Discovery, enrichment and research talk to the registry, never to a provider directly. For each
capability it holds an ordered list of providers (priority waterfall: InfoJoy -> web search ->
Apify) and applies three cross-cutting policies uniformly:

  * **Budgets** — a per-source call ceiling so one source can't run away on cost.
  * **Circuit breakers** — after N consecutive failures a source is skipped for the rest of the
    process, so a flaky/expensive source degrades instead of retrying forever.
  * **Caching** — results are memoized by ``(capability, normalized query)`` within the registry's
    lifetime, so the same ICP asked twice in a run hits the network once.

Results from multiple sources are merged with per-field provenance (:mod:`nexus.integrations.
provenance`): the highest-priority source anchors a record and lower-priority sources fill only
the gaps. Every provider call is isolated — a raising provider is logged and skipped, never
surfaced. Offline this all resolves to stubs that return empty, so CI stays deterministic and
zero-network.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from nexus.integrations.company_search import (
    CompanyCandidate,
    CompanySearchProvider,
    SearchBackedCompanySearchProvider,
    StubCompanySearchProvider,
)
from nexus.integrations.contact_search import (
    ContactCandidate,
    ContactSearchProvider,
    StubContactSearchProvider,
)
from nexus.integrations.search import (
    SearchHit,
    SearchProvider,
    StubSearchProvider,
    build_search_provider,
)
from nexus.research import ResearchProfile, ResearchProvider, build_research_provider
from nexus.verification import (
    EmailVerification,
    EmailVerificationProvider,
    build_email_verifier,
)

logger = logging.getLogger("nexus.integrations.registry")


@dataclass
class _SourceState:
    """Mutable budget + circuit-breaker state for one named source."""

    calls: int = 0
    consecutive_failures: int = 0
    open: bool = False  # True once the breaker has tripped


class _Policy:
    """Enforces per-source budgets and circuit breakers around isolated provider calls."""

    def __init__(self, *, per_source_budget: int, breaker_threshold: int):
        self.per_source_budget = per_source_budget
        self.breaker_threshold = breaker_threshold
        self._state: dict[str, _SourceState] = {}

    def _get(self, name: str) -> _SourceState:
        st = self._state.get(name)
        if st is None:
            st = self._state[name] = _SourceState()
        return st

    def allow(self, name: str) -> bool:
        st = self._get(name)
        if st.open:
            return False
        if self.per_source_budget and st.calls >= self.per_source_budget:
            logger.info("source %s over budget (%d calls); skipping", name, st.calls)
            return False
        return True

    async def call(self, name: str, coro_factory):
        """Run one provider call under budget + breaker policy. Returns None on skip/failure."""
        if not self.allow(name):
            return None
        st = self._get(name)
        st.calls += 1
        try:
            result = await coro_factory()
        except Exception as exc:  # provider isolation
            st.consecutive_failures += 1
            if st.consecutive_failures >= self.breaker_threshold:
                st.open = True
                logger.warning("circuit breaker OPEN for source %s after %d failures",
                               name, st.consecutive_failures)
            logger.warning("source %s call failed: %r", name, exc)
            return None
        st.consecutive_failures = 0
        return result

    def snapshot(self) -> dict:
        return {n: {"calls": s.calls, "failures": s.consecutive_failures, "open": s.open}
                for n, s in self._state.items()}


def _norm_key(*parts) -> str:
    """Stable cache key from arbitrary JSON-able parts."""
    try:
        return json.dumps(parts, sort_keys=True, default=str)
    except TypeError:
        return repr(parts)


def _merge_company(anchor: CompanyCandidate, incoming: CompanyCandidate) -> None:
    """Fill the anchor's empty fields from a lower-priority candidate, recording provenance."""
    for f in ("url", "industry", "country", "employee_count", "snippet"):
        cur = getattr(anchor, f)
        new = getattr(incoming, f)
        if (cur is None or cur == "") and new not in (None, ""):
            setattr(anchor, f, new)
            anchor.provenance[f] = incoming.source
    # Confidence is the best any source expressed for this company.
    anchor.confidence = max(anchor.confidence, incoming.confidence)


class DataSourceRegistry:
    def __init__(
        self,
        *,
        company_search: list[CompanySearchProvider] | None = None,
        search: SearchProvider | None = None,
        research: ResearchProvider | None = None,
        email_verify: EmailVerificationProvider | None = None,
        contact_search: list[ContactSearchProvider] | None = None,
        per_source_budget: int = 64,
        breaker_threshold: int = 3,
        cache_enabled: bool = True,
    ):
        self.company_search_providers = company_search or [StubCompanySearchProvider()]
        self.search_provider = search or StubSearchProvider()
        self.research_provider = research
        self.email_verifier = email_verify
        self.contact_search_providers = contact_search or [StubContactSearchProvider()]
        self._policy = _Policy(per_source_budget=per_source_budget,
                               breaker_threshold=breaker_threshold)
        self._cache_enabled = cache_enabled
        self._cache: dict[str, object] = {}

    # ------------------------------------------------------------------ search
    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        key = _norm_key("search", self.search_provider.name, query, limit)
        if self._cache_enabled and key in self._cache:
            return list(self._cache[key])  # type: ignore[arg-type]
        hits = await self._policy.call(
            self.search_provider.name,
            lambda: self.search_provider.search(query, limit=limit),
        ) or []
        if self._cache_enabled:
            self._cache[key] = list(hits)
        return hits

    async def find_similar(self, url: str, *, limit: int = 10) -> list[SearchHit]:
        """Pages similar to a seed URL (lookalike discovery). Empty when unsupported."""
        if not url:
            return []
        key = _norm_key("find_similar", self.search_provider.name, url, limit)
        if self._cache_enabled and key in self._cache:
            return list(self._cache[key])  # type: ignore[arg-type]
        hits = await self._policy.call(
            self.search_provider.name,
            lambda: self.search_provider.find_similar(url, limit=limit),
        ) or []
        if self._cache_enabled:
            self._cache[key] = list(hits)
        return hits

    # ---------------------------------------------------------- company_search
    async def company_search(
        self, icp: dict, *, limit: int = 25, exclude_domains: list[str] | None = None
    ) -> list[CompanyCandidate]:
        """Waterfall every company source in priority order, merging by normalized domain.

        ``exclude_domains`` is forwarded to each provider (e.g. Exa ``excludeDomains``) so a caller
        that already tracks some companies reaches NEW ones on each run. It's part of the cache key
        so a different exclusion set isn't served a stale, pre-exclusion result.
        """
        # Sorted so the key is stable regardless of caller ordering.
        excl = sorted({d for d in (exclude_domains or []) if d}) or None
        key = _norm_key("company_search", icp, limit, excl)
        if self._cache_enabled and key in self._cache:
            return list(self._cache[key])  # type: ignore[arg-type]

        merged: dict[str, CompanyCandidate] = {}
        order: list[str] = []
        for provider in self.company_search_providers:
            found = await self._policy.call(
                provider.name,
                lambda p=provider: p.search(icp, limit=limit, exclude_domains=excl),
            )
            for cand in found or []:
                domain = (cand.domain or "").lower()
                if not domain:
                    continue
                if domain not in merged:
                    # Anchor: record provenance for every populated field.
                    cand.provenance = {
                        f: cand.source
                        for f in ("name", "url", "industry", "country",
                                  "employee_count", "snippet")
                        if getattr(cand, f) not in (None, "")
                    }
                    merged[domain] = cand
                    order.append(domain)
                else:
                    _merge_company(merged[domain], cand)

        result = [merged[d] for d in order][:limit]
        if self._cache_enabled:
            self._cache[key] = list(result)
        return result

    # ---------------------------------------------------------------- research
    async def research(self, *, company: str | None = None,
                       domain: str | None = None) -> ResearchProfile:
        if self.research_provider is None:
            return ResearchProfile()
        key = _norm_key("research", self.research_provider.name, company, domain)
        if self._cache_enabled and key in self._cache:
            return self._cache[key]  # type: ignore[return-value]
        profile = await self._policy.call(
            self.research_provider.name,
            lambda: self.research_provider.research(company=company, domain=domain),
        ) or ResearchProfile()
        if self._cache_enabled:
            self._cache[key] = profile
        return profile

    # ------------------------------------------------------------ verify_email
    async def verify_email(self, email: str) -> EmailVerification:
        if self.email_verifier is None:
            return EmailVerification(email=email)
        key = _norm_key("verify_email", self.email_verifier.name, email)
        if self._cache_enabled and key in self._cache:
            return self._cache[key]  # type: ignore[return-value]
        verdict = await self._policy.call(
            self.email_verifier.name, lambda: self.email_verifier.verify_one(email)
        ) or EmailVerification(email=email)
        if self._cache_enabled:
            self._cache[key] = verdict
        return verdict

    # ----------------------------------------------------------- contact_search
    async def contact_search(
        self, account, icp: dict, *, limit: int = 3
    ) -> list[ContactCandidate]:
        """Waterfall the configured contact providers, deduping by (full_name, title)."""
        key = _norm_key("contact_search", account.id, icp, limit)
        if self._cache_enabled and key in self._cache:
            return list(self._cache[key])  # type: ignore[arg-type]

        merged: dict[tuple, ContactCandidate] = {}
        order: list[tuple] = []
        for provider in self.contact_search_providers:
            found = await self._policy.call(
                provider.name,
                lambda p=provider: p.search(account, icp, limit=limit),
            )
            for cand in found or []:
                ck = ((cand.full_name or "").lower(), (cand.title or "").lower())
                if ck not in merged:
                    merged[ck] = cand
                    order.append(ck)
        result = [merged[k] for k in order][:limit]
        if self._cache_enabled:
            self._cache[key] = list(result)
        return result

    # -------------------------------------------------------------- diagnostics
    def source_health(self) -> dict:
        """Budget / breaker state per source — surfaced for observability."""
        return self._policy.snapshot()


def _build_company_search(sources: list[str], *, search: SearchProvider,
                          browser=None) -> list[CompanySearchProvider]:
    providers: list[CompanySearchProvider] = []
    for token in sources:
        key = token.strip().lower()
        if not key:
            continue
        if key == "stub":
            providers.append(StubCompanySearchProvider())
        elif key == "search":
            providers.append(SearchBackedCompanySearchProvider(search))
        else:
            # InfoJoy / Apify adapters land here later; until then keep the slot honest
            # by skipping unknown tokens rather than silently substituting another source.
            logger.warning("unknown company_search source %r; skipping", token)
    if not providers:
        providers.append(StubCompanySearchProvider())
    return providers


def _build_contact_search(sources: list[str], *, search=None) -> list[ContactSearchProvider]:
    from nexus.integrations.contact_search import SearchBackedContactSearchProvider

    providers: list[ContactSearchProvider] = []
    for token in sources:
        key = token.strip().lower()
        if not key:
            continue
        if key == "stub":
            providers.append(StubContactSearchProvider())
        elif key == "search" and search is not None:
            # Real people via web search (Exa) + LLM name extraction; lazy LLM so a missing
            # key just degrades to no extracted names (the stub committee then fills in).
            from nexus.agents.llm import get_llm_provider

            providers.append(SearchBackedContactSearchProvider(search, get_llm_provider()))
        else:
            # Apollo / InfoJoy / ZoomInfo adapters land here later; skip unknown tokens
            # rather than silently substituting another source.
            logger.warning("unknown contact_search source %r; skipping", token)
    if not providers:
        providers.append(StubContactSearchProvider())
    return providers


def build_registry_from_settings(browser=None) -> DataSourceRegistry:
    """Assemble a registry from ``NEXUS_*`` settings.

    ``browser`` (when given) is threaded into the web-search provider so callers — notably the
    agent runtime in tests — can inject a deterministic browser. Defaults preserve today's
    behavior: web search is on (DuckDuckGo) and company discovery is search-backed.
    """
    from nexus.core.config import get_settings

    s = get_settings()
    search = build_search_provider(s.search_provider, browser=browser)
    return DataSourceRegistry(
        company_search=_build_company_search(
            s.company_search_source_list, search=search, browser=browser
        ),
        search=search,
        research=build_research_provider(s.research_provider),
        email_verify=build_email_verifier(s.email_verify_provider),
        contact_search=_build_contact_search(s.contact_search_source_list, search=search),
    )


_registry: DataSourceRegistry | None = None


def get_registry() -> DataSourceRegistry:
    global _registry
    if _registry is None:
        _registry = build_registry_from_settings()
    return _registry


def set_registry(registry: DataSourceRegistry | None) -> None:
    global _registry
    _registry = registry
