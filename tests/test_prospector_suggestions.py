"""Typeahead for Prospector's text filters (asked 2026-11-19).

"Industries ... appear as we enter 2-3 letters, and in all other text based filters." A filter
matches what the provider stores, so its own values are learned from every page bought and offered
first, most seen first; seeds (LinkedIn industries, the title catalogue, countries, states,
departments, common tools) fill a field before anything is learned. Only business facts are kept:
never a person's name, email or phone, nor a domain or LinkedIn page, which are masked and paid for.
"""
from __future__ import annotations

import pytest

from nexus.prospector import vocabulary
from tests.conftest import auth, signup
from tests.prospector_fixtures import CONTACTS, FakeVendor, SECRETS


@pytest.fixture
def vendor():
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient

    fake = FakeVendor()
    search.set_client(InfoJoyClient(keys=["k"], transport=fake.transport(), backoff_s=0))
    yield fake
    search.set_client(None)


def test_a_page_yields_business_facts_and_no_personal_or_paid_value():
    seen = vocabulary.harvest("contacts", [
        {**CONTACTS[0], "IndustryType": "Software", "Technology": "Salesforce, HubSpot , NULL",
         "City": "San Francisco", "State": "Not Available"},
        {**CONTACTS[3], "IndustryType": "software"},
    ])
    fields = {field for field, _ in seen}
    assert {"title", "industry", "technology", "city", "company_name", "country"} <= fields
    assert ("technology", "HubSpot") in seen and ("technology", "NULL") not in seen
    assert not any(field == "state" for field, _ in seen)          # a placeholder is not a value
    text = " ".join(value for _, value in seen).lower()
    assert not [s for s in SECRETS if s.lower() in text], "a personal or paid value was kept"
    assert "john" not in text and "smith" not in text               # people's names never


async def test_learned_values_come_first_most_seen_first_then_seeds():
    await vocabulary.learn("contacts", [{"IndustryType": "Banking"}] * 3
                           + [{"IndustryType": "Investment Banking"}])
    values = await vocabulary.suggest("contacts", "industries", "bank")
    assert values[:2] == ["Banking", "Investment Banking"]
    assert len(values) <= vocabulary.DEFAULT_LIMIT
    # Seeds fill the rest, without repeating a learned value in another case.
    assert len({v.lower() for v in values}) == len(values)


async def test_values_starting_with_the_text_come_before_those_containing_it():
    await vocabulary.learn("contacts", [{"Title": "Head of Sales"}] * 5 + [{"Title": "Sales Manager"}])
    values = await vocabulary.suggest("contacts", "titles", "sal")
    assert values.index("Sales Manager") < values.index("Head of Sales")


async def test_one_letter_suggests_nothing_and_wildcards_are_literal():
    await vocabulary.learn("contacts", [{"City": "Austin"}])
    assert await vocabulary.suggest("contacts", "person_cities", "a") == []
    assert await vocabulary.suggest("contacts", "person_cities", "%%") == []


async def test_seeds_answer_before_anything_is_learned():
    assert "United Kingdom" in await vocabulary.suggest("contacts", "person_countries", "unit")
    assert any("Software" in v for v in await vocabulary.suggest("companies", "industries", "soft"))
    assert "Texas" in await vocabulary.suggest("contacts", "person_states", "tex")


async def test_a_search_teaches_the_vocabulary(client, vendor):
    token = await signup(client, slug="sg1", email="o@sg1.io", company="SG1")
    r = await client.post("/api/prospector/search", headers=auth(token),
                          json={"view": "contacts", "page_size": 25})
    assert r.status_code == 200, r.text
    assert "VP Sales" in await vocabulary.suggest("contacts", "titles", "vp s")


async def test_the_endpoint_answers_and_refuses_a_field_without_suggestions(client, vendor):
    token = await signup(client, slug="sg2", email="o@sg2.io", company="SG2")
    h = auth(token)
    ok = await client.get("/api/prospector/suggest", headers=h,
                          params={"view": "contacts", "field": "person_countries", "q": "can"})
    assert ok.status_code == 200, ok.text
    assert "Canada" in ok.json()["values"]
    domains = await client.get("/api/prospector/suggest", headers=h,
                               params={"view": "contacts", "field": "company_domains", "q": "ac"})
    assert domains.status_code == 422


async def test_the_catalog_marks_the_fields_that_suggest(client, vendor):
    token = await signup(client, slug="sg3", email="o@sg3.io", company="SG3")
    r = await client.get("/api/prospector/filters", headers=auth(token),
                         params={"view": "contacts"})
    fields = {f["key"]: f for s in r.json()["sections"] for f in s["fields"]}
    assert fields["titles"]["suggest"] and fields["industries"]["suggest"]
    assert fields["exclude_keywords"]["suggest"] and fields["person_cities"]["suggest"]
    assert not fields["company_domains"]["suggest"] and not fields["person_zips"]["suggest"]
    assert not fields["seniorities"]["suggest"]          # a checkbox list already


def test_the_chip_box_is_a_combobox_wired_into_the_rail():
    """No frontend test runner: the source is the contract. From two letters, keyboard and
    screen-reader usable, and the rail hands every field that suggests its lookup."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "frontend" / "src"
    chip = (root / "pages" / "prospector" / "ChipInput.tsx").read_text(encoding="utf-8")
    rail = (root / "pages" / "prospector" / "FilterRail.tsx").read_text(encoding="utf-8")
    assert "SUGGEST_MIN_CHARS = 2" in chip
    for attr in ('role: "combobox"', '"aria-activedescendant"', 'role="listbox"', 'role="option"'):
        assert attr in chip, attr
    assert "ArrowDown" in chip and "Escape" in chip
    # Only the typed text starts a lookup; a new function from the rail each render must not.
    assert "suggestRef" in chip and "[canSuggest, query, full]" in chip
    assert rail.count("suggest={suggestFor(field)}") == 2 and "prospectorSuggest" in rail
