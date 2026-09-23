"""Filing a sourced person under their employer resolves the company's domain automatically.

The add form left the domain blank for a company the workspace does not track, and blank meant no
email could be found and no signals were ever collected for that account — so the rep typed it by
hand or got nothing.
"""
from __future__ import annotations

import pytest

from nexus.core.security import decode_access_token
from nexus.integrations.search.provider import SearchHit, set_search_provider
from nexus.models.account import Account
from tests.conftest import auth, signup, tenant_session


class FakeSearch:
    name = "fake"
    query_dialect = "plain"
    last_failure = ""

    def __init__(self, *hits):
        self._hits = [SearchHit(title=t, url=u, snippet="", source="fake") for t, u in hits]

    async def search(self, query, *, limit=5):
        return list(self._hits)


@pytest.fixture
def searching():
    def install(*hits):
        set_search_provider(FakeSearch(*hits))

    yield install
    set_search_provider(None)


async def _add(client, token, **body):
    return await client.post(
        "/api/accounts/contacts/from-lookalike",
        json={"full_name": "Jane Doe", "title": "VP Sales", "linkedin_url": None,
              "company": "Acme Corp", **body},
        headers=auth(token),
    )


async def test_a_new_account_gets_the_resolved_domain(client, searching):
    searching(("Acme Corp — Official site", "https://www.acme.com/"))
    token = await signup(client, slug="sp1", email="o@sp1.x", company="SP1")

    body = (await _add(client, token)).json()

    assert body["account_domain"] == "acme.com"
    assert body["account_created"] is True


async def test_a_typed_domain_always_wins(client, searching):
    searching(("Acme Corp — Official site", "https://www.acme.com/"))
    token = await signup(client, slug="sp2", email="o@sp2.x", company="SP2")

    body = (await _add(client, token, new_account_domain="acme.io")).json()

    assert body["account_domain"] == "acme.io"


async def test_a_near_miss_leaves_the_domain_blank_rather_than_guessing(client, searching):
    # Nobody confirms the server's own fallback, and an account under the wrong domain collects
    # another company's signals and guesses email addresses there.
    searching(("Acme Plumbing - emergency plumbers", "https://acmeplumbing.com/"))
    token = await signup(client, slug="sp3", email="o@sp3.x", company="SP3")

    body = (await _add(client, token)).json()

    assert body["account_domain"] in (None, "")
    assert body["account_created"] is True


async def test_an_existing_account_with_the_same_domain_is_reused(client, searching):
    searching(("Acme Corp — Official site", "https://www.acme.com/"))
    token = await signup(client, slug="sp4", email="o@sp4.x", company="SP4")
    tid = (decode_access_token(token) or {})["tid"]
    async with tenant_session(tid) as ts:
        existing = Account(tenant_id=tid, name="Acme Corporation", domain="acme.com")
        ts.add(existing)
        await ts.flush()
        existing_id = existing.id

    body = (await _add(client, token)).json()

    assert body["account_id"] == existing_id, "a second account for the same company was created"
    assert body["account_created"] is False


async def test_the_endpoint_answers_the_add_form(client, searching):
    searching(("Acme Corp — Official site", "https://www.acme.com/"))
    token = await signup(client, slug="sp5", email="o@sp5.x", company="SP5")

    r = await client.get("/api/accounts/company-domain?name=Acme%20Corp", headers=auth(token))

    assert r.status_code == 200, r.text
    assert r.json()["domain"] == "acme.com"
    assert r.json()["url"].startswith("https://www.acme.com")


async def test_the_endpoint_says_nothing_rather_than_guessing(client, searching):
    searching(("Acme Corp | LinkedIn", "https://www.linkedin.com/company/acme"))
    token = await signup(client, slug="sp6", email="o@sp6.x", company="SP6")

    r = await client.get("/api/accounts/company-domain?name=Acme%20Corp", headers=auth(token))

    assert r.status_code == 200
    assert r.json()["domain"] in (None, "")


def _form_source() -> str:
    from pathlib import Path

    return Path("frontend/src/components/AddSimilarPerson.tsx").read_text(encoding="utf-8")


def test_the_form_asks_the_server_rather_than_the_rep():
    # There is no frontend test runner, so this reads the source, like the other *_ui tests.
    src = _form_source()

    assert ".companyDomain(" in src, "the add form no longer looks the domain up"
    assert "creating" in src, "the lookup is not scoped to a new account"


def test_the_lookup_never_overwrites_what_the_rep_typed():
    # A resolved domain is a suggestion. Landing on top of a hand-typed one would file the person
    # under a company the rep had already corrected.
    src = _form_source()

    assert "setNewDomain((typed) => typed || found.domain" in src
