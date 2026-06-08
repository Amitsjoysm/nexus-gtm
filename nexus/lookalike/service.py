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

from nexus.core.tenancy import TenantSession
from nexus.integrations.company_search import domain_from_url
from nexus.integrations.registry import get_registry
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


def _seed_url(account: Account) -> str | None:
    """Prefer an explicit domain; build a canonical https URL the similarity API can seed from."""
    domain = (account.domain or "").strip().lower()
    if not domain:
        return None
    return domain if "://" in domain else f"https://{domain}"


class LookalikeService:
    async def find(
        self, ts: TenantSession, account: Account, *, limit: int = 10
    ) -> list[Lookalike]:
        seed = _seed_url(account)
        if seed is None:
            return []
        seed_domain = domain_from_url(seed)

        hits = await get_registry().find_similar(seed, limit=limit)
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
            seen.add(domain)
            title = (getattr(hit, "title", None) or domain).strip()
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
