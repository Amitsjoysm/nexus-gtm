# tests/test_title_matching_wiring.py
"""Contact SEARCH and contact RANKING must use the same title matcher.

A tester asked for "Facilities Director" and got no contacts across three campaigns. Two separate
places had to change, and they had to change to the *same* rule:

* **search** built its queries from the one literal title, so the index's "Director of Facilities"
  was never queried for;
* **ranking** tested `bt.lower() in title`, which is False for that pair, so even a contact that
  did arrive ranked as a non-match.

If these two ever disagree, a contact is found and then discarded as irrelevant — worse than not
finding them, because the search was billed for.
"""
from __future__ import annotations

from nexus.models.account import Account


class _RecordingSearch:
    def __init__(self):
        self.queries: list[str] = []

    async def search(self, query, limit=10, **kw):
        self.queries.append(query)
        return []


# ---- search ------------------------------------------------------------------------------------

async def test_the_spec_widens_the_queries():
    """The index holds 'Director of Facilities'; a query for the literal 'Facilities Director'
    finds neither it nor 'Head of Facilities'."""
    from nexus.integrations.contact_search import SearchBackedContactSearchProvider

    search = _RecordingSearch()
    provider = SearchBackedContactSearchProvider(search=search, llm=None)
    icp = {"job_levels": ["director", "head"], "title_keywords": ["facilities"]}
    await provider.search(Account(tenant_id="t1", name="Acme", domain="acme.com"), icp, limit=5)

    blob = " ".join(search.queries).lower()
    assert "facilities" in blob, f"the keyword never reached a query: {search.queries}"
    assert any(p in blob for p in ("director of facilities", "facilities director")), \
        f"no expanded phrasing in the queries: {search.queries}"


async def test_plain_buyer_titles_are_unaffected():
    """Regression guard: a workspace with literal buyer_titles and no spec queries as before."""
    from nexus.integrations.contact_search import SearchBackedContactSearchProvider

    search = _RecordingSearch()
    provider = SearchBackedContactSearchProvider(search=search, llm=None)
    await provider.search(Account(tenant_id="t1", name="Acme", domain="acme.com"),
                          {"buyer_titles": ["VP Sales"]}, limit=5)
    assert "vp sales" in " ".join(search.queries).lower()


async def test_an_icp_with_no_titles_still_searches():
    """No titles at all must still query the company's leadership — the pre-existing behaviour."""
    from nexus.integrations.contact_search import SearchBackedContactSearchProvider

    search = _RecordingSearch()
    provider = SearchBackedContactSearchProvider(search=search, llm=None)
    await provider.search(Account(tenant_id="t1", name="Acme", domain="acme.com"), {}, limit=5)
    assert search.queries, "an ICP with no titles must not stop the search entirely"


async def test_a_result_outside_the_spec_is_discarded():
    """The expanded queries over-match on purpose. A person who fails the spec must not reach the
    rep — the noise belongs in the query, not in the output."""
    from nexus.integrations import contact_search as cs
    from nexus.integrations.contact_search import SearchBackedContactSearchProvider

    class _Hit:
        title = "Acme leadership"
        snippet = "..."
        url = "https://acme.com/team"

    class _Search:
        async def search(self, query, limit=10, **kw):
            return [_Hit()]

    provider = SearchBackedContactSearchProvider(search=_Search(), llm=None)

    async def _people(account, titles, hits, limit):
        return [
            {"full_name": "Jane Roe", "title": "Director of Facilities"},
            {"full_name": "Sam Poe", "title": "Software Engineer"},
        ]

    provider._extract_people = _people
    out = await provider.search(
        Account(tenant_id="t1", name="Acme", domain="acme.com"),
        {"job_levels": ["director"], "title_keywords": ["facilities"]},
        limit=5,
    )
    assert [c.full_name for c in out] == ["Jane Roe"]


# ---- ranking -----------------------------------------------------------------------------------

def test_contact_rec_uses_the_shared_matcher():
    """A private matching rule in the agent would drift from the one search uses, and the first
    symptom is a contact that search finds and ranking calls irrelevant."""
    import inspect

    from nexus.agents import contact_rec

    src = inspect.getsource(contact_rec)
    assert "matches_title" in src, (
        "contact_rec must use nexus.relevance.job_levels.matches_title"
    )


def test_the_reordered_title_the_tester_reported_now_matches():
    from nexus.relevance.job_levels import matches_title

    spec = {"job_levels": ["director"], "title_keywords": ["facilities"]}
    assert matches_title("Director of Facilities", spec)
    # And the substring test that used to gate it does not:
    assert "facilities director" not in "director of facilities"
