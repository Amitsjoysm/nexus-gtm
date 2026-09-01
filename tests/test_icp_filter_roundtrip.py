# tests/test_icp_filter_roundtrip.py
"""The new ICP filters must survive a save and come back.

The engine has scored `regions`, `postal_codes` and `revenue_min/max` since migration 0051, and
`job_levels` / `title_keywords` have driven contact matching since job_levels.py -- but there was no
field to set any of them from, so a tester could filter on Country and nothing finer and had to
rely on exact buyer titles.

`profile.icp` is a JSON column, so a field the API schema drops is not an error: it is silently
absent on read, and the customer sees their filter "not stick" with nothing reporting a fault. That
is the failure this file exists to catch, and it is why the assertions read the value BACK rather
than trusting a 200.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def test_the_new_filters_round_trip(client, fresh_db):
    token = await signup(client, slug="icpf", email="a@icpf.com", company="ICPF")

    icp = {
        "industries": ["Manufacturing"],
        "countries": ["United States"],
        "regions": ["California", "Texas"],
        "postal_codes": ["941", "personal"],
        "revenue_min": 10_000_000,
        "revenue_max": 500_000_000,
        "employee_min": 50,
        "employee_max": 5000,
        "required_tech": ["Salesforce"],
        "buyer_titles": ["Facilities Director"],
        "job_levels": ["director", "head", "vp"],
        "title_keywords": ["facilities", "workplace"],
        "exclude_title_keywords": ["assistant", "deputy"],
    }
    saved = await client.put("/api/relevance/profile", headers=auth(token),
                             json={"icp": icp, "value_props": [], "product_context": ""})
    assert saved.status_code == 200, saved.text

    got = (await client.get("/api/relevance/profile", headers=auth(token))).json()["icp"]
    for key in ("regions", "postal_codes", "job_levels", "title_keywords",
                "exclude_title_keywords"):
        assert got.get(key) == icp[key], f"{key} did not survive the round trip: {got.get(key)!r}"
    assert got.get("revenue_min") == 10_000_000
    assert got.get("revenue_max") == 500_000_000


async def test_an_icp_without_the_new_keys_is_unchanged(client, fresh_db):
    """THE regression guard. Every existing workspace has none of these keys, and saving a profile
    without them must not invent them or drop what is already there."""
    token = await signup(client, slug="icpf2", email="a@icpf2.com", company="ICPF2")

    icp = {"industries": ["SaaS"], "countries": ["United States"], "employee_min": 10}
    await client.put("/api/relevance/profile", headers=auth(token),
                     json={"icp": icp, "value_props": [], "product_context": ""})

    got = (await client.get("/api/relevance/profile", headers=auth(token))).json()["icp"]
    assert got.get("industries") == ["SaaS"]
    assert got.get("employee_min") == 10
    for key in ("regions", "postal_codes", "job_levels", "title_keywords"):
        assert not got.get(key), f"{key} should be absent or empty, got {got.get(key)!r}"


async def test_the_saved_levels_actually_drive_matching(client, fresh_db):
    """A filter that saves but changes nothing is worse than no filter -- the customer believes
    they have narrowed the search. This asserts the stored shape is the shape the matcher reads."""
    from nexus.relevance.job_levels import matches_title, spec_from_icp

    token = await signup(client, slug="icpf3", email="a@icpf3.com", company="ICPF3")
    await client.put("/api/relevance/profile", headers=auth(token), json={
        "icp": {"job_levels": ["director", "head"], "title_keywords": ["facilities"],
                "exclude_title_keywords": ["assistant"]},
        "value_props": [], "product_context": "",
    })
    icp = (await client.get("/api/relevance/profile", headers=auth(token))).json()["icp"]

    spec = spec_from_icp(icp)
    for title in ("Head of Facilities", "Facilities Head", "Director of Facilities",
                  "Director, Facilities", "Facilities Director"):
        assert matches_title(title, spec), f"{title!r} should match what was saved"
    assert not matches_title("Assistant Director of Facilities", spec)
    assert not matches_title("Facilities Coordinator", spec)
