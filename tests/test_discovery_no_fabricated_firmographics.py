# tests/test_discovery_no_fabricated_firmographics.py
"""Discovery must not stamp the ICP's own answers onto a candidate it has not verified.

`auto_discover_for_tenant` built each candidate with
``industry=cand.industry or fallback_industry`` where ``fallback_industry`` is
``icp["industries"][0]``. Two things then compound:

* `SearchBackedAccountEnricher.apply` fills BLANK fields only — deliberately, so a tenant's CRM
  data is never overwritten — so the real industry the web crawl found is discarded.
* `score_icp_fit` awards the full industry weight (0.35) for a value the ICP itself supplied,
  which makes the industry component of "strict ICP fit" unconditionally true.

Measured live 2026-08-27 against the Infojoy workspace: ArcelorMittal Tailored Blanks (a steel
supplier, per its own enriched description) and Gerdau (a steelmaker) were both stored as
``Software & SaaS`` and returned by the orchestrator at fit 80 with the reason
"industry 'Software & SaaS' is in ICP".
"""
from __future__ import annotations

import ast
from pathlib import Path


def _discovery_source() -> str:
    return (Path(__file__).resolve().parents[1] / "nexus/discovery/auto.py").read_text(
        encoding="utf-8"
    )


def test_candidates_are_not_stamped_with_the_icps_own_industry_or_geo():
    """The Account(...) built per candidate must not fall back to an ICP-derived value."""
    tree = ast.parse(_discovery_source())
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Account"
    ]
    assert constructions, "no Account(...) construction found; did discovery move?"
    offenders = []
    for call in constructions:
        for kw in call.keywords:
            if kw.arg not in ("industry", "country"):
                continue
            # `x or fallback_y` is the fabrication; a bare `cand.x` is fine.
            if isinstance(kw.value, ast.BoolOp) and isinstance(kw.value.op, ast.Or):
                names = [
                    n.id for n in ast.walk(kw.value) if isinstance(n, ast.Name)
                ]
                if any(n.startswith("fallback") for n in names):
                    offenders.append(kw.arg)
    assert not offenders, (
        f"discovery fabricates {sorted(set(offenders))} from the ICP. Enrichment cannot correct "
        "it (apply() fills blanks only), so score_icp_fit scores the ICP against itself."
    )


def test_the_icp_derived_fallbacks_are_gone_entirely():
    """A leftover `fallback_industry = icp[...][0]` is the bug waiting to be re-wired."""
    src = _discovery_source()
    for name in ("fallback_industry", "fallback_country"):
        assert name not in src, (
            f"{name} still exists in nexus/discovery/auto.py; remove it so it cannot be "
            "reattached to a candidate."
        )
