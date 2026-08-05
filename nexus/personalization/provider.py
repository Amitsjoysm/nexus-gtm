"""Social-enrichment provider seam for person-level personalization.

v1 ships only :class:`StubPersonalizationProvider` (no network, returns nothing). A future Apify
actor (LinkedIn / X / other social) plugs in behind ``NEXUS_PERSONALIZATION_PROVIDER`` and returns
:class:`PersonInsights` — recent posts, comments, headline, interests — which land on
``contact.custom_fields['personalization']`` and are folded into the email/call brief automatically.
Selection mirrors the other provider seams (email_verify / crm / telephony): one env line, no
caller changes.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass(slots=True)
class PersonInsights:
    """What we learned about a person from their public social footprint."""

    headline: str = ""                       # e.g. LinkedIn headline
    summary: str = ""                        # short bio
    recent_posts: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    source: str = ""                         # provider name ("apify", ...)
    fetched_at: str | None = None

    def as_dict(self) -> dict:
        return {
            "headline": self.headline,
            "summary": self.summary,
            "recent_posts": list(self.recent_posts),
            "comments": list(self.comments),
            "interests": list(self.interests),
            "source": self.source,
            "fetched_at": self.fetched_at,
        }

    def is_empty(self) -> bool:
        return not (self.headline or self.summary or self.recent_posts or self.comments or self.interests)


class PersonalizationProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def fetch(self, *, full_name: str, linkedin_url: str | None,
                    social_urls: list[str] | None = None) -> PersonInsights | None:
        """Return social insights for a person, or None if nothing is available."""


class StubPersonalizationProvider(PersonalizationProvider):
    """Offline default: no social fetch. Person-level personalization still works off the
    contact's own fields (title/seniority/role angle) — this only adds social *signal* when a
    real provider (Apify) is configured."""

    name = "stub"

    async def fetch(self, *, full_name, linkedin_url=None, social_urls=None) -> PersonInsights | None:
        return None


def build_personalization_provider(name: str) -> PersonalizationProvider:
    """Construct the configured provider. Unknown/blank keys fail safe to the offline stub.

    An unknown name returning the stub rather than raising is deliberate: a typo in
    ``NEXUS_PERSONALIZATION_PROVIDER`` costs personalization, not the ability to send email at all.
    It is logged, because silently running the stub when someone asked for Apify is exactly the
    "configured but doing nothing" state this codebase keeps having to diagnose.
    """
    key = (name or "").strip().lower()
    if key in ("", "stub", "none"):
        return StubPersonalizationProvider()
    if key == "apify":
        from nexus.personalization.apify_provider import ApifyPersonalizationProvider

        return ApifyPersonalizationProvider()
    import logging

    logging.getLogger("nexus.personalization").warning(
        "unknown NEXUS_PERSONALIZATION_PROVIDER %r; using the offline stub (no social insights)",
        name,
    )
    return StubPersonalizationProvider()


_provider: PersonalizationProvider | None = None


def get_personalization_provider() -> PersonalizationProvider:
    global _provider
    if _provider is None:
        from nexus.core.config import get_settings

        _provider = build_personalization_provider(get_settings().personalization_provider)
    return _provider


def set_personalization_provider(provider: PersonalizationProvider) -> None:
    """Test/runtime override."""
    global _provider
    _provider = provider


async def refresh_person_insights(ts, contact) -> PersonInsights | None:
    """Fetch a contact's social insights via the configured provider and persist them on
    ``contact.custom_fields['personalization']``. No-op (returns None) under the stub, so it's
    safe to call from enrichment now; it lights up the moment Apify is configured."""
    from datetime import datetime, timezone

    provider = get_personalization_provider()
    try:
        insights = await provider.fetch(
            full_name=contact.full_name,
            linkedin_url=contact.linkedin_url,
            social_urls=(contact.custom_fields or {}).get("social_urls"),
        )
    except Exception:  # a social-fetch outage must never break enrichment
        return None
    if insights is None or insights.is_empty():
        return None
    insights.fetched_at = datetime.now(timezone.utc).isoformat()
    cf = dict(contact.custom_fields or {})
    cf["personalization"] = insights.as_dict()
    contact.custom_fields = cf
    await ts.flush()
    return insights
