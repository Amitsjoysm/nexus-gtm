"""Seed↔candidate similarity scoring — pure, deterministic, offline.

These cover the heart of the improved look-alike feature: how closely a candidate company (or
person) resembles the seed across industry / sub-industry / geo / size / revenue / tech / keywords
(companies) and title / seniority / department / company (people). The functions are pure (no I/O,
no LLM), so the tests assert concrete scores, relative ordering, partial-credit behavior, and the
human-readable reasons.
"""
from __future__ import annotations

from nexus.lookalike.similarity import (
    company_similarity,
    contact_similarity,
    parse_revenue,
)
from nexus.models.account import Account, Contact


def _acc(**kw) -> Account:
    """Build a transient Account (no DB) with firmographics + custom_fields for scoring."""
    cf = kw.pop("custom_fields", None)
    a = Account(tenant_id="t", name=kw.pop("name", "Co"))
    for k, v in kw.items():
        setattr(a, k, v)
    if cf is not None:
        a.custom_fields = cf
    return a


def _contact(**kw) -> Contact:
    c = Contact(tenant_id="t", account_id=kw.pop("account_id", "a"), full_name=kw.pop("full_name", "X"))
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ---- revenue parsing -------------------------------------------------------------------------

def test_parse_revenue_handles_common_shapes():
    assert parse_revenue("$10M") == 10_000_000
    assert parse_revenue("1.2B") == 1_200_000_000
    assert parse_revenue("$10 million") == 10_000_000
    assert parse_revenue(5_000_000) == 5_000_000
    # "10-50M" → midpoint 30M (both numbers inherit the trailing unit)
    assert parse_revenue("10-50M") == 30_000_000


def test_parse_revenue_rejects_garbage():
    assert parse_revenue(None) is None
    assert parse_revenue("") is None
    assert parse_revenue("private") is None
    assert parse_revenue(0) is None
    assert parse_revenue(-3) is None


# ---- company similarity ----------------------------------------------------------------------

def test_near_identical_companies_score_very_high():
    seed = _acc(
        name="Acme Payments", industry="Fintech", employee_count=200, country="USA",
        tech_stack=["AWS", "Stripe", "React"],
        custom_fields={"sub_industry": "payments", "revenue": "$50M", "region": "California",
                       "city": "San Francisco", "keywords": ["corporate cards", "spend"]},
    )
    twin = _acc(
        name="Globex Spend", industry="Fintech", employee_count=250, country="USA",
        tech_stack=["AWS", "Stripe", "Vue"],
        custom_fields={"sub_industry": "payments", "revenue": "$60M", "region": "California",
                       "city": "San Francisco", "keywords": ["corporate cards", "expense"]},
    )
    res = company_similarity(seed, twin)
    assert res.score >= 80
    joined = " ".join(res.reasons).lower()
    assert "same industry" in joined
    assert "same city" in joined


def test_close_company_outranks_distant_company():
    seed = _acc(
        name="Acme Payments", industry="Fintech", employee_count=200, country="USA",
        tech_stack=["AWS", "Stripe"],
        custom_fields={"sub_industry": "payments", "revenue": "$50M", "city": "San Francisco",
                       "region": "California", "keywords": ["corporate cards"]},
    )
    close = _acc(
        name="Brex-like", industry="Fintech", employee_count=180, country="USA",
        tech_stack=["AWS", "Stripe"],
        custom_fields={"sub_industry": "payments", "revenue": "$40M", "city": "San Francisco",
                       "region": "California", "keywords": ["corporate cards"]},
    )
    distant = _acc(
        name="Tractor Supply", industry="Agriculture", employee_count=12000, country="India",
        tech_stack=["SAP"],
        custom_fields={"sub_industry": "farm equipment", "revenue": "$3B", "city": "Pune",
                       "region": "Maharashtra", "keywords": ["tractors"]},
    )
    close_score = company_similarity(seed, close).score
    distant_score = company_similarity(seed, distant).score
    assert close_score > distant_score
    assert close_score >= 80
    assert distant_score <= 40


