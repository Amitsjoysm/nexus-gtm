"""The ICP's industries become LinkedIn industry codes, once, cheaply, and never by a naive match.

The company-search actor filters on LinkedIn's numeric industry codes (434 of them, v2). An ICP says
"Software & SaaS" or "Fintech". Measured against the real list: "SaaS", "fintech", "cyber",
"e-commerce" and "AI" match no label at all, and a substring match on "artificial" maps "Artificial
Intelligence" to *Artificial Rubber and Synthetic Fiber Manufacturing*. So the mapping is whole-word
and synonym-led first, and the LLM is asked only for what that misses — from a short list, never
all 434 with their descriptions.
"""
from __future__ import annotations

import pytest

from nexus.prospecting.industries import (
    all_industries,
    descendants,
    map_industries,
    match_term,
    shortlist_for,
)


def test_the_full_list_is_available():
    industries = all_industries()
    assert len(industries) == 434
    assert industries[4].label == "Software Development"
    assert "Technology, Information and Internet" in industries[4].hierarchy


@pytest.mark.parametrize("term, expected", [
    ("Software Development", [4]),          # exact label
    ("SaaS", [4]),                          # no label says SaaS
    ("software", [4]),
    ("Cybersecurity", [118]),
    ("E-commerce", [1445]),
    ("Financial Services", [43]),
    ("insurance", [42]),
    ("Staffing and Recruiting", [104]),
    ("health care", [14]),                  # the top-level one, not Home Health Care Services
])
def test_common_icp_terms_map_deterministically(term, expected):
    assert match_term(term) == expected


def test_artificial_intelligence_is_not_rubber_manufacturing():
    ids = match_term("Artificial Intelligence")
    assert 703 not in ids, "'artificial' substring-matched Artificial Rubber manufacturing"
    assert 4 in ids


def test_a_compound_icp_value_splits_on_ampersand_but_not_on_and():
    # "Software & SaaS" is two terms; "Food and Beverage Services" is one label.
    assert match_term("Food and Beverage Services") == [34]


async def test_mapping_a_whole_icp_dedupes_and_keeps_order():
    assert await map_industries(["Software & SaaS", "Cybersecurity", "software"]) == [4, 118]


def test_a_parent_code_covers_its_children_for_database_matching():
    below = descendants([43])
    assert {43, 129, 45}.issubset(below), "Capital Markets and Investment Banking sit under 43"
    assert 4 not in below


# ---- "Technology" is tech companies, not publishers (2026-10-15) ---------------------------------
#
# Measured on the local deploy: daily discovery delivered CNN, The Washington Post, The Economist,
# TIME and Deadline to a workspace whose ICP says "Technology". The term matched LinkedIn's parent
# "Technology, Information and Media", whose children include newspapers and broadcasters.

#: Newspapers, periodicals, broadcasters, online video, internet news and blogs.
MEDIA = {81, 82, 36, 113, 3124, 3125}


def test_technology_means_tech_companies_not_publishers():
    ids = match_term("Technology")
    assert 1594 not in ids, "mapped to the parent that includes publishing and broadcasting"
    assert {4, 96} <= set(ids)
    assert not descendants(ids) & MEDIA


@pytest.mark.parametrize("term", ["tech", "Information Technology", "Technology Services"])
def test_other_technology_wordings_stay_out_of_media(term):
    ids = match_term(term)
    assert ids, f"{term!r} maps to nothing, so the LLM would guess"
    assert not descendants(ids) & MEDIA


def test_b2b_services_maps_to_professional_services():
    assert match_term("B2B Services") == [1810]


def test_a_parent_whose_label_has_an_ampersand_still_covers_its_children():
    """The vendored label is "Media & Telecommunications" and its children's paths say "Media and
    Telecommunications", so the parent covered nothing: an ICP naming it matched no company."""
    assert {81, 82, 36, 8} <= descendants([3133])


async def test_the_measured_icp_maps_without_any_media_code():
    async def no_llm(term, shortlist):
        raise AssertionError(f"{term!r} should map without the LLM")

    ids = await map_industries(
        ["Manufacturing", "SaaS / Software", "Healthcare", "Financial Services", "Technology",
         "Professional Services", "Energy", "B2B Services"], llm=no_llm)
    assert not descendants(ids) & MEDIA


