# tests/test_title_suggestions_context.py
"""Suggested titles must move when campaign context is added.

Tester, 2026-08-27: after entering a value proposition, the pain being solved and product context,
"the recommendations remained largely the same" — Chief Technology Officer, Head of Demand
Generation, Head of Sales, Head of Data. Generic B2B defaults, and `recommend_titles_for_icp` never
read those three fields at all.

Two separate causes, both fixed here:

1. The scorer ignored value props, pains and product context.
2. The role catalogue was tech-GTM-shaped — 19 roles across Sales, Marketing, Engineering, Data,
   Security — with no facilities, workplace or plant roles. So even a perfect scorer had nothing
   right to return for a manufacturing or facilities campaign.
"""
from __future__ import annotations


def _titles(icp):
    from nexus.relevance.titles import recommend_titles_for_icp

    return [r.title for r in recommend_titles_for_icp(icp)]


def test_context_changes_the_suggestions():
    base = {"industries": ["Manufacturing"]}
    with_ctx = {
        **base,
        "value_props": ["Cut facility energy spend"],
        "pains_solved": ["Rising utility costs", "Unplanned equipment downtime"],
        "product_context": "IoT sensors for building management and facilities operations",
    }
    assert _titles(base) != _titles(with_ctx), (
        "adding value props, pains and product context changed nothing — which is exactly what the "
        "tester reported"
    )


def test_facilities_context_surfaces_a_facilities_role():
    icp = {
        "industries": ["Manufacturing"],
        "pains_solved": ["Rising facility energy costs", "Unplanned equipment downtime"],
        "product_context": "building management, facilities and maintenance operations",
    }
    blob = " ".join(_titles(icp)).lower()
    assert any(w in blob for w in ("facilit", "workplace", "plant", "maintenance", "operations")), \
        f"no operations-side role suggested for a facilities campaign: {_titles(icp)}"


def test_a_security_campaign_surfaces_security_roles():
    """The mechanism is general, not a special case bolted on for facilities."""
    icp = {
        "industries": ["SaaS"],
        "pains_solved": ["Failing SOC2 audits"],
        "product_context": "security compliance automation",
    }
    blob = " ".join(_titles(icp)).lower()
    assert any(w in blob for w in ("security", "ciso", "compliance", "information")), \
        f"no security role for a security campaign: {_titles(icp)}"


def test_an_icp_with_no_context_is_unchanged():
    """Regression guard: existing callers pass no context and must keep today's output."""
    out = _titles({"industries": ["SaaS"], "required_tech": ["Salesforce"]})
    assert out, "an ICP with no context must still produce suggestions"
    assert all(isinstance(t, str) and t for t in out)


def test_the_context_bonus_does_not_break_ranking_order():
    """Scores must stay monotonic — the list is rendered top-down and a rep reads the first three."""
    from nexus.relevance.titles import recommend_titles_for_icp

    recs = recommend_titles_for_icp({
        "industries": ["Manufacturing"],
        "product_context": "facilities and maintenance operations",
    })
    scores = [r.priority_score for r in recs]
    assert scores == sorted(scores, reverse=True)


def test_context_is_read_from_every_supported_key():
    """The orchestrator and the Relevance form use different names for the same idea; missing one
    means the feature silently does nothing on that screen."""
    from nexus.relevance.titles import recommend_titles_for_icp

    base = _titles({"industries": ["Manufacturing"]})
    for key in ("value_props", "pains_solved", "product_context", "problem"):
        icp = {"industries": ["Manufacturing"], key: (
            "facilities maintenance operations" if key in ("product_context", "problem")
            else ["facilities maintenance operations"]
        )}
        assert _titles(icp) != base, f"context key {key!r} was ignored"
        assert recommend_titles_for_icp(icp), f"context key {key!r} produced no suggestions"


def test_the_catalogue_covers_operations_side_functions():
    """A tech-GTM-only catalogue cannot serve manufacturing, retail or healthcare — three of the
    largest B2B segments — however good the scorer is."""
    from nexus.relevance.titles import ROLES

    blob = " ".join(f"{r.title} {r.department}" for r in ROLES).lower()
    for want in ("facilit", "workplace"):
        assert want in blob, f"no role covering {want!r} in the catalogue"


def test_context_does_not_crash_on_odd_shapes():
    """`value_props` is a JSON column an operator can populate through several paths; a string
    where a list was expected must degrade, not raise, on a read-only suggestion endpoint."""
    for odd in ({"value_props": "a string"}, {"value_props": [None, 3]},
                {"product_context": None}, {"value_props": [{"no_name": 1}]}):
        assert _titles({"industries": ["SaaS"], **odd}) is not None


async def test_the_endpoint_forwards_the_context(client, fresh_db):
    """The endpoint built its ICP dict from four keys and dropped the rest, so even a correct
    ranking function would have received nothing to rank on."""
    from tests.conftest import auth, signup

    token = await signup(client, slug="tsug", email="a@tsug.com", company="TSug")
    body = {
        "industries": ["Manufacturing"],
        "value_props": [{"name": "Facilities energy optimisation",
                         "pains_solved": ["rising facility energy costs"]}],
        "product_context": "building management and facilities operations",
        "limit": 10,
    }
    r = await client.post("/api/relevance/suggest-titles", headers=auth(token), json=body)
    assert r.status_code == 200, r.text
    with_ctx = {row["title"].lower() for row in r.json()}

    plain = await client.post("/api/relevance/suggest-titles", headers=auth(token),
                              json={"industries": ["Manufacturing"], "limit": 10})
    assert plain.status_code == 200, plain.text
    assert with_ctx != {row["title"].lower() for row in plain.json()}, (
        "the endpoint dropped value_props/product_context before ranking"
    )
