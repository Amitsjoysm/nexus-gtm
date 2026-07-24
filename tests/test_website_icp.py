"""AI website -> ICP drafting. Offline (fake search + LLM)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from nexus.relevance.website_icp import analyze_website_to_icp, domain_of
from tests.conftest import auth, signup


def test_domain_of_normalizes():
    assert domain_of("https://www.acme.com/pricing") == "acme.com"
    assert domain_of("acme.io") == "acme.io"
    assert domain_of("http://sub.acme.co.uk/x?y=1") == "sub.acme.co.uk"
    assert domain_of("") == ""


class _FakeSearch:
    def __init__(self, hits):
        self._hits = hits

    async def __call__(self, query, *, limit=5):
        return self._hits


class _FakeLLM:
    def __init__(self, text):
        self._text = text

    async def complete(self, messages, **kwargs):
        return SimpleNamespace(text=self._text)


def _hit(title="", snippet="", url=""):
    return SimpleNamespace(title=title, snippet=snippet, url=url)


async def test_analyze_returns_coerced_draft():
    hits = [_hit("Acme - Payments API", "Acme sells payment infra to fintechs", "https://acme.com")]
    llm_json = json.dumps({
        "icp": {"industries": ["Fintech", "Software"], "countries": ["United States"],
                "required_tech": ["AWS"], "employee_min": 50, "employee_max": 1000,
                "buyer_titles": ["VP Engineering", "CTO"]},
        "value_props": [{"name": "Fast payments", "description": "x",
                         "pains_solved": ["slow settlement"]}],
        "product_context": "Acme provides payment APIs."})
    draft = await analyze_website_to_icp(
        "https://acme.com", search=_FakeSearch(hits), llm=_FakeLLM(llm_json)
    )
    assert draft["icp"]["industries"] == ["Fintech", "Software"]
    assert draft["icp"]["employee_min"] == 50
    assert draft["icp"]["buyer_titles"] == ["VP Engineering", "CTO"]
    assert draft["value_props"][0]["name"] == "Fast payments"
    assert "payment" in draft["product_context"].lower()


async def test_analyze_empty_when_no_search_hits():
    draft = await analyze_website_to_icp(
        "https://acme.com", search=_FakeSearch([]), llm=_FakeLLM("{}")
    )
    assert draft == {"icp": {}, "value_props": [], "product_context": ""}


async def test_analyze_empty_on_unparseable_url():
    draft = await analyze_website_to_icp("", search=_FakeSearch([_hit("x")]), llm=_FakeLLM("{}"))
    assert draft["icp"] == {}


async def test_analyze_tolerates_prose_around_json():
    hits = [_hit("Acme", "sells to retail", "https://acme.com")]
    text = ('Here is the ICP:\n{"icp": {"industries": ["Retail"]}, "value_props": [], '
            '"product_context": "Acme"}\nHope that helps!')
    draft = await analyze_website_to_icp("acme.com", search=_FakeSearch(hits), llm=_FakeLLM(text))
    assert draft["icp"]["industries"] == ["Retail"]


async def test_analyze_website_endpoint_shape(client):
    # In the test env search/LLM are stubs -> empty but well-formed draft, 200 OK.
    token = await signup(client, slug="aw", email="o@aw.com", company="AW")
    r = await client.post(
        "/api/relevance/analyze-website", headers=auth(token), json={"url": "https://acme.com"}
    )
    assert r.status_code == 200
    data = r.json()
    assert set(data) >= {"icp", "value_props", "product_context"}
