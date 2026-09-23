"""Resolving an employer's website from its name, for "Find similar people".

A rep had to type the domain by hand, and blank meant no email and no signals for that company.
A name is not an identity, so the match has to be defensible: the search result travels back with
the answer so the rep confirming it can see what matched.
"""
from __future__ import annotations

from nexus.enrichment.company_domain import resolve_company_domain
from nexus.integrations.search.provider import SearchHit


def _search(*hits):
    async def search(query, limit=5):
        assert "acme" in query.lower()
        return [SearchHit(title=t, url=u, snippet="", source="fake") for t, u in hits]

    return search


async def test_the_companys_own_site_is_found():
    found = await resolve_company_domain("Acme Corp", search=_search(
        ("Acme Corp | LinkedIn", "https://www.linkedin.com/company/acme"),
        ("Acme Corp — Official site", "https://www.acme.com/"),
    ))
    assert found.domain == "acme.com"
    assert found.url.startswith("https://www.acme.com")


async def test_directories_and_social_profiles_are_never_the_answer():
    found = await resolve_company_domain("Acme Corp", search=_search(
        ("Acme Corp | LinkedIn", "https://www.linkedin.com/company/acme"),
        ("Acme Corp - Crunchbase", "https://www.crunchbase.com/organization/acme"),
        ("Acme Corp jobs", "https://www.indeed.com/cmp/Acme-Corp"),
    ))
    assert found is None


async def test_a_legal_suffix_does_not_stop_the_match():
    found = await resolve_company_domain("Acme Technologies Pvt Ltd", search=_search(
        ("Acme", "https://acme.io/"),
    ))
    assert found.domain == "acme.io"


async def test_a_near_miss_is_not_offered():
    # Shares a word with the company and nothing else. Offering it invites a rep to click past it.
    hits = _search(("Acme Plumbing - emergency plumbers", "https://acmeplumbing.com/"))
    assert await resolve_company_domain("Acme Corp", search=hits) is None


async def test_a_title_match_is_offered_when_a_rep_will_confirm_it():
    found = await resolve_company_domain("Acme Corp", search=_search(
        ("Acme Corp — the home of Acme", "https://acmehq.com/"),
    ))
    assert found.domain == "acmehq.com"


async def test_a_failing_search_is_no_answer_not_an_error():
    async def boom(query, limit=5):
        raise RuntimeError("provider down")

    assert await resolve_company_domain("Acme Corp", search=boom) is None


async def test_no_name_no_search():
    async def must_not_run(query, limit=5):
        raise AssertionError("searched for nothing")

    assert await resolve_company_domain("  ", search=must_not_run) is None
