"""An address scraped from search results must belong to THIS person, at THIS company.

The search provider took the first address on the company domain it saw in six result snippets,
and failing that ANY address at 0.55 — so `press@`, a colleague's `jane.smith@`, or an address at
another company could become the contact's email. Correct means: on the company's email domain,
and in one of this person's own name patterns (the same list the pattern finder tries).
"""
from __future__ import annotations

from nexus.enrichment.providers import SearchEnrichmentProvider, name_patterns
from nexus.models.account import Account, Contact


class FakeBrowser:
    def __init__(self, snippet: str):
        self._snippet = snippet

    async def search(self, query, limit=6):
        return [{"title": "Result", "snippet": self._snippet, "url": "https://x.test"}]


async def _found(snippet: str, name="Jane Doe"):
    account = Account(tenant_id="t", name="Acme", domain="acme.com")
    contact = Contact(tenant_id="t", account_id="a", full_name=name)
    return await SearchEnrichmentProvider(FakeBrowser(snippet)).enrich(account, contact)


def test_the_name_patterns_are_the_finders_list():
    assert name_patterns("Jane Doe")[:3] == ["jane.doe", "jane", "janedoe"]
    assert "jdoe" in name_patterns("Jane Doe")
    assert name_patterns("Renée O'Mara")[0] == "renee.omara"


async def test_the_persons_own_address_is_taken():
    res = await _found("contact Jane at j.doe@acme.com")
    assert res.email == "j.doe@acme.com"
    assert res.email_confidence == 0.8


async def test_a_colleagues_address_is_not_taken():
    res = await _found("press contact: jane.smith@acme.com")
    assert res.email is None


async def test_a_role_address_is_not_taken():
    res = await _found("write to press@acme.com or info@acme.com")
    assert res.email is None


async def test_an_address_at_another_company_is_not_taken():
    res = await _found("Jane Doe, previously jane.doe@oldcorp.com")
    assert res.email is None
