# tests/test_discovery_geo_gate.py
"""A country the ICP excludes is a definitive non-match, so discovery must not surface it.

Reported: UK organisations appearing for a workspace whose ICP names the USA. Two causes, and only
the second is a defect:

* one workspace's ICP genuinely listed seven countries including the UK — the ICP said so;
* another had "Google DeepMind" (United Kingdom) against `countries: ["United States"]`, where geo
  scored 0.0 but only LOWERED the fit. Measured on the live engine, that account scores **75** at
  default weights, because industry (0.35), size (0.30) and tech (0.20) together outweigh geo
  (0.15). 75 reads as a decent fit.

Scoring it down is not enough when the answer is "we should not be able to find such companies".
So geo joins `_within_size_band` as a HARD gate at discovery, with identical semantics — which is
the point: the rule already exists for headcount and its shape is already argued.

    known value outside the stated set  ->  definitive non-match, never persisted
    unknown value                       ->  kept, and ranked rather than excluded
    ICP that names no countries         ->  everything passes

The unknown-is-kept half is load-bearing. Search rarely returns a country, and excluding on missing
data would discard candidates for a field we simply have not fetched — the same argument the size
gate makes, and the same one `score_icp_fit` makes when it treats a NULL as neutral.
"""
from __future__ import annotations

import pytest



def _icp(countries=None, **extra):
    icp = {"industries": ["Software & SaaS"], "employee_min": 10, "employee_max": 100000}
    if countries is not None:
        icp["countries"] = countries
    icp.update(extra)
    return icp


# ---- the gate ------------------------------------------------------------------------------------

@pytest.mark.parametrize("country,allowed", [
    ("United Kingdom", False),
    ("India", False),
    ("United States", True),
    # Case and spacing are what an enricher actually returns, not what a form validated.
    ("united states", True),
    ("  United States  ", True),
])
def test_a_known_country_outside_the_icp_is_a_definitive_non_match(country, allowed):
    from nexus.discovery.auto import _within_geo

    assert _within_geo(country, _icp(["United States"])) is allowed


def test_an_unknown_country_is_kept():
    """Search rarely returns a country. Excluding on missing data would discard candidates for a
    field we have not fetched — the same rule the size gate and `score_icp_fit` both follow."""
    from nexus.discovery.auto import _within_geo

    assert _within_geo(None, _icp(["United States"])) is True
    assert _within_geo("", _icp(["United States"])) is True


def test_an_icp_with_no_countries_admits_everything():
    """A workspace that has not stated a geography must see exactly what it saw before this gate
    existed."""
    from nexus.discovery.auto import _within_geo

    assert _within_geo("United Kingdom", _icp()) is True
    assert _within_geo("United Kingdom", _icp([])) is True


def test_common_country_aliases_are_not_treated_as_different_countries():
    """An enricher writes "USA", "US" or "United States" for the same place. Treating them as
    different would exclude a perfectly good US company from a US-only ICP — the gate failing in
    the direction it least can afford, since the whole point is to be trusted enough to exclude."""
    from nexus.discovery.auto import _within_geo

    for stored in ("USA", "US", "U.S.", "United States of America"):
        assert _within_geo(stored, _icp(["United States"])) is True, stored
    for stored in ("UK", "United Kingdom", "Great Britain"):
        assert _within_geo(stored, _icp(["United Kingdom"])) is True, stored
    # And the alias table must not collapse genuinely different countries.
    assert _within_geo("United Kingdom", _icp(["United States"])) is False


# ---- it is actually wired into discovery ----------------------------------------------------------

def test_discovery_applies_the_geo_gate_beside_the_size_gate():
    """Structural: the gate has to run in the persist loop, next to `_within_size_band`. A helper
    that exists and is never called is the failure this codebase keeps finding."""
    import inspect

    from nexus.discovery import auto

    src = inspect.getsource(auto)
    assert "_within_geo(" in src
    persist = src[src.index("# 3) Hard size-band gate"):]
    assert "_within_geo(" in persist, "the geo gate is defined but not applied where accounts persist"


def test_the_rescreen_also_archives_an_out_of_geo_account():
    """Discovery's gate runs on incomplete data, so a candidate with no known country is admitted
    and only proves out-of-geo once the refresh crawl fills it in. Without the re-screen it lingers
    in the rep's list forever — exactly the argument that already justifies the size re-screen."""
    import inspect

    from nexus.discovery.auto import rescreen_discovered_account

    src = inspect.getsource(rescreen_discovered_account)
    assert "_within_geo(" in src, "an account that proves out-of-geo after enrichment is never re-screened"
