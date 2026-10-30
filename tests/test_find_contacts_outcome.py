"""Find contacts says why it found nobody, and asks LinkedIn for titles a person would hold.

SDR feedback, 2026-10-26: "contacts often came back 'not found', even after trying different buyer
titles". The chain knew why: LinkedIn's answer carried `no_verified_page`, `nobody_fits`,
`failed`, and how many rows named another company or held another title. It only LOGGED them, and
the page said "Couldn't source net-new people for this account right now", so the team changed
titles when titles were not the problem. Measured on a probe of 20 companies: LinkedIn `failed` on
all 20 (both Apify keys refused), 4 got nobody from the web fallback either, and 16 of 37 people
found had no email any verifier would confirm.

The titles themselves went to LinkedIn as typed: "Chief Revenue Officer (CRO)" and "Founder / CEO"
with their brackets and slash, and the ICP's job LEVEL codes ("head", "c_level") as if they were
titles. Levels are a gate on the title (`relevance/job_levels.matches_title`), as the web path and
the Relevance page already treat them.
"""
from __future__ import annotations

import pytest

from nexus.lookalike.contacts import icp_title_targets
from nexus.prospecting.linkedin import LinkedInPerson, employee_fits


def _person(title, company="Acme"):
    return LinkedInPerson(linkedin_url="https://www.linkedin.com/in/x-1/", first_name="X",
                          last_name="Y", title=title, company_name=company)


# ---- what LinkedIn is asked --------------------------------------------------------------------

def test_titles_reach_linkedin_as_titles_a_person_holds():
    from nexus.prospecting.contacts import linkedin_search_titles

    icp = {"buyer_titles": ["Chief Revenue Officer (CRO)", "Founder / CEO", "VP/Head of Sales",
                            "  Head of Sales  "],
           "job_levels": ["head", "c_level"]}

    assert linkedin_search_titles(icp) == ["Chief Revenue Officer", "CRO", "Founder", "CEO",
                                           "Head of Sales"]


def test_levels_and_keywords_become_phrases_never_bare_codes():
    from nexus.prospecting.contacts import linkedin_search_titles

    titles = linkedin_search_titles({"job_levels": ["director"], "title_keywords": ["Facilities"]})

    assert "Director of Facilities" in titles and "Facilities Director" in titles
    assert not {"director", "Director", "c_level", "head"} & set(titles)


@pytest.mark.parametrize("title, fits", [
    ("VP of Sales", True),
    ("Vice President, Sales EMEA", True),
    ("Sales Manager", False),            # the right function at a level the ICP did not ask for
    ("Head of Sales", False),
])
def test_a_level_restricts_who_fits_rather_than_being_a_title(title, fits):
    targets = icp_title_targets({"buyer_titles": ["VP Sales"], "job_levels": ["vp"]})

    ok, why = employee_fits(_person(title), ["Acme"], targets)

    assert ok is fits, why
    assert "vp" not in targets.wanted, "a level code is not a title"


def test_an_excluded_word_still_wins():
    targets = icp_title_targets({"buyer_titles": ["Director of Operations"],
                                 "exclude_title_keywords": ["assistant"]})

    assert employee_fits(_person("Assistant Director of Operations"), ["Acme"], targets)[0] is False


# ---- what the person pressing the button is told -------------------------------------------------

async def _account(ts, tid, domain="acme-ops.io"):
    from nexus.models.account import Account

    acc = Account(tenant_id=tid, name="Acme Ops", domain=domain)
    ts.add(acc)
    await ts.flush()
    return acc


class _DeadLinkedIn:
    async def run_actor(self, *args, **kwargs):
        from nexus.integrations.apify import ApifyError

        raise ApifyError("all 2 key(s) refused - Monthly usage hard limit exceeded")


async def test_nobody_found_says_which_step_stopped_and_why():
    from nexus.contacts.sourcing import find_contacts
    from tests.conftest import make_tenant, seed_relevance_profile, tenant_session

    tid = await make_tenant("fc1")
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, icp={"buyer_titles": ["Operations Manager"]})
        acc = await _account(ts, tid)
        result = await find_contacts(ts, acc, limit=5, linkedin_client=_DeadLinkedIn())

    assert result.contacts == []
    assert result.outcome["linkedin"]["status"] == "unavailable"
    assert result.outcome["web"]["status"] == "none"
    assert "LinkedIn search is unavailable right now" in result.message
    assert "Web search found nobody with your buyer titles at acme-ops.io" in result.message
    assert result.message.endswith("Nothing was charged."), "charged only for a result"


async def test_without_buyer_titles_it_says_to_add_them():
    from nexus.contacts.sourcing import find_contacts
    from tests.conftest import make_tenant, seed_relevance_profile, tenant_session

    tid = await make_tenant("fc2")
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, icp={"industries": ["Retail"]})
        acc = await _account(ts, tid, "acme-retail.io")
        result = await find_contacts(ts, acc, limit=5, linkedin_client=_DeadLinkedIn())

    assert result.outcome["linkedin"]["status"] == "no_titles"
    assert "Add buyer titles on the Relevance page" in result.message


def test_the_message_counts_people_by_what_their_email_can_be_trusted_for():
    from nexus.contacts.sourcing import FindContactsResult
    from nexus.models.account import Account, Contact

    acc = Account(tenant_id="t", name="Acme", domain="acme.io")
    people = [Contact(tenant_id="t", account_id="a", full_name="A", email="a@acme.io",
                      email_status="valid"),
              Contact(tenant_id="t", account_id="a", full_name="B", email="b@acme.io",
                      email_status="catch_all"),
              Contact(tenant_id="t", account_id="a", full_name="C"),
              Contact(tenant_id="t", account_id="a", full_name="D")]
    result = FindContactsResult(contacts=people, linkedin={"status": "found", "found": 4},
                                web={"status": "not_needed"})

    assert result.outcome["emails"] == {"verified": 1, "unconfirmed": 1, "none": 2}
    assert result.message_for(acc) == (
        "Added 4 people from LinkedIn: 1 with a verified email, 1 whose email cannot be "
        "confirmed (the domain accepts every address), 2 with no email we could verify (no "
        "charge for those).")


async def test_the_endpoint_returns_the_contacts_the_outcome_and_the_sentence(client):
    from tests.conftest import auth, signup

    token = await signup(client, slug="fc3", email="o@fc3.com", company="FC3")
    r = await client.post("/api/accounts", headers=auth(token),
                          json={"name": "Nobody Here", "domain": "nobody-here.io"})
    account_id = r.json()["id"]

    found = await client.post(f"/api/accounts/{account_id}/source-contacts", headers=auth(token))

    assert found.status_code == 200, found.text
    body = found.json()
    assert body["contacts"] == []
    assert body["outcome"]["linkedin"]["status"]
    assert body["message"]


async def test_an_ai_app_is_told_the_count_and_the_reason_too(client):
    """The MCP tool read the endpoint as a list: on the new shape it would report three "added"
    (the object's keys) and drop the reason an assistant needs to answer "why nobody?"."""
    from tests.conftest import auth, signup
    from tests.test_mcp_coverage_tools import _call

    token = await signup(client, slug="fc4", email="o@fc4.com", company="FC4")
    r = await client.post("/api/accounts", headers=auth(token),
                          json={"name": "Nobody There", "domain": "nobody-there.io"})

    out = await _call(client, token, "find_contacts", account_id=r.json()["id"], limit=5)

    assert out.ok, out
    assert out.data["count"] == 0 and out.data["added"] == []
    assert "Add buyer titles on the Relevance page" in out.data["why"]
    assert out.data["outcome"]["linkedin"]["status"] == "no_titles"
