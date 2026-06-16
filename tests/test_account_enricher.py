"""Web-backed account firmographic enrichment (Exa/DuckDuckGo + LLM). Offline/deterministic."""
from __future__ import annotations

from nexus.agents.llm import LLMResponse, StubLLMProvider
from nexus.enrichment.account import SearchBackedAccountEnricher, _parse_obj
from nexus.models.account import Account


class _Hit:
    def __init__(self, title, snippet="", url=""):
        self.title, self.snippet, self.url = title, snippet, url


class _FakeSearch:
    def __init__(self, hits):
        self._hits = hits

    async def search(self, q, *, limit=6):
        return self._hits


class _FakeLLM:
    def __init__(self, text):
        self.text = text

    async def complete(self, messages, **k):
        return LLMResponse(text=self.text)


def test_parse_obj_tolerates_prose():
    assert _parse_obj('x {"industry":"Logistics"} y') == {"industry": "Logistics"}
    assert _parse_obj("nope") == {} and _parse_obj(None) == {}


async def test_enrich_fills_blank_firmographics():
    acc = Account(tenant_id="t", name="Northwind", domain="northwind.com")
    hits = [_Hit("Northwind Logistics", "3PL provider, 1200 employees, USA", "https://northwind.com")]
    llm = _FakeLLM('{"industry":"Logistics","employee_count":1200,"country":"United States",'
                   '"description":"Third-party logistics provider.","tech_stack":["SAP","Salesforce"]}')
    out = await SearchBackedAccountEnricher(_FakeSearch(hits), llm).enrich(acc)
    assert set(out) == {"industry", "employee_count", "country", "tech_stack", "description"}
    assert acc.industry == "Logistics" and acc.employee_count == 1200
    assert acc.country == "United States" and acc.tech_stack == ["SAP", "Salesforce"]
    assert acc.custom_fields["description"].startswith("Third-party")


async def test_enrich_never_overwrites_existing_values():
    acc = Account(tenant_id="t", name="Acme", domain="acme.com",
                  industry="Fintech", employee_count=50, country="Canada")
    llm = _FakeLLM('{"industry":"Logistics","employee_count":9999,"country":"United States"}')
    out = await SearchBackedAccountEnricher(_FakeSearch([_Hit("Acme")]), llm).enrich(acc)
    # Pre-set fields are preserved; only genuinely blank ones could be filled.
    assert acc.industry == "Fintech" and acc.employee_count == 50 and acc.country == "Canada"
    assert "industry" not in out and "employee_count" not in out


async def test_enrich_stub_llm_is_noop():
    acc = Account(tenant_id="t", name="Acme", domain="acme.com")
    out = await SearchBackedAccountEnricher(_FakeSearch([_Hit("Acme")]), StubLLMProvider()).enrich(acc)
    assert out == [] and acc.industry is None  # offline → nothing extracted


async def test_enrich_no_hits_is_noop():
    acc = Account(tenant_id="t", name="Ghost", domain="ghost.example")
    out = await SearchBackedAccountEnricher(_FakeSearch([]), _FakeLLM("{}")).enrich(acc)
    assert out == []


async def test_enrich_ignores_bool_employee_count():
    acc = Account(tenant_id="t", name="Acme", domain="acme.com")
    llm = _FakeLLM('{"employee_count": true, "industry": "SaaS"}')
    out = await SearchBackedAccountEnricher(_FakeSearch([_Hit("Acme")]), llm).enrich(acc)
    assert acc.employee_count is None and "employee_count" not in out
    assert acc.industry == "SaaS"
