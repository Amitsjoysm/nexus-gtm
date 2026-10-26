"""A company's industry is what it DOES, read off its own website with a quote (2026-10-26).

SDR feedback: "we chose Oil & Gas; it pulled Manufacturing companies and labelled them Oil & Gas".
Measured on the live Exa API, most oilfield equipment makers CALL themselves Oil and Gas (in Exa's
profile and on LinkedIn), so even a source-reported label files them under the industry their
customers are in. Decided with the owner: industry from evidence, traceable (D1), and the
company's activity decides (D12); an ICP can tick "include suppliers" to take them back.

The reader is held to evidence: one answer must quote a sentence that is on the page, from a list
it was offered. No quote, an id it was not offered, or a page with too little text is no verdict,
never a guess. It is off until a hand-checked sample clears the precision bar.
"""
from __future__ import annotations

import json
from collections import Counter

from nexus.companies import industry_evidence as ev

MAKER = ("Lincoln Manufacturing builds pressure-control equipment. We manufacture wellhead "
         "components, frac valves and flow iron for drilling contractors across Texas. Our "
         "machine shop runs CNC lathes around the clock to deliver API 6A parts on time. "
         "Founded in 1981 in Houston, we serve operators and service companies worldwide.")
PRODUCER = ("Mewbourne Oil Company is an independent oil and natural gas exploration and "
            "production company. We drill and operate wells in the Anadarko and Permian basins "
            "and produce crude oil and natural gas from more than 2,000 wells. Our geologists "
            "and engineers have explored the Mid-Continent since 1965.")


def _site(text, url="https://example.test/about"):
    async def fetch(domain):
        return url, text
    return fetch


def _answers(*replies):
    """An LLM that answers each prompt in turn and records what it was offered."""
    asked: list[str] = []

    async def ask(prompt: str) -> str:
        asked.append(prompt)
        return json.dumps(replies[len(asked) - 1])
    ask.asked = asked  # type: ignore[attr-defined]
    return ask


async def test_activity_decides_a_maker_for_oil_producers_is_manufacturing():
    ask = _answers({"sector": 25, "quote": "We manufacture wellhead components, frac valves"},
                   {"id": 55, "quote": "We manufacture wellhead components, frac valves and "
                                       "flow iron for drilling contractors"})

    verdict = await ev.classify("lincolnmanufacturing.com", "Lincoln Manufacturing",
                                fetch=_site(MAKER), ask=ask)

    assert verdict is not None and verdict.code == 55          # Machinery Manufacturing
    assert verdict.sector == 25 and verdict.url == "https://example.test/about"
    assert "frac valves" in verdict.quote
    # The model is told to judge the company's OWN activity, never its customers' industry.
    assert "not by the industry its customers are in" in ask.asked[0]
    # A sector of 91 codes is offered as at most 30: the prompt that was never answered.
    assert len(ask.asked[1].splitlines()) < 60 and "55: Machinery Manufacturing" in ask.asked[1]


async def test_a_producer_is_oil_and_gas():
    ask = _answers({"sector": 332, "quote": "independent oil and natural gas exploration and "
                                            "production company"},
                   {"id": 3095, "quote": "produce crude oil and natural gas from more than "
                                         "2,000 wells"})

    verdict = await ev.classify("mewbourne.com", "Mewbourne Oil Company", fetch=_site(PRODUCER),
                                ask=ask)

    assert verdict is not None and verdict.code == 3095


async def test_a_quote_that_is_not_on_the_page_is_no_verdict():
    ask = _answers({"sector": 332, "quote": "We are the leading oil and gas producer in Texas"})

    assert await ev.classify("lincolnmanufacturing.com", "Lincoln", fetch=_site(MAKER),
                             ask=ask) is None


async def test_an_id_it_was_not_offered_is_no_verdict():
    ask = _answers({"sector": 25, "quote": "We manufacture wellhead components"},
                   {"id": 4, "quote": "We manufacture wellhead components"})   # software

    assert await ev.classify("lincolnmanufacturing.com", "Lincoln", fetch=_site(MAKER),
                             ask=ask) is None


async def test_too_little_text_is_no_verdict_and_asks_nothing():
    ask = _answers()

    assert await ev.classify("tiny.io", "Tiny", fetch=_site("Welcome. Login."), ask=ask) is None
    assert ask.asked == []


async def test_a_private_or_blocked_host_is_never_fetched():
    assert await ev.site_text("localhost") == ("", "")
    assert await ev.site_text("169.254.169.254") == ("", "")


def test_visible_text_drops_scripts_styles_and_markup_and_keeps_case():
    html = ("<html><head><style>.a{}</style><script>var x='We sell cars';</script></head>"
            "<body><nav>Home</nav><h1>Acme Valves</h1><p>We make &amp; test valves.</p></body>")
    assert ev.visible_text(html) == "Home Acme Valves We make & test valves."


