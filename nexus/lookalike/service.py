"""Find-lookalike-companies — the Tier-3 "more like this won account" play.

Seed with an account's URL, ask the data substrate for similar companies (Exa ``find_similar``
behind the :class:`~nexus.integrations.registry.DataSourceRegistry`), then score each candidate
through the same relevance engine that ranks the rest of the book. The result is a ranked list of
net-new companies a rep can pursue or import as accounts.

Everything degrades cleanly offline: the stub search provider returns no similar pages, so the
service returns ``[]`` — no network, deterministic CI. The shape is identical to the real path so
swapping in a keyed Exa provider lights this up without touching callers.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nexus.core.config import get_settings
from nexus.core.tenancy import TenantSession
from nexus.integrations.company_search import clean_company_name, domain_from_url, looks_like_company
from nexus.integrations.search.provider import get_search_provider
from nexus.models.account import Account
from nexus.outcomes.service import get_outcome_service
from nexus.relevance.engine import get_profile, get_relevance_engine


@dataclass(slots=True)
class Lookalike:
    """A discovered similar company, scored against the tenant's ICP."""

    name: str
    domain: str
    url: str | None = None
    snippet: str = ""
    score: int = 50
    reasons: list[str] = field(default_factory=list)
    source: str = ""
    already_tracked: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "domain": self.domain,
            "url": self.url,
            "snippet": self.snippet,
            "score": self.score,
            "reasons": list(self.reasons),
            "source": self.source,
            "already_tracked": self.already_tracked,
        }


def _similar_query(account: Account) -> str:
    """Build a 'find companies like this one' query from the seed's firmographics — what the
    company *does* (description), its industry, geo, and stack — rather than its bare URL. This
    is what surfaces real peers (corporate-card fintechs, not the seed's own profile pages)."""
    cf = account.custom_fields or {}
    desc = (cf.get("description") or "").strip()
    bits: list[str] = []
    if desc:
        bits.append(desc)
    elif account.industry:
        bits.append(f"{account.industry} company")
    if account.country:
        bits.append(account.country)
    if account.tech_stack:
        bits.append("using " + ", ".join(str(t) for t in account.tech_stack[:3]))
    profile = " ".join(bits).strip()
    name = (account.name or account.domain or "").strip()
    if not profile:
        return f"companies similar to {name}" if name else ""
    return f"companies similar to {name}: {profile}" if name else profile


class LookalikeService:
    async def find(
        self, ts: TenantSession, account: Account, *, limit: int = 10
    ) -> list[Lookalike]:
        seed_domain = (account.domain or "").strip().lower()

        # 1) Make sure we actually know what this company is. Enrich blank firmographics from the
        #    web first (industry/description/geo/tech) so the similarity query is rich, not bare.
        settings = get_settings()
        needs_profile = account.industry is None or not (account.custom_fields or {}).get("description")
        if settings.account_enrich_enabled and needs_profile:
            try:
                from nexus.enrichment.account import get_account_enricher

                if await get_account_enricher().enrich(account):
                    await ts.flush()
            except Exception:  # enrichment must never block lookalikes
                pass

        # 2) Search for company homepages matching that profile (Exa category=company excludes
        #    directories/news), skipping the seed's own domain.
        query = _similar_query(account)
        if not query:
            return []
        provider = get_search_provider()
        exclude = [seed_domain] if seed_domain else None
        if hasattr(provider, "search_companies"):
            hits = await provider.search_companies(query, limit=max(limit * 3, 15), exclude_domains=exclude)
        else:
            hits = await provider.search(query, limit=max(limit * 3, 15))
        if not hits:
            return []

        # Dedup against the seed and anything already in the book; flag known domains.
        tracked = {
            (a.domain or "").lower()
            for a in await ts.list(Account)
            if a.domain
        }
        profile = await get_profile(ts)
        engine = get_relevance_engine()
        # Lean candidate scoring toward whatever firmographics this tenant's wins share.
        learned = (await get_outcome_service().learned_weights(ts)).weights

        seen: set[str] = set()
        out: list[Lookalike] = []
        for hit in hits:
            domain = domain_from_url(getattr(hit, "url", None))
            if not domain or domain == seed_domain or domain in seen:
                continue
            title = clean_company_name(getattr(hit, "title", None)) or domain
            # Drop aggregator / data-vendor / profile pages — a real lookalike is a company,
            # not the seed's listing on LinkedIn / Crunchbase / PitchBook / an app store.
            if not looks_like_company(domain, title):
                continue
            seen.add(domain)
            # Transient (un-persisted) account so the relevance engine can score the candidate
            # from whatever firmographics we have; never added to the session.
            candidate = Account(tenant_id=ts.tenant_id, name=title, domain=domain)
            fit = engine.score_icp_fit(profile, candidate, learned_weights=learned)
            out.append(
                Lookalike(
                    name=title,
                    domain=domain,
                    url=getattr(hit, "url", None),
                    snippet=getattr(hit, "snippet", "") or "",
                    score=fit.score,
                    reasons=fit.reasons,
                    source=getattr(hit, "source", "") or "",
                    already_tracked=domain in tracked,
                )
            )

        out.sort(key=lambda lk: lk.score, reverse=True)
        return out[:limit]


_service: LookalikeService | None = None


def get_lookalike_service() -> LookalikeService:
    global _service
    if _service is None:
        _service = LookalikeService()
    return _service


def set_lookalike_service(service: LookalikeService | None) -> None:
    global _service
    _service = service
