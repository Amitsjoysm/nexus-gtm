# tests/test_account_dedupe.py
"""One Acme per workspace, however it got there.

Accounts arrive from six paths, each of which grew its own duplicate check comparing the **raw**
domain string — so ``acme.com``, ``www.acme.com``, ``https://acme.com/`` and ``ACME.com`` were four
different accounts. The manual create endpoint, the path a rep uses most, had no check at all.

What the rep saw: four Acme rows, the fit score computed four times, four inbox tasks for the same
funding round, and no way to tell which row held their notes.
"""
from __future__ import annotations

from nexus.accounts.dedupe import find_existing_account, normalise_name, normalise_on_write
from tests.conftest import auth, make_tenant, signup, tenant_session


async def _create(client, token, name, domain):
    return await client.post(
        "/api/accounts", headers=auth(token), json={"name": name, "domain": domain},
    )


# ---- the four spellings ---------------------------------------------------------------------------

async def test_the_same_company_typed_four_ways_creates_one_account(client):
    token = await signup(client, slug="dd1", email="o@dd1.com", company="DD1")

    first = await _create(client, token, "Acme", "acme.com")
    assert first.status_code == 201

    for spelling in ("www.acme.com", "https://acme.com/", "ACME.com", "http://www.acme.com"):
        again = await _create(client, token, "Acme Corp", spelling)
        assert again.status_code == 409, f"{spelling} should have been recognised as Acme"
        assert again.json()["detail"]["account_id"] == first.json()["id"]

    listed = await client.get("/api/accounts", headers=auth(token))
    assert len([a for a in listed.json() if "acme" in (a["domain"] or "")]) == 1


async def test_the_conflict_names_the_account_so_the_ui_can_offer_to_open_it(client):
    """A rep who hits this wants to go to the existing Acme, not read an error."""
    token = await signup(client, slug="dd2", email="o@dd2.com", company="DD2")
    created = await _create(client, token, "Acme", "acme.com")
    dupe = await _create(client, token, "Acme", "acme.com")

    detail = dupe.json()["detail"]
    assert detail["error"] == "duplicate_account"
    assert detail["account_id"] == created.json()["id"]
    assert "Acme" in detail["message"]


async def test_genuinely_different_companies_are_still_separate(client):
    token = await signup(client, slug="dd3", email="o@dd3.com", company="DD3")
    assert (await _create(client, token, "Acme", "acme.com")).status_code == 201
    assert (await _create(client, token, "Globex", "globex.com")).status_code == 201


async def test_the_stored_domain_is_normalised(client):
    """So the next comparison is exact rather than another LIKE scan."""
    token = await signup(client, slug="dd4", email="o@dd4.com", company="DD4")
    created = await _create(client, token, "Acme", "https://www.acme.com/pricing?utm=x")
    assert created.json()["domain"] == "acme.com"


# ---- archived rows --------------------------------------------------------------------------------

async def test_re_adding_an_archived_account_is_a_conflict_not_a_second_row(client):
    """A rep who archived Acme and re-adds it wants their old row back with its notes. Creating a
    duplicate alongside an archived original is how a workspace ends up holding both."""
    token = await signup(client, slug="dd5", email="o@dd5.com", company="DD5")
    created = await _create(client, token, "Acme", "acme.com")
    aid = created.json()["id"]
    await client.delete(f"/api/accounts/{aid}", headers=auth(token))

    dupe = await _create(client, token, "Acme", "acme.com")
    assert dupe.status_code == 409
    assert dupe.json()["detail"]["account_id"] == aid
    assert dupe.json()["detail"]["archived"] is True


# ---- tenancy ---------------------------------------------------------------------------------------

async def test_dedupe_never_reaches_across_tenants(client):
    """Two workspaces tracking Acme is the normal case. Deduping across them would be a data leak."""
    a = await signup(client, slug="dd6", email="o@dd6.com", company="DD6")
    b = await signup(client, slug="dd7", email="o@dd7.com", company="DD7")

    assert (await _create(client, a, "Acme", "acme.com")).status_code == 201
    assert (await _create(client, b, "Acme", "acme.com")).status_code == 201, (
        "another workspace's Acme must not block this one"
    )


# ---- name fallback ----------------------------------------------------------------------------------

async def test_an_account_with_no_domain_dedupes_on_an_exact_normalised_name(client):
    token = await signup(client, slug="dd8", email="o@dd8.com", company="DD8")
    assert (await _create(client, token, "Northwind Traders", None)).status_code == 201
    dupe = await _create(client, token, "Northwind Traders, Inc.", None)
    assert dupe.status_code == 409, "the legal suffix is not a different company"


async def test_a_name_match_never_overrides_a_different_domain(client):
    """Apex the fintech and Apex the logistics firm are different companies. A name is not an
    identity, and this codebase has already shipped six wrong-attribution bugs trusting one."""
    token = await signup(client, slug="dd9", email="o@dd9.com", company="DD9")
    assert (await _create(client, token, "Apex", "apex-fintech.com")).status_code == 201
    assert (await _create(client, token, "Apex", "apexlogistics.io")).status_code == 201


def test_legal_suffixes_normalise_away():
    for variant in ("Acme", "Acme Inc", "Acme, Inc.", "Acme LLC", "Acme Corp.", "Acme Co., Ltd."):
        assert normalise_name(variant) == "acme", variant


def test_normalising_keeps_an_unusable_domain_rather_than_blanking_it():
    """It is still what the rep typed, and it may be all they have."""
    assert normalise_on_write("not a domain") == "not a domain"
    assert normalise_on_write(None) is None
    assert normalise_on_write("https://www.acme.com/x") == "acme.com"


# ---- the bulk paths ------------------------------------------------------------------------------------

async def test_the_helper_finds_an_account_stored_unnormalised():
    """Rows created before normalisation-on-write still have to be recognised, or the fix would
    create a second wave of duplicates against the first."""
    from nexus.models.account import Account

    tid = await make_tenant(slug="dd10", name="DD10")
    async with tenant_session(tid) as ts:
        legacy = Account(name="Acme", domain="https://WWW.Acme.com/")
        ts.add(legacy)
        await ts.flush()

        found = await find_existing_account(ts, domain="acme.com")
        assert found is not None and found.id == legacy.id
