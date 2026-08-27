# tests/test_relevance_geo_revenue.py
"""Region, postal-code and revenue scoring.

A tester evaluating the product found Country was the only geographic filter — no State/Province, no
ZIP — and no revenue filter at all. Both are table stakes for territory-based GTM.

Every one of these is **absent by default**: an ICP that does not mention them must score exactly as
it did before they existed. That is the first test in this file because it is the one protecting
every existing workspace.
"""
from __future__ import annotations


def _account(**kw):
    from nexus.models.account import Account

    return Account(tenant_id="t1", name=kw.pop("name", "Acme"), **kw)


def _profile(icp):
    from nexus.models.relevance import RelevanceProfile

    return RelevanceProfile(tenant_id="t1", icp=icp)


def _fit(icp, account):
    from nexus.relevance.engine import RelevanceEngine

    return RelevanceEngine().score_icp_fit(_profile(icp), account)


# ---- the compatibility line -------------------------------------------------------------------

def test_an_icp_that_names_none_of_them_scores_exactly_as_before():
    """THE regression guard. Adding three dimensions must not move an existing workspace's scores."""
    icp = {"industries": ["SaaS"], "countries": ["United States"]}
    fit = _fit(icp, _account(industry="SaaS", country="United States", employee_count=200))
    for key in ("region", "postal", "revenue"):
        assert key not in fit.breakdown, f"{key} must not be scored when the ICP does not ask"


def test_the_score_is_identical_with_and_without_the_new_columns_populated():
    """An account carrying region/postal/revenue data must not score differently while the ICP
    ignores those fields — otherwise a CSV import would silently reshuffle the book."""
    icp = {"industries": ["SaaS"], "countries": ["United States"]}
    bare = _fit(icp, _account(industry="SaaS", country="United States", employee_count=200))
    rich = _fit(icp, _account(industry="SaaS", country="United States", employee_count=200,
                              region="California", postal_code="94107", annual_revenue=25_000_000))
    assert bare.score == rich.score


# ---- region -------------------------------------------------------------------------------------

def test_region_matches_case_insensitively():
    fit = _fit({"regions": ["California"]}, _account(region="california"))
    assert fit.breakdown["region"] == 1.0


def test_an_unknown_region_is_neutral_not_a_miss():
    fit = _fit({"regions": ["California"]}, _account(region=None))
    assert fit.breakdown["region"] == 0.5, "an un-enriched account must not be punished"


def test_a_wrong_region_is_a_miss():
    fit = _fit({"regions": ["California"]}, _account(region="Texas"))
    assert fit.breakdown["region"] == 0.0


# ---- postal code --------------------------------------------------------------------------------

def test_postal_code_matches_on_a_prefix():
    """GTM teams target areas, not single codes: '941' must catch 94107 and 94110."""
    assert _fit({"postal_codes": ["941"]}, _account(postal_code="94107")).breakdown["postal"] == 1.0
    assert _fit({"postal_codes": ["941"]}, _account(postal_code="94110")).breakdown["postal"] == 1.0


def test_a_postal_outside_the_area_is_a_miss():
    assert _fit({"postal_codes": ["941"]}, _account(postal_code="10001")).breakdown["postal"] == 0.0


def test_an_unknown_postal_is_neutral():
    assert _fit({"postal_codes": ["941"]}, _account(postal_code=None)).breakdown["postal"] == 0.5


# ---- revenue ------------------------------------------------------------------------------------

def test_revenue_inside_the_band_scores_full():
    icp = {"revenue_min": 10_000_000, "revenue_max": 100_000_000}
    assert _fit(icp, _account(annual_revenue=25_000_000)).breakdown["revenue"] == 1.0


def test_revenue_outside_the_band_scores_below_full():
    icp = {"revenue_min": 10_000_000, "revenue_max": 100_000_000}
    assert _fit(icp, _account(annual_revenue=500_000)).breakdown["revenue"] < 1.0


def test_unknown_revenue_is_neutral():
    """Same rule as unknown tech: 'we have not fetched it' is not evidence against the account."""
    assert _fit({"revenue_min": 10_000_000}, _account(annual_revenue=None)).breakdown["revenue"] == 0.5


def test_a_revenue_floor_alone_works():
    """An ICP naming only a minimum is the common case — 'companies above $10M'."""
    icp = {"revenue_min": 10_000_000}
    assert _fit(icp, _account(annual_revenue=50_000_000)).breakdown["revenue"] == 1.0


# ---- weighting ----------------------------------------------------------------------------------

def test_geography_cannot_outvote_industry_and_size_together():
    """Region and postal refine a geography `geo` already scores. At full weight a single ICP could
    let location outvote the two dimensions that actually define an ICP."""
    from nexus.relevance.engine import DEFAULT_WEIGHTS

    geographic = DEFAULT_WEIGHTS["geo"] + DEFAULT_WEIGHTS["region"] + DEFAULT_WEIGHTS["postal"]
    assert geographic < DEFAULT_WEIGHTS["industry"] + DEFAULT_WEIGHTS["size"]
