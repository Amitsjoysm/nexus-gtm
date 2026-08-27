# tests/test_relevance_unknown_tech.py
"""'We do not know their stack' and 'they do not use it' are different facts.

Reported by a tester 2026-08-27: adding *any* tech to the ICP took account results to **zero**, in
the Accounts view and through the Orchestrator alike. They suspected the ICP conditions were acting
as hard filters. They were right about the effect and the mechanism is worse than a filter.

`RelevanceEngine.score` computed ``sub["tech"] = len(hits) / len(required_tech)``. A freshly
discovered account has an **empty** ``tech_stack`` — nothing has enriched it yet — so it scored
``0.0``, which is the identical value a confirmed mismatch gets. ``nexus/discovery/auto.py`` then
drops anything under ``min_fit``, so requiring tech eliminated every candidate *before* enrichment
could ever populate the field. The data that would have qualified them is fetched after the gate
they just failed.

Every other dimension in this engine already treats unknown as neutral: no industries in the ICP
scores 0.5, no countries scores 0.5. Tech was the outlier, and it is the one dimension whose data
arrives last.
"""
from __future__ import annotations


def _account(**kw):
    from nexus.models.account import Account

    return Account(tenant_id="t1", name=kw.pop("name", "Acme"), **kw)


def _profile(icp):
    from nexus.models.relevance import RelevanceProfile

    return RelevanceProfile(tenant_id="t1", icp=icp)


def test_an_account_with_an_unknown_stack_is_not_scored_as_a_miss():
    from nexus.relevance.engine import RelevanceEngine

    icp = {"required_tech": ["salesforce", "hubspot"]}
    fit = RelevanceEngine().score_icp_fit(_profile(icp), _account(tech_stack=[]))
    assert fit.breakdown["tech"] == 0.5, (
        f"an unknown stack scored {fit.breakdown['tech']} — 0.0 puts every un-enriched account "
        f"under the discovery min_fit gate, which is why adding tech returned zero accounts"
    )


def test_a_confirmed_miss_still_scores_zero():
    """The neutral treatment must not hide a real mismatch. We know this account's stack, and the
    required tech is not in it — that is a genuine non-fit and must stay one, or the filter stops
    meaning anything at all."""
    from nexus.relevance.engine import RelevanceEngine

    icp = {"required_tech": ["salesforce"]}
    fit = RelevanceEngine().score_icp_fit(
        _profile(icp), _account(tech_stack=["pipedrive", "intercom"])
    )
    assert fit.breakdown["tech"] == 0.0


def test_a_partial_match_is_unchanged():
    from nexus.relevance.engine import RelevanceEngine

    icp = {"required_tech": ["salesforce", "hubspot"]}
    fit = RelevanceEngine().score_icp_fit(_profile(icp), _account(tech_stack=["salesforce"]))
    assert fit.breakdown["tech"] == 0.5


def test_no_required_tech_is_unchanged():
    """Regression guard on the pre-existing neutral-when-unspecified behaviour."""
    from nexus.relevance.engine import RelevanceEngine

    # NOT `{}` -- an empty ICP short-circuits to "no ICP defined" with an empty breakdown, so it
    # would assert nothing. This is an ICP that is real but says nothing about tech.
    fit = RelevanceEngine().score_icp_fit(
        _profile({"industries": ["SaaS"]}), _account(tech_stack=["salesforce"])
    )
    assert fit.breakdown["tech"] == 0.5


def test_an_unknown_stack_does_not_sink_an_otherwise_perfect_account():
    """The end-to-end shape of the bug, stated as a score rather than a sub-score.

    An account matching industry, size and geo exactly, whose stack simply has not been fetched
    yet, must not land near the bottom purely because the ICP mentions tech.
    """
    from nexus.relevance.engine import RelevanceEngine

    icp = {
        "industries": ["SaaS"],
        "countries": ["United States"],
        "employee_min": 50,
        "employee_max": 500,
        "required_tech": ["salesforce"],
    }
    acct = _account(
        industry="SaaS", country="United States", employee_count=200, tech_stack=[]
    )
    fit = RelevanceEngine().score_icp_fit(_profile(icp), acct)
    assert fit.score >= 85, (
        f"a perfect firmographic match with an un-fetched stack scored {fit.score}; discovery's "
        f"min_fit gate drops it and enrichment never runs"
    )