# ---- what delivery does with it ------------------------------------------------------------------

ICP = {"industries": ["Oil & Gas"], "linkedin_industry_ids": [57], "countries": ["United States"]}


async def _deliver(monkeypatch, *, verdicts, mode="on", icp=ICP):
    from nexus.core.config import get_settings
    from nexus.prospecting.companies import Candidate
    from nexus.prospecting.deliver import deliver
    from nexus.relevance.engine import get_profile
    from tests.conftest import make_tenant, seed_relevance_profile, tenant_session

    calls: list[str] = []

    async def classify(domain, name, **kwargs):
        calls.append(domain)
        code = verdicts.get(domain)
        if code is None:
            return None
        return ev.IndustryVerdict(code=code, label=ev.label_of(code), sector=0,
                                  url=f"https://{domain}/about", quote="We do this.")

    monkeypatch.setattr(ev, "classify", classify)
    monkeypatch.setattr(get_settings(), "industry_evidence_mode", mode)
    monkeypatch.setattr(get_settings(), "icp_discovery_enrich_candidates", False)
    tid = await make_tenant(f"iev-{mode}-{len(verdicts)}-{icp.get('include_suppliers', 0)}")
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, icp=icp)
    cands = [Candidate(domain="mewbourne.com", name="Mewbourne", source="linkedin",
                       industry="Oil and Gas", country="United States",
                       linkedin_url="https://www.linkedin.com/company/mewbourne/"),
             Candidate(domain="lincolnmanufacturing.com", name="Lincoln", source="linkedin",
                       industry="Oil and Gas", country="United States",
                       linkedin_url="https://www.linkedin.com/company/lincoln-mfg/")]
    discarded = Counter()
    async with tenant_session(tid) as ts:
        ids, _ = await deliver(ts, await get_profile(ts), cands, limit=5,
                               source="auto_discovery", min_fit=0, discarded=discarded)
        from nexus.models.account import Account

        accounts = [await ts.get(Account, i) for i in ids]
    return accounts, discarded, calls


async def test_on_a_supplier_that_calls_itself_oil_and_gas_is_held_out(monkeypatch):
    accounts, discarded, _ = await _deliver(
        monkeypatch, verdicts={"mewbourne.com": 3095, "lincolnmanufacturing.com": 901})

    assert [a.domain for a in accounts] == ["mewbourne.com"]
    assert discarded["wrong_industry"] == 1
    producer = accounts[0]
    assert producer.industry == "Oil Extraction"
    assert producer.field_sources["industry"]["source"] == "website"
    assert producer.field_sources["industry"]["ref"] == "https://mewbourne.com/about"
    # What the company calls itself is kept, as evidence, not as the verdict.
    assert producer.custom_fields["industry_self_label"] == "Oil and Gas"


async def test_include_suppliers_takes_them_back(monkeypatch):
    accounts, discarded, _ = await _deliver(
        monkeypatch, verdicts={"mewbourne.com": 3095, "lincolnmanufacturing.com": 901},
        icp={**ICP, "include_suppliers": True})

    assert sorted(a.domain for a in accounts) == ["lincolnmanufacturing.com", "mewbourne.com"]
    maker = next(a for a in accounts if a.domain == "lincolnmanufacturing.com")
    assert maker.industry == ev.label_of(901)


async def test_shadow_records_the_verdict_and_changes_nothing(monkeypatch):
    accounts, discarded, _ = await _deliver(
        monkeypatch, verdicts={"mewbourne.com": 3095, "lincolnmanufacturing.com": 901},
        mode="shadow")

    assert sorted(a.domain for a in accounts) == ["lincolnmanufacturing.com", "mewbourne.com"]
    maker = next(a for a in accounts if a.domain == "lincolnmanufacturing.com")
    assert maker.industry == "Oil and Gas"
    assert maker.custom_fields["industry_evidence"]["code"] == 901
    assert not discarded


async def test_off_asks_nothing(monkeypatch):
    accounts, _, calls = await _deliver(monkeypatch, verdicts={}, mode="off")

    assert len(accounts) == 2 and calls == []


async def test_no_verdict_keeps_the_source_label_when_on(monkeypatch):
    """A site that cannot be read is no evidence either way: the source's label stands and the
    mandatory-field gate decides on it, rather than dropping a company for a slow website."""
    accounts, discarded, _ = await _deliver(monkeypatch, verdicts={"mewbourne.com": 3095})

    assert sorted(a.domain for a in accounts) == ["lincolnmanufacturing.com", "mewbourne.com"]
    maker = next(a for a in accounts if a.domain == "lincolnmanufacturing.com")
    assert maker.industry == "Oil and Gas"
    assert maker.field_sources["industry"]["source"] == "linkedin"
