"""LinkedIn profile finder: URL extraction + name-matching. Offline (fake search callable)."""
from __future__ import annotations

from types import SimpleNamespace

from nexus.enrichment.linkedin import LinkedInFinder, canonical_profile_url
from nexus.models.account import Account, Contact


def test_canonical_profile_url_extracts_and_normalizes():
    assert (
        canonical_profile_url("https://www.linkedin.com/in/john-collison/")
        == "https://www.linkedin.com/in/john-collison"
    )
    assert (
        canonical_profile_url("https://linkedin.com/in/jane-doe?trk=abc")
        == "https://linkedin.com/in/jane-doe"
    )
    # Non-profile links are rejected (company/school pages, other sites).
    assert canonical_profile_url("https://www.linkedin.com/company/stripe") is None
    assert canonical_profile_url("https://example.com/team") is None


class _FakeSearch:
    """Stand-in for registry.search: an awaitable callable returning preset hits."""

    def __init__(self, hits):
        self._hits = hits
        self.calls: list[str] = []

    async def __call__(self, query, *, limit=5):
        self.calls.append(query)
        return self._hits


def _hit(url, title="", snippet=""):
    return SimpleNamespace(url=url, title=title, snippet=snippet)


async def test_finder_matches_correct_person():
    hits = [
        _hit("https://www.linkedin.com/company/stripe", "Stripe | LinkedIn"),
        _hit("https://www.linkedin.com/in/patrick-collison", "Patrick Collison - Stripe"),
    ]
    finder = LinkedInFinder(_FakeSearch(hits))
    url = await finder.find(
        Account(name="Stripe", domain="stripe.com"),
        Contact(full_name="Patrick Collison", account_id="a"),
    )
    assert url == "https://www.linkedin.com/in/patrick-collison"


async def test_finder_rejects_namesake():
    # Only "John Collison" appears — must not be returned for a different "Jane Collison".
    hits = [_hit("https://www.linkedin.com/in/john-collison", "John Collison")]
    finder = LinkedInFinder(_FakeSearch(hits))
    url = await finder.find(
        Account(name="Stripe", domain="stripe.com"),
        Contact(full_name="Jane Collison", account_id="a"),
    )
    assert url is None


async def test_finder_no_results_returns_none():
    finder = LinkedInFinder(_FakeSearch([]))
    url = await finder.find(
        Account(name="Acme", domain="acme.com"),
        Contact(full_name="Ada Lovelace", account_id="a"),
    )
    assert url is None
