"""The Relevance Engine: deterministic ICP scoring and prompt grounding."""
from __future__ import annotations

from nexus.models.account import Account
from nexus.models.relevance import RelevanceProfile
from nexus.relevance.engine import get_relevance_engine

ENGINE = get_relevance_engine()


def _profile() -> RelevanceProfile:
    return RelevanceProfile(
        tenant_id="t",
        icp={
            "industries": ["Software"],
            "employee_min": 100,
            "employee_max": 1000,
            "countries": ["US"],
            "required_tech": ["snowflake"],
        },
        value_props=[{"name": "Faster GTM", "description": "x", "pains_solved": ["slow pipeline"]}],
        product_context="We sell a GTM intelligence platform.",
    )


def test_perfect_fit_scores_high():
    acc = Account(
        tenant_id="t", name="Acme", industry="Software",
        employee_count=500, country="US", tech_stack=["snowflake"],
    )
    fit = ENGINE.score_icp_fit(_profile(), acc)
    assert fit.score == 100


def test_poor_fit_scores_low():
    acc = Account(
        tenant_id="t", name="Misfit", industry="Agriculture",
        employee_count=5, country="DE", tech_stack=[],
    )
    fit = ENGINE.score_icp_fit(_profile(), acc)
    assert fit.score < 30


def test_no_profile_is_neutral():
    acc = Account(tenant_id="t", name="X", industry="Software")
    fit = ENGINE.score_icp_fit(None, acc)
    assert fit.score == 50


def test_scoring_is_deterministic():
    acc = Account(
        tenant_id="t", name="Acme", industry="Software",
        employee_count=500, country="US", tech_stack=["snowflake"],
    )
    a = ENGINE.score_icp_fit(_profile(), acc)
    b = ENGINE.score_icp_fit(_profile(), acc)
    assert a.score == b.score and a.breakdown == b.breakdown


def test_context_prompt_includes_grounding():
    acc = Account(tenant_id="t", name="Acme", industry="Software", employee_count=500, country="US")
    ctx = ENGINE.build_context(_profile(), acc)
    prompt = ctx.to_prompt()
    assert "GTM intelligence platform" in prompt
    assert "Faster GTM" in prompt
    assert "ACCOUNT ICP FIT" in prompt
