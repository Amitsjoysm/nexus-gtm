"""Web-backed account firmographic enrichment (Exa/DuckDuckGo + LLM). Offline/deterministic.

These are about EXTRACTION — what the LLM output turns into on the account — so they run with
`meter=False` and no session. Billing for `enrich.account` is a separate concern with its own
tests in `tests/test_enrichment_billing.py`; mixing the two here would make a field-mapping
test fail for a billing reason.
"""
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
    out = await SearchBackedAccountEnricher(_FakeSearch(hits), llm).enrich(None, acc, meter=False)
    assert set(out) == {"industry", "employee_count", "country", "tech_stack", "description"}
    assert acc.industry == "Logistics" and acc.employee_count == 1200
    assert acc.country == "United States" and acc.tech_stack == ["SAP", "Salesforce"]
    assert acc.custom_fields["description"].startswith("Third-party")


async def test_enrich_never_overwrites_existing_values():
    acc = Account(tenant_id="t", name="Acme", domain="acme.com",
                  industry="Fintech", employee_count=50, country="Canada")
    llm = _FakeLLM('{"industry":"Logistics","employee_count":9999,"country":"United States"}')
    out = await SearchBackedAccountEnricher(_FakeSearch([_Hit("Acme")]), llm).enrich(None, acc, meter=False)
    # Pre-set fields are preserved; only genuinely blank ones could be filled.
    assert acc.industry == "Fintech" and acc.employee_count == 50 and acc.country == "Canada"
    assert "industry" not in out and "employee_count" not in out


async def test_enrich_stub_llm_is_noop():
    acc = Account(tenant_id="t", name="Acme", domain="acme.com")
    out = await SearchBackedAccountEnricher(_FakeSearch([_Hit("Acme")]), StubLLMProvider()).enrich(None, acc, meter=False)
    assert out == [] and acc.industry is None  # offline → nothing extracted


async def test_enrich_no_hits_is_noop():
    acc = Account(tenant_id="t", name="Ghost", domain="ghost.example")
    out = await SearchBackedAccountEnricher(_FakeSearch([]), _FakeLLM("{}")).enrich(None, acc, meter=False)
    assert out == []


async def test_enrich_ignores_bool_employee_count():
    acc = Account(tenant_id="t", name="Acme", domain="acme.com")
    llm = _FakeLLM('{"employee_count": true, "industry": "SaaS"}')
    out = await SearchBackedAccountEnricher(_FakeSearch([_Hit("Acme")]), llm).enrich(None, acc, meter=False)
    assert acc.employee_count is None and "employee_count" not in out
    assert acc.industry == "SaaS"


# ---- whose country is it? ----------------------------------------------------------------------

def test_the_prompt_says_country_means_where_the_company_is():
    """Observed in production: "Isys Softech Pvt Ltd", a healthcare BPO headquartered in Jaipur,
    was stored with country="United States" while city/region correctly read "Jaipur, Rajasthan".
    It then scored geo 1.0 against a USA-only ICP and reached a rep's list.

    The two fields come from the SAME LLM call on the SAME snippets, so the model was not confused
    about the location — it answered a different question. The prompt asked for `"country"` with no
    anchor, and this company's snippets are saturated with the market it serves: "medical billing
    for US clients", "us healthcare outsourcing". Asked for "country" against that text, "United
    States" is a defensible reading.

    `region` and `city` were unaffected because they are anchored by their own descriptions
    ("state/province/region"), which have no market-shaped alternative reading.

    A structural test on the prompt, because there is no offline way to assert what a model returns
    — the stub LLM does not reason. What IS checkable is that the instruction is unambiguous.
    """
    import inspect

    from nexus.enrichment.account import SearchBackedAccountEnricher

    src = inspect.getsource(SearchBackedAccountEnricher.fetch)
    lowered = src.lower()
    assert "headquarter" in lowered, (
        "the country field is not anchored to where the company is headquartered"
    )
    # The failure mode named explicitly. "Don't invent" was already there and did not help: the
    # model invented nothing, it answered the wrong question.
    assert "serve" in lowered or "customer" in lowered or "market" in lowered, (
        "the prompt does not rule out answering with the market the company SELLS INTO, which is "
        "the exact mistake that shipped"
    )


def test_a_country_that_contradicts_the_city_is_not_stored():
    """Defence in depth behind the prompt.

    `city="Jaipur"` with `country="United States"` is not a low-confidence answer, it is an
    impossible one, and it is worse than no answer: a blank country scores geo neutral, while a
    wrong country scores a perfect 1.0 against an ICP the account does not belong to. Dropping the
    contradiction leaves the record honest and the account correctly unscored on geo.

    Only checks the pairs it can actually settle. A city it does not recognise is left alone —
    guessing would trade this bug for a worse one.
    """
    from nexus.enrichment.account import SearchBackedAccountEnricher
    from nexus.models.account import Account

    enricher = SearchBackedAccountEnricher(search=None, llm=None)

    a = Account(name="Isys Softech", domain="eliteoffshoreresources.com")
    filled = enricher.apply(a, {
        "city": "Jaipur", "region": "Rajasthan", "country": "United States",
    })
    assert a.country in (None, ""), (
        f"stored a country that contradicts the city: country={a.country!r} city=Jaipur"
    )
    assert "country" not in filled
    # The location we DO trust is kept — the record still says where the company is.
    assert (a.custom_fields or {}).get("city") == "Jaipur"


def test_a_consistent_country_is_still_stored():
    """The guard must not cost the common case."""
    from nexus.enrichment.account import SearchBackedAccountEnricher
    from nexus.models.account import Account

    enricher = SearchBackedAccountEnricher(search=None, llm=None)
    a = Account(name="Ramp", domain="ramp.com")
    filled = enricher.apply(a, {"city": "New York", "region": "NY", "country": "United States"})
    assert a.country == "United States"
    assert "country" in filled


def test_an_unrecognised_city_does_not_block_the_country():
    """The guard settles only what it can. An unknown city must not suppress a good country."""
    from nexus.enrichment.account import SearchBackedAccountEnricher
    from nexus.models.account import Account

    enricher = SearchBackedAccountEnricher(search=None, llm=None)
    a = Account(name="Somewhere Ltd", domain="example.org")
    enricher.apply(a, {"city": "Nowheresville", "country": "United States"})
    assert a.country == "United States"