async def test_the_llm_is_asked_only_for_misses_and_only_from_a_shortlist():
    asked: list[str] = []

    async def llm(term: str, shortlist):
        asked.append(term)
        valid = shortlist[0].id
        return f"[{valid}, 999999]"          # one real pick and one invented id

    ids = await map_industries(["SaaS", "developer tools platforms"], llm=llm)

    assert asked == ["developer tools platforms"], "the LLM was asked about a term the synonyms know"
    assert shortlist_for("developer tools platforms")[0].id in ids, "the LLM's shortlisted pick was lost"
    assert 999999 not in ids, "an id outside the shortlist was accepted"


async def test_a_broken_llm_costs_the_term_not_the_mapping():
    async def llm(term, shortlist):
        raise RuntimeError("provider down")

    assert await map_industries(["SaaS", "developer tools platforms"], llm=llm) == [4]


def test_the_shortlist_is_short_and_relevant():
    names = [i.label for i in shortlist_for("marketing automation software")]
    assert len(names) <= 20
    assert "Marketing Services" in names or "Software Development" in names


# ---- every vertical, not one (SDR feedback, 2026-10-26) ------------------------------------------
#
# An audit of ~170 terms an ICP names found single-word matches that file a whole vertical under a
# niche: whole-word containment picks the label with the fewest extra words, so "Shipping" became
# *Boilers, Tanks, and Shipping Container Manufacturing* and "Distribution" *Natural Gas
# Distribution*, and common words (Pharmaceuticals, Automotive, Telecom) went to the LLM, which
# answers differently from one save to the next. Each expected code below was read off LinkedIn's
# own tree (`nexus/data/linkedin_industries_v2.csv`).

@pytest.mark.parametrize("term, expected", [
    # were filed under a niche
    ("Shipping", [95, 87]),                 # Maritime; Freight and Package Transportation
    ("Distribution", [133]),                # Wholesale, not Natural Gas Distribution
    ("Consumer Goods", [23, 18, 727]),      # not Consumer Goods Rental
    ("Agriculture", [201]),                 # Farming, Ranching, Forestry
    ("Furniture", [26]),                    # not Reupholstery and Furniture Repair
    ("Beverages", [142]),                   # Beverage Manufacturing, not alcohol wholesale
    ("Energy", [57, 383, 382, 397]),        # oil and gas, power generation, grid, gas distribution
    ("Renewable Energy", [3240, 3243, 3241]),
    ("Transportation", [116]),              # the sector, not Truck Transportation
    ("Consulting", [11]),                   # Business Consulting and Services
    ("Engineering", [3242]),                # Engineering Services, not Civil Engineering
    ("Facilities", [122]),                  # Facilities Services, not Recreational Facilities
    ("Media", [82, 36, 35]),                # publishing, broadcast, film: not telecom carriers
    ("Paper", [61]),
    ("Metals", [807, 840]),
    # went to the LLM
    ("Pharmaceuticals", [15]), ("Pharma", [15]), ("Biotech", [12]),
    ("Medical Devices", [17]), ("Life Sciences", [12, 15, 17]),
    ("Automotive", [53, 1042]), ("Chemicals", [54]), ("Semiconductors", [7]),
    ("Telecom", [8]), ("Banks", [41]), ("Public Sector", [75]), ("Nonprofit", [100]),
    ("Management Consulting", [11, 102]), ("Law Firms", [9]), ("Textiles", [60]),
    ("Commercial Real Estate", [44]), ("Trucking", [92]), ("Steel", [807]),
    ("Video Games", [109, 3131]), ("Car Dealerships", [1292]), ("Dental", [2045]),
    ("Senior Living", [2091]), ("CPG", [23, 18, 727]), ("FMCG", [23, 18, 727]),
    ("B2B SaaS", [4]), ("Enterprise Software", [4]), ("K-12", [67]),
    # already right, and must stay so
    ("Oil & Gas", [57]), ("Manufacturing", [25]), ("Healthcare", [14]), ("Retail", [27]),
    ("Logistics", [116]), ("Construction", [48]), ("Real Estate", [44]), ("Hospitals", [2081]),
    ("Higher Education", [68]), ("Telecommunications", [8]), ("Mining", [56]),
    ("Utilities", [59]), ("Wholesale", [133]), ("Accounting", [47]), ("Restaurants", [32]),
])
def test_industries_a_person_names_map_to_what_they_mean(term, expected):
    assert match_term(term) == expected


