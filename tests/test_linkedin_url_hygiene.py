"""A LinkedIn URL on a contact is that person's own profile, and on an account that company's own
page, or there is none (2026-10-02).

Asked: "ensure the LinkedIn URLs are correct and incorrect ones are not mapped to accounts or
contacts." Four ways a wrong one got in:

* **Any string was accepted** where a person types or uploads one: the contact form, adding a
  similar person, and the contact CSV import. A company page, a post, a search URL or a typo became
  the person's "profile", and the phone finder and personalisation then read someone else's page.
* **A name anywhere in the result was enough** to attach a profile, snippet included. A colleague's
  profile whose snippet mentions the person passed both the name and the company check. A LinkedIn
  result's TITLE (or URL slug) is the profile owner's name, so that is what has to name them.
* **The account's company page was whatever the model read off six search snippets**, checked only
  for "linkedin.com" somewhere in it: a person's profile or a namesake company's page (measured:
  `linkedin.com/company/vanta` is VANTA - Chauffeurs) became the page a rep "verifies" against.
  Now it is the page a LinkedIn read proved (the shared record), else a company page the search
  actually returned whose result names the account's own domain.
* Values already stored are cleared by `scripts/clean_linkedin_urls.py` (dry run by default).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.enrichment.linkedin import company_page_url, is_own_profile, person_profile_url
from nexus.models.account import Account, Contact
from tests.conftest import auth, make_tenant, signup, tenant_session


# ---- the shapes -----------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "https://www.linkedin.com/in/jane-doe",
    "linkedin.com/in/jane-doe/",
    "https://uk.linkedin.com/in/jane-doe?trk=public",
    "http://www.linkedin.com/in/ACoAAB12xyz",
    "https://www.linkedin.com/in/jane-doe/details/experience/",
])
def test_a_member_profile_is_a_person_url(value):
    assert person_profile_url(value)


@pytest.mark.parametrize("value", [
    "https://www.linkedin.com/company/stripe",
    "https://www.linkedin.com/posts/jane-doe_activity-123",
    "https://www.linkedin.com/pub/dir/Jane/Doe",
    "https://www.linkedin.com/search/results/people/?keywords=jane",
    "https://www.linkedin.com/in/",
    "https://notlinkedin.com/in/jane-doe",
    "https://linkedin.com.evil.io/in/jane-doe",
    "https://evil.io/?next=https://linkedin.com/in/jane-doe",
    "jane doe",
    "",
])
def test_anything_else_is_not(value):
    assert person_profile_url(value) is None


def test_a_company_page_is_a_company_url_and_a_profile_is_not():
    assert company_page_url("https://www.linkedin.com/company/vanta-security/") == \
        "https://www.linkedin.com/company/vanta-security"
    assert company_page_url("linkedin.com/showcase/stripe-atlas")
    assert company_page_url("https://www.linkedin.com/in/jane-doe") is None
    assert company_page_url("https://www.linkedin.com/company/") is None


# ---- whose profile it is ------------------------------------------------------------------------

def _hit(url, title="", snippet=""):
    return SimpleNamespace(url=url, title=title, snippet=snippet)


def test_the_profile_owner_is_named_by_the_title_or_the_slug_not_the_snippet():
    own = _hit("https://www.linkedin.com/in/jd-123", title="Jane Doe - VP Sales - Acme | LinkedIn")
    slug = _hit("https://www.linkedin.com/in/jane-doe-4a1", title="LinkedIn")
    colleague = _hit("https://www.linkedin.com/in/sam-lee",
                     title="Sam Lee - Sales Manager - Acme | LinkedIn",
                     snippet="Reports to Jane Doe, VP Sales at Acme.")
    assert is_own_profile(own, "Jane Doe") and is_own_profile(slug, "Jane Doe")
    assert not is_own_profile(colleague, "Jane Doe")


async def test_the_finder_never_attaches_a_colleagues_profile():
    from nexus.enrichment.linkedin import LinkedInFinder

    class Search:
        async def __call__(self, query, *, limit=5):
            return [_hit("https://www.linkedin.com/in/sam-lee",
                         title="Sam Lee - Sales Manager - Acme | LinkedIn",
                         snippet="Works with Jane Doe at Acme.")]

    account = Account(name="Acme", domain="acme.io")
    contact = Contact(full_name="Jane Doe", title="VP Sales")
    assert await LinkedInFinder(Search()).find(account, contact) is None


def test_contact_search_keeps_only_the_persons_own_proving_profile():
    from nexus.integrations.contact_search import SearchBackedContactSearchProvider as Search

    proof = [{"url": "https://www.linkedin.com/in/sam-lee",
              "title": "Sam Lee - Acme | LinkedIn", "snippet": "Jane Doe and Sam Lee at Acme"},
             {"url": "https://www.linkedin.com/in/jane-doe-9",
              "title": "Jane Doe - VP Sales - Acme | LinkedIn", "snippet": ""}]
    assert Search._proven_profile(None, proof, "Jane Doe") == \
        "https://www.linkedin.com/in/jane-doe-9"
    assert Search._proven_profile(None, proof[:1], "Jane Doe") is None


# ---- where people type or upload them ---------------------------------------------------------------

async def test_the_contact_form_refuses_what_is_not_a_profile(client):
    headers = auth(await signup(client, slug="liform", email="rep@liform.io"))
    account = (await client.post("/api/accounts", json={"name": "Acme", "domain": "acme.io"},
                                 headers=headers)).json()
    bad = await client.post(f"/api/accounts/{account['id']}/contacts", headers=headers, json={
        "full_name": "Jane Doe", "linkedin_url": "https://www.linkedin.com/company/acme"})
    assert bad.status_code == 422 and "linkedin.com/in/" in bad.text
    good = await client.post(f"/api/accounts/{account['id']}/contacts", headers=headers, json={
        "full_name": "Jane Doe", "linkedin_url": "linkedin.com/in/jane-doe"})
    assert good.status_code in (200, 201) and good.json()["linkedin_url"]


async def test_the_csv_import_leaves_a_bad_profile_blank_and_says_so():
    from nexus.imports.csv_ingest import import_contacts_csv

    tid = await make_tenant("licsv")
    content = ("email,name,linkedin\n"
               "jane@acme.io,Jane Doe,https://www.linkedin.com/company/acme\n"
               "sam@acme.io,Sam Lee,https://www.linkedin.com/in/sam-lee\n").encode()
    mapping = {"email": "email", "name": "full_name", "linkedin": "linkedin_url"}
    async with tenant_session(tid) as ts:
        report = await import_contacts_csv(ts, content=content, mapping=mapping)
    assert report["created"] == 2
    assert any("LinkedIn" in e and "row 2" in e for e in report["errors"])
    async with tenant_session(tid) as ts:
        jane = await ts.first(Contact, Contact.email == "jane@acme.io")
        sam = await ts.first(Contact, Contact.email == "sam@acme.io")
    assert jane.linkedin_url is None and sam.linkedin_url == "https://www.linkedin.com/in/sam-lee"


# ---- the account's company page ------------------------------------------------------------------

def test_the_account_page_must_be_returned_by_the_search_and_name_the_domain():
    from nexus.enrichment.account import proven_company_page

    account = Account(name="Vanta", domain="vanta.com")
    namesake = _hit("https://www.linkedin.com/company/vanta",
                    title="VANTA - Chauffeurs | LinkedIn", snippet="Executive travel, London.")
    own = _hit("https://www.linkedin.com/company/vanta-security",
               title="Vanta | LinkedIn", snippet="Website: https://www.vanta.com. Security...")
    hits = [namesake, own]
    assert proven_company_page("https://www.linkedin.com/company/vanta", hits, account) is None
    assert proven_company_page("https://www.linkedin.com/company/vanta-security/", hits,
                               account) == "https://www.linkedin.com/company/vanta-security"
    # Invented (not among the results) or a person's profile: never.
    assert proven_company_page("https://www.linkedin.com/company/vanta-inc", hits, account) is None
    assert proven_company_page("https://www.linkedin.com/in/christina-cacioppo", hits,
                               account) is None


def test_applying_enrichment_never_stores_a_non_company_page():
    from nexus.enrichment.account import SearchBackedAccountEnricher

    account = Account(name="Acme", domain="acme.io", custom_fields={})
    SearchBackedAccountEnricher(search=None, llm=None).apply(
        account, {"linkedin_url": "https://www.linkedin.com/in/someone"})
    assert "linkedin_url" not in (account.custom_fields or {})


# ---- what is already stored ------------------------------------------------------------------------

async def test_the_cleanup_clears_bad_values_and_keeps_them_for_reference(monkeypatch, capsys):
    import sys

    from scripts import clean_linkedin_urls

    tid = await make_tenant("liclean")
    async with tenant_session(tid) as ts:
        account = Account(tenant_id=tid, name="Acme", domain="acme.io",
                          custom_fields={"linkedin_url": "https://www.linkedin.com/in/someone"})
        ts.add(account)
        await ts.flush()
        ts.add(Contact(tenant_id=tid, account_id=account.id, full_name="Jane Doe",
                       linkedin_url="https://www.linkedin.com/company/acme"))
        ts.add(Contact(tenant_id=tid, account_id=account.id, full_name="Sam Lee",
                       linkedin_url="https://www.linkedin.com/in/sam-lee"))

    monkeypatch.setattr(sys, "argv", ["clean_linkedin_urls.py"])
    await clean_linkedin_urls.main()
    out = capsys.readouterr().out
    assert "1 contact" in out and "1 account" in out
    async with tenant_session(tid) as ts:
        assert (await ts.first(Contact, Contact.full_name == "Jane Doe")).linkedin_url  # dry run

    monkeypatch.setattr(sys, "argv", ["clean_linkedin_urls.py", "--apply"])
    await clean_linkedin_urls.main()
    async with tenant_session(tid) as ts:
        jane = await ts.first(Contact, Contact.full_name == "Jane Doe")
        sam = await ts.first(Contact, Contact.full_name == "Sam Lee")
        acme = await ts.first(Account)
    assert jane.linkedin_url is None
    assert jane.custom_fields["rejected_linkedin_url"] == "https://www.linkedin.com/company/acme"
    assert sam.linkedin_url == "https://www.linkedin.com/in/sam-lee"
    assert "linkedin_url" not in acme.custom_fields
    assert acme.custom_fields["rejected_linkedin_url"] == "https://www.linkedin.com/in/someone"
