"""Title recommendation engine + endpoint. Deterministic and offline."""
from __future__ import annotations

from nexus.relevance.titles import ENT, MID, SMB, recommend_titles, size_band
from tests.conftest import auth, signup


def _titles(recs):
    return [r.title for r in recs]


# ---------------------------------------------------------------- engine (unit)
def test_size_band_thresholds():
    assert size_band(None) is None
    assert size_band(0) is None
    assert size_band(50) == SMB
    assert size_band(199) == SMB
    assert size_band(200) == MID
    assert size_band(2000) == MID
    assert size_band(2001) == ENT


def test_smb_elevates_founder():
    recs = recommend_titles(industry="Software", employee_count=40, limit=20)
    by = {r.title: r.priority_score for r in recs}
    assert "Founder" in by
    # Founder (SMB-only) outranks the enterprise-only CIO for a tiny company.
    assert by["Founder"] > by.get("Chief Information Officer", 0)


def test_tech_stack_elevates_head_of_data():
    with_data = recommend_titles(
        industry="Retail", employee_count=800, tech_stack=["Snowflake", "Looker"], limit=30
    )
    without = recommend_titles(industry="Retail", employee_count=800, limit=30)
    hod_with = next(r for r in with_data if r.title == "Head of Data")
    hod_without = next(r for r in without if r.title == "Head of Data")
    assert hod_with.priority_score > hod_without.priority_score
    assert "tech stack" in hod_with.reason
    assert hod_with.confidence > hod_without.confidence


def test_industry_elevates_ciso_and_cfo_in_fintech():
    fin = {r.title: r.priority_score
           for r in recommend_titles(industry="Financial Services", employee_count=1500, limit=40)}
    retail = {r.title: r.priority_score
              for r in recommend_titles(industry="Retail", employee_count=1500, limit=40)}
    assert fin["Chief Information Security Officer"] > retail["Chief Information Security Officer"]
    assert fin["Chief Financial Officer"] > retail["Chief Financial Officer"]


def test_icp_buyer_titles_boost_and_cite_reason():
    boosted = recommend_titles(
        industry="Software", employee_count=800, icp_buyer_titles=["Head of Product"], limit=40
    )
    plain = recommend_titles(industry="Software", employee_count=800, limit=40)
    hop_b = next(r for r in boosted if r.title == "Head of Product")
    hop_p = next(r for r in plain if r.title == "Head of Product")
    assert hop_b.priority_score > hop_p.priority_score
    assert hop_b.confidence >= hop_p.confidence
    assert "ICP" in hop_b.reason


def test_department_filter_restricts_output():
    recs = recommend_titles(industry="Software", employee_count=800, department="Sales", limit=20)
    assert recs
    assert all(r.department == "Sales" for r in recs)


def test_output_is_deterministic_bounded_and_well_formed():
    a = recommend_titles(industry="Software", employee_count=800, limit=5)
    b = recommend_titles(industry="Software", employee_count=800, limit=5)
    assert _titles(a) == _titles(b)          # deterministic
    assert len(a) == 5                        # honours limit
    # sorted by priority descending
    assert [r.priority_score for r in a] == sorted((r.priority_score for r in a), reverse=True)
    for r in a:
        assert 0 <= r.priority_score <= 100
        assert 0.0 <= r.confidence <= 1.0
        assert r.buying_influence in (
            "economic_buyer", "champion", "technical_evaluator", "end_user"
        )


# ---------------------------------------------------------------- endpoint (integration)
async def test_endpoint_adhoc_firmographics(client):
    token = await signup(client, slug="rex", email="o@rex.com", company="Rex")
    r = await client.post(
        "/api/relevance/title-recommendations",
        headers=auth(token),
        json={"industry": "Financial Services", "employee_count": 1500,
              "tech_stack": ["Snowflake"], "limit": 5},
    )
    assert r.status_code == 200
    data = r.json()
    assert 1 <= len(data) <= 5
    assert {
        "title", "priority_score", "confidence", "department",
        "buying_influence", "reason", "alternatives",
    } <= set(data[0])


async def test_endpoint_uses_account_and_icp(client):
    token = await signup(client, slug="rex2", email="o@rex2.com", company="Rex2")
    h = auth(token)
    await client.put("/api/relevance/profile", headers=h, json={
        "icp": {"industries": ["Software"], "buyer_titles": ["Head of Product"]},
        "value_props": [], "product_context": ""})
    acc = (await client.post("/api/accounts", headers=h, json={
        "name": "Globex", "domain": "globex.com", "industry": "Software",
        "employee_count": 800, "country": "US", "tech_stack": ["snowflake"]})).json()
    r = await client.post(
        "/api/relevance/title-recommendations",
        headers=h, json={"account_id": acc["id"], "limit": 15},
    )
    assert r.status_code == 200
    data = r.json()
    hop = next(d for d in data if d["title"] == "Head of Product")
    assert "ICP" in hop["reason"]        # ICP buyer_titles boosted it and is cited
