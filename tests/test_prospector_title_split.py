"""Several job titles are several provider requests, merged (reported 2026-11-18).

"Job titles: CEO, CTO" showed only CTOs. Measured on the live InfoJoy API the same day:
``Titles: [CEO, CTO]`` returned the same 60 rows as ``[CTO]`` in either order, and
``[CFO, CEO, CTO]`` no CEO, while every other list filter (seniority, departments, countries,
industries, technologies, company names, keywords, sizes, revenue) mixed its values. So each title
is asked for alone, at a page size that keeps the rows bought what one search would buy, and the
pages are merged one row from each in turn. The fake provider here behaves like the real one: it
honours only the LAST title it is sent.
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from tests.conftest import auth, principal_from_token, signup


def _person(i: int, title: str) -> dict:
    slug = title.lower().replace(" ", "")
    return {"Title": title, "FirstName": f"{slug}{i}", "LastName": "Example",
            "Email": f"{slug}{i}@split-example.com", "DirectPhone": None,
            "CompanyName": f"Split {i} Example", "CompanyURL": f"split{i}-example.com",
            "Country": "United States"}


PEOPLE = ([_person(i, "CEO") for i in range(30)] + [_person(i, "CTO") for i in range(5)]
          # One person under both titles: returned by both parts, shown once.
          + [{**_person(99, "CEO & CTO"), "Email": "both@split-example.com"}])


class OneTitleVendor:
    """Answers like InfoJoy: of several Titles only the last is applied."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        titles = (body.get("Filters") or {}).get("Titles") or []
        wanted = titles[-1].lower() if titles else ""
        rows = [p for p in PEOPLE if wanted in p["Title"].lower()]
        number, size = int(body["PageNumber"]), int(body["PageSize"])
        return httpx.Response(200, json={"PageNumber": number, "PageSize": size,
                                         "Items": rows[(number - 1) * size: number * size]})


@pytest.fixture
def vendor():
    from nexus.prospector import search
    from nexus.prospector.client import InfoJoyClient

    fake = OneTitleVendor()
    search.set_client(InfoJoyClient(keys=["k"], transport=httpx.MockTransport(fake),
                                    backoff_s=0))
    yield fake
    search.set_client(None)


@pytest.fixture(autouse=True)
async def _billing_seeded():
    from nexus.billing.catalog import sync_catalog
    from nexus.billing.plans import sync_plans
    from nexus.billing.rates import sync_rates

    await sync_catalog()
    await sync_plans()
    await sync_rates()


async def _workspace(client, slug):
    token = await signup(client, slug=slug, email=f"owner@{slug}.io", company=slug.upper())
    return auth(token), principal_from_token(token).tenant_id


async def _search(client, h, titles, page=1, page_size=25):
    r = await client.post("/api/prospector/search", headers=h, json={
        "view": "contacts", "filters": {"titles": titles}, "page": page,
        "page_size": page_size})
    return r


async def test_two_titles_bring_people_with_either_title(client, vendor):
    h, _ = await _workspace(client, "ts1")
    r = await _search(client, h, ["CEO", "CTO"])
    assert r.status_code == 200, r.text
    titles = [row["title"] for row in r.json()["rows"]]

    assert any(t == "CEO" for t in titles) and any(t == "CTO" for t in titles)
    # One request per title, each a share of the page: rows bought are what one search buys.
    sent = [(req["Filters"]["Titles"], req["PageSize"]) for req in vendor.requests]
    assert sorted(sent) == [(["CEO"], 13), (["CTO"], 13)]
    # Interleaved, not one title's block and then the other's.
    assert titles[:2] in (["CEO", "CTO"], ["CTO", "CEO"])


async def test_a_person_under_both_titles_appears_once(client, vendor):
    h, _ = await _workspace(client, "ts2")
    rows = (await _search(client, h, ["CEO", "CTO"], page_size=100)).json()["rows"]
    keys = [r["row_key"] for r in rows]
    assert len(keys) == len(set(keys))
    assert sum(r["title"] == "CEO & CTO" for r in rows) == 1


async def test_more_pages_follow_while_any_title_has_more(client, vendor):
    """CTO runs out on page 1 (5 people) while CEO has 31: the short merged page must not read as
    the last one."""
    h, _ = await _workspace(client, "ts3")
    first = (await _search(client, h, ["CEO", "CTO"])).json()
    assert first["has_more"] is True
    second = (await _search(client, h, ["CEO", "CTO"], page=2)).json()
    assert second["rows"] and all("CEO" in r["title"] for r in second["rows"])


async def test_rows_picked_from_a_merged_page_are_found_again_and_not_rebought(client, vendor):
    h, _ = await _workspace(client, "ts4")
    rows = (await _search(client, h, ["CEO", "CTO"])).json()["rows"]
    asked = len(vendor.requests)
    picked = [r for r in rows if r["title"] in ("CEO", "CTO")][:4]
    r = await client.post("/api/prospector/deliver", headers=h, json={
        "selection": {"view": "contacts", "scope": "total",
                      "filters": {"titles": ["CEO", "CTO"]},
                      "rows": [{"api_page": p["api_page"], "api_page_size": p["api_page_size"],
                                "row_key": p["row_key"]} for p in picked]},
        "action": "save", "request_id": uuid.uuid4().hex})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 4 and r.json()["unavailable"] == 0
    assert len(vendor.requests) == asked, "the selection was resolved from the cached parts"


async def test_one_title_is_one_request_as_before(client, vendor):
    h, _ = await _workspace(client, "ts5")
    await _search(client, h, ["CTO"])
    assert [(req["Filters"]["Titles"], req["PageSize"]) for req in vendor.requests] == \
        [(["CTO"], 25)]


async def test_more_than_ten_titles_is_refused_before_anything_is_bought(client, vendor):
    h, _ = await _workspace(client, "ts6")
    r = await _search(client, h, [f"Title {i}" for i in range(11)])
    assert r.status_code == 422 and "up to 10 job titles" in r.json()["detail"]
    assert vendor.requests == []