def test_partial_data_is_not_punished_weights_renormalize():
    # Only two dims are comparable: industry (exact match) and keywords (name+industry tokens).
    # Weights renormalize over just those, so a sparse candidate isn't dragged toward 0 — an exact
    # industry match with differing names still lands as a solid (not weak) score.
    seed = _acc(name="Acme", industry="Fintech")
    cand = _acc(name="Globex", industry="Fintech")
    assert company_similarity(seed, cand).score >= 70


def test_completely_dissimilar_companies_score_low():
    # Different names AND no shared tokens → keywords dimension is 0; nothing else comparable.
    seed = _acc(name="Acme")
    cand = _acc(name="ZZZ Holdings")
    assert company_similarity(seed, cand).score <= 20


def test_no_comparable_data_returns_neutral():
    # Truly empty accounts (no name, industry, or custom_fields) → no dimension to compare → 50.
    seed = Account(tenant_id="t")
    cand = Account(tenant_id="t")
    res = company_similarity(seed, cand)
    assert res.score == 50
    assert "neutral" in " ".join(res.reasons).lower()


def test_geo_grading_city_beats_region_beats_country():
    seed = _acc(name="S", country="USA",
                custom_fields={"city": "Austin", "region": "Texas"})
    same_city = _acc(name="A", country="USA", custom_fields={"city": "Austin", "region": "Texas"})
    same_region = _acc(name="B", country="USA", custom_fields={"city": "Dallas", "region": "Texas"})
    same_country = _acc(name="C", country="USA", custom_fields={"city": "Miami", "region": "Florida"})
    diff_country = _acc(name="D", country="Canada", custom_fields={"city": "Toronto", "region": "Ontario"})
    c = company_similarity(seed, same_city).breakdown["geo"]
    r = company_similarity(seed, same_region).breakdown["geo"]
    n = company_similarity(seed, same_country).breakdown["geo"]
    f = company_similarity(seed, diff_country).breakdown["geo"]
    assert c > r > n > f
    assert (c, r, n, f) == (1.0, 0.75, 0.45, 0.0)


def test_size_similarity_decays_with_distance():
    seed = _acc(name="S", employee_count=200)
    near = company_similarity(seed, _acc(name="N", employee_count=220)).breakdown["size"]
    far = company_similarity(seed, _acc(name="F", employee_count=15000)).breakdown["size"]
    assert near > far
    assert near >= 0.9
    assert far <= 0.1


# ---- contact similarity ----------------------------------------------------------------------

def test_same_role_seniority_department_scores_high():
    seed = _contact(full_name="Jane", title="VP of Sales", seniority="vp")
    twin = _contact(full_name="John", title="VP of Sales", seniority="vp")
    res = contact_similarity(seed, twin, company_sim=0.9)
    assert res.score >= 85
    joined = " ".join(res.reasons).lower()
    assert "same title" in joined
    assert "similar company" in joined


def test_similar_role_outranks_unrelated_role():
    seed = _contact(full_name="Jane", title="VP of Sales", seniority="vp")
    similar = _contact(full_name="Sam", title="Director of Sales", seniority="director")
    unrelated = _contact(full_name="Pat", title="Junior Software Engineer", seniority="associate")
    s = contact_similarity(seed, similar, company_sim=0.8).score
    u = contact_similarity(seed, unrelated, company_sim=0.8).score
    assert s > u


def test_contact_company_similarity_moves_the_score():
    seed = _contact(full_name="Jane", title="Head of Marketing", seniority="head")
    cand = _contact(full_name="Lee", title="Head of Marketing", seniority="head")
    high = contact_similarity(seed, cand, company_sim=0.95).score
    low = contact_similarity(seed, cand, company_sim=0.05).score
    assert high > low


def test_contact_with_no_overlap_is_neutral():
    seed = _contact(full_name="Jane")  # no title/seniority
    cand = _contact(full_name="Bob")
    res = contact_similarity(seed, cand)  # no company_sim either
    assert res.score == 50