async def test_no_common_vertical_needs_the_llm():
    async def no_llm(term, shortlist):
        raise AssertionError(f"{term!r} should map without the LLM")

    ids = await map_industries(
        ["Pharmaceuticals", "Automotive", "Chemicals", "Telecom", "Shipping & Logistics",
         "Food & Beverage", "Aerospace & Defense", "Energy", "Medical Devices"], llm=no_llm)
    assert 861 not in ids, "Shipping filed under container manufacturing"
    assert {95, 87, 116, 15, 53, 54, 8, 17} <= set(ids)


def test_the_mapping_has_a_version_so_stored_codes_can_be_known_stale():
    from nexus.prospecting.industries import MAPPING_VERSION

    assert isinstance(MAPPING_VERSION, int) and MAPPING_VERSION >= 2


async def test_a_saved_icp_mapped_by_an_older_version_is_mapped_again_on_save():
    """Saving with unchanged industries reused the stored codes, so a workspace whose "Shipping"
    was filed under container manufacturing kept it until somebody changed the words."""
    from nexus.api.routers.relevance import _with_linkedin_codes
    from nexus.prospecting.industries import MAPPING_VERSION

    saved = {"industries": ["Shipping"], "linkedin_industry_ids": [861]}
    icp = await _with_linkedin_codes({"industries": ["Shipping"]}, saved)
    assert icp["linkedin_industry_ids"] == [95, 87]
    assert icp["linkedin_industry_map_version"] == MAPPING_VERSION

    current = {**icp}
    again = await _with_linkedin_codes({"industries": ["Shipping"]}, current)
    assert again["linkedin_industry_ids"] == [95, 87]


async def test_the_search_maps_again_when_the_stored_codes_are_stale():
    from nexus.prospecting.companies import query_for_icp

    stale = await query_for_icp({"industries": ["Shipping"], "linkedin_industry_ids": [861]})
    assert stale.industry_ids == (95, 87)
    from nexus.prospecting.industries import MAPPING_VERSION

    fresh = await query_for_icp({"industries": ["Shipping"], "linkedin_industry_ids": [95],
                                 "linkedin_industry_map_version": MAPPING_VERSION})
    assert fresh.industry_ids == (95,), "current codes are used as stored, not mapped again"


async def test_the_script_maps_saved_icps_again_and_dry_run_writes_nothing(monkeypatch, capsys):
    """The fit score reads the codes stored on the ICP, so a workspace's saved codes from an older
    mapping are what its accounts are scored against until they are mapped again."""
    import sys

    from scripts import remap_icp_industries
    from nexus.models.relevance import RelevanceProfile
    from nexus.prospecting.industries import MAPPING_VERSION
    from tests.conftest import make_tenant, tenant_session

    tid = await make_tenant("remap1")
    async with tenant_session(tid) as ts:
        ts.add(RelevanceProfile(tenant_id=tid, icp={
            "industries": ["Shipping", "Energy"], "linkedin_industry_ids": [861, 3243]}))
    current = await make_tenant("remap2")
    async with tenant_session(current) as ts:
        ts.add(RelevanceProfile(tenant_id=current, icp={
            "industries": ["Retail"], "linkedin_industry_ids": [27],
            "linkedin_industry_map_version": MAPPING_VERSION}))

    async def icp_of(t):
        async with tenant_session(t) as ts:
            return (await ts.first(RelevanceProfile)).icp

    monkeypatch.setattr(sys, "argv", ["remap_icp_industries.py"])
    await remap_icp_industries.main()
    assert (await icp_of(tid))["linkedin_industry_ids"] == [861, 3243]
    out = capsys.readouterr().out
    assert "1 of" in out and "[861, 3243] -> [95, 87, 57, 383, 382, 397]" in out

    monkeypatch.setattr(sys, "argv", ["remap_icp_industries.py", "--apply"])
    await remap_icp_industries.main()
    icp = await icp_of(tid)
    assert icp["linkedin_industry_ids"] == [95, 87, 57, 383, 382, 397]
    assert icp["linkedin_industry_map_version"] == MAPPING_VERSION
    assert icp["industries"] == ["Shipping", "Energy"], "the words are never touched"
    assert (await icp_of(current))["linkedin_industry_ids"] == [27]
