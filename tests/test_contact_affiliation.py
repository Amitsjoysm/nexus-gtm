"""A contact belongs to an account only when something PROVES they work there.

Reported from staging 2026-09-10: "Find contacts" on devbay.com added a person whose LinkedIn
(linkedin.com/in/furqan-mustafa-42a17676) is at a completely different company.

Reproduced locally the same day, and the source was not a LinkedIn profile at all:

    https://softmount.devsoftmount.com/leadership/     titled "Leadership – Devbay"

a leadership page on ANOTHER company's domain that happens to carry the name. The LLM extraction
took everyone on it — Arham Hashmi, David Fullerton, Khurrum Jawaid, Furqan Mustafa — because it was
asked for people who "plausibly hold these roles" and nothing afterwards checked where the page was.
Then `LinkedInFinder` attached a profile to each by NAME alone, which cannot tell one Furqan Mustafa
from another: the namesake's profile the user saw.

This codebase has shipped six wrong-COMPANY bugs by trusting a name match, and its own rule for
people is stricter still: "getting a person wrong means a rep phones a stranger with someone else's
context". So the question every source here must answer is the one CLAUDE.md asks of signals — what
proves this result is about this account? Two things do:

* the page is on the ACCOUNT'S OWN domain (its team / about / leadership page), or
* it is LinkedIn, where a person states their own employer, and it names this company.

A third-party page that merely shares the name is not proof, however plausible it reads.
"""
from __future__ import annotations

from types import SimpleNamespace

from nexus.integrations.contact_search import SearchBackedContactSearchProvider
from nexus.models.account import Account, Contact


def _hit(url: str, title: str = "", snippet: str = ""):
    return SimpleNamespace(url=url, title=title, snippet=snippet)


class _FakeSearch:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, q, *, limit=5):
        return self._hits


class _FakeLLM:
    def __init__(self, text):
        self.text = text

    async def complete(self, messages, **k):
        from nexus.agents.llm import LLMResponse

        return LLMResponse(text=self.text)


def _people(*names_titles_urls) -> str:
    import json

    return json.dumps([
        {"full_name": n, "title": t, "seniority": "", "linkedin_url": u}
        for n, t, u in names_titles_urls
    ])


DEVBAY = Account(tenant_id="t", name="Devbay", domain="devbay.com")


# ---- sourcing ---------------------------------------------------------------------------------------

async def test_people_on_another_companys_page_are_not_this_accounts_contacts():
    """The staging report, reproduced: a leadership page on devsoftmount.com titled "Devbay"."""
    hits = [_hit(
        "https://softmount.devsoftmount.com/leadership/", "Leadership – Devbay",
        "Arham Hashmi Chief Executive Officer. Furqan Mustafa Chief Technology Officer.",
    )]
    llm = _FakeLLM(_people(("Arham Hashmi", "Chief Executive Officer", None),
                           ("Furqan Mustafa", "Chief Technology Officer", None)))
    out = await SearchBackedContactSearchProvider(_FakeSearch(hits), llm).search(DEVBAY, {}, limit=5)

    assert out == [], "a page on a different domain sourced contacts because its title said Devbay"


async def test_people_on_the_accounts_own_site_are_its_contacts():
    hits = [_hit("https://devbay.com/about/team", "Our team | Devbay",
                 "Sara Iqbal, Chief Revenue Officer, leads our go-to-market.")]
    llm = _FakeLLM(_people(("Sara Iqbal", "Chief Revenue Officer", None)))
    out = await SearchBackedContactSearchProvider(_FakeSearch(hits), llm).search(DEVBAY, {}, limit=5)

    assert [c.full_name for c in out] == ["Sara Iqbal"]


async def test_a_linkedin_profile_naming_this_company_is_proof():
    hits = [_hit("https://www.linkedin.com/in/omar-riaz", "Omar Riaz - VP Sales - Devbay | LinkedIn",
                 "VP Sales at Devbay. Building partnerships across the US.")]
    llm = _FakeLLM(_people(("Omar Riaz", "VP Sales", "https://www.linkedin.com/in/omar-riaz")))
    out = await SearchBackedContactSearchProvider(_FakeSearch(hits), llm).search(DEVBAY, {}, limit=5)

    assert [c.full_name for c in out] == ["Omar Riaz"]
    assert out[0].linkedin_url == "https://www.linkedin.com/in/omar-riaz"


async def test_a_linkedin_profile_at_another_company_is_not():
    hits = [_hit("https://www.linkedin.com/in/furqan-mustafa-42a17676",
                 "Furqan Mustafa - CTO - Softmount | LinkedIn", "CTO at Softmount.")]
    llm = _FakeLLM(_people(("Furqan Mustafa", "CTO", "https://www.linkedin.com/in/furqan-mustafa-42a17676")))
    out = await SearchBackedContactSearchProvider(_FakeSearch(hits), llm).search(DEVBAY, {}, limit=5)

    assert out == []


async def test_a_linkedin_url_that_no_result_contained_is_dropped():
    """The model may not invent a profile. A URL is kept only if the search actually returned it."""
    hits = [_hit("https://devbay.com/team", "Team | Devbay", "Sara Iqbal, Chief Revenue Officer")]
    llm = _FakeLLM(_people(("Sara Iqbal", "Chief Revenue Officer",
                            "https://www.linkedin.com/in/some-other-sara")))
    out = await SearchBackedContactSearchProvider(_FakeSearch(hits), llm).search(DEVBAY, {}, limit=5)

    assert [c.full_name for c in out] == ["Sara Iqbal"]
    assert out[0].linkedin_url is None


async def test_a_short_company_name_must_appear_as_a_word():
    """"Opp" is inside "opportunity"; a substring is not the company."""
    opp = Account(tenant_id="t", name="Opp", domain="opp.agency")
    hits = [_hit("https://www.linkedin.com/in/lee-park", "Lee Park | LinkedIn",
                 "Seizing every opportunity in enterprise sales at Globex.")]
    llm = _FakeLLM(_people(("Lee Park", "Head of Sales", "https://www.linkedin.com/in/lee-park")))
    out = await SearchBackedContactSearchProvider(_FakeSearch(hits), llm).search(opp, {}, limit=5)

    assert out == []


# ---- the LinkedIn finder ------------------------------------------------------------------------------

class _FinderSearch:
    def __init__(self, hits):
        self._hits = hits

    async def __call__(self, query, *, limit=5):
        return self._hits


async def test_the_finder_does_not_attach_a_namesakes_profile():
    """Right name, wrong company: exactly the profile the staging user was shown."""
    from nexus.enrichment.linkedin import LinkedInFinder

    hits = [_hit("https://www.linkedin.com/in/furqan-mustafa-42a17676",
                 "Furqan Mustafa - Software Engineer - Globex | LinkedIn", "Engineer at Globex.")]
    url = await LinkedInFinder(_FinderSearch(hits)).find(
        DEVBAY, Contact(full_name="Furqan Mustafa", account_id="a"))
    assert url is None


async def test_the_finder_attaches_a_profile_that_names_the_company():
    from nexus.enrichment.linkedin import LinkedInFinder

    hits = [_hit("https://www.linkedin.com/in/omar-riaz", "Omar Riaz - Devbay | LinkedIn",
                 "VP Sales at Devbay")]
    url = await LinkedInFinder(_FinderSearch(hits)).find(
        DEVBAY, Contact(full_name="Omar Riaz", account_id="a"))
    assert url == "https://www.linkedin.com/in/omar-riaz"
