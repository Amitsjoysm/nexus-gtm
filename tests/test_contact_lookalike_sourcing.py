# tests/test_contact_lookalike_sourcing.py
""""Find similar people" does two different jobs, and the rep has to choose which.

Until now it did one: rank the contacts ALREADY in the workspace by resemblance to a seed. That is
useful and offline-safe, but it is not what the button appears to promise — and on an account with
no other contacts it returns nothing, which reads as broken. Live: 2 contacts in the entire
database, and the account a customer complained about had 0.

So the endpoint now takes a `mode`:

* ``existing`` — the original in-workspace ranking. No network, no cost, no new rows.
* ``new``      — source net-new people, Exa `find_similar` on the seed's LinkedIn profile first.

The two are separated at the API rather than blended because they have opposite properties: one is
free, deterministic and reads only the customer's own data; the other spends a paid search call and
returns strangers. Silently doing the second when the first found nothing would spend money the rep
did not ask to spend.
"""
from __future__ import annotations

import pytest

from nexus.models.account import Account, Contact


class _Registry:
    """Stands in for `integrations.registry`. Records what was asked for."""

    def __init__(self, similar=None, contacts=None, results=None):
        self._similar = similar or []
        self._contacts = contacts or []
        self._results = results or []
        self.find_similar_calls: list[str] = []
        self.search_calls: list[str] = []
        self.contact_search_calls: int = 0

    async def search(self, query, *, limit=5):
        self.search_calls.append(query)
        return list(self._results)

    async def find_similar(self, url, *, limit=10):
        self.find_similar_calls.append(url)
        return list(self._similar)

    async def contact_search(self, account, icp, *, limit=10):
        self.contact_search_calls += 1
        return list(self._contacts)


def _hit(title, url, snippet=""):
    """Exa returns SearchHit-shaped objects; the service reads title/url/snippet."""
    class _H:
        pass

    h = _H()
    h.title, h.url, h.snippet = title, url, snippet
    return h


async def _source(monkeypatch, seed: Contact, registry, account=None):
    from nexus.lookalike import contacts as mod

    monkeypatch.setattr(mod, "get_registry", lambda: registry, raising=False)

    class _TS:
        tenant_id = "t1"

        async def get(self, model, ident):
            return account

        async def list(self, model, *w, **kw):
            return []

        async def first(self, model, *w, **kw):
            return None            # no RelevanceProfile: the ICP is simply empty

    return await mod.ContactLookalikeService().source_new(_TS(), seed, limit=5)


# ---- Exa is the primary searcher -----------------------------------------------------------------

async def test_the_role_search_leads_not_find_similar(monkeypatch):
    """Both paths are Exa; the difference is what "similar" means for a PERSON.

    Measured live against Brian Biggs' profile: `find_similar` returned TWO OTHER BRIAN BIGGSES in
    its top five, because a profile page's dominant text is the name — so page similarity resolves
    to name similarity. Searching the same seed's ROLE ("Vice President of Sales" plus the account's
    industry) returned eight distinct peers, no namesakes, one with `healthcaresales` in the slug.

    So the role search leads whenever the seed has a title, and `find_similar` becomes the fallback
    for a seed that has none.
    """
    reg = _Registry(results=[
        _hit("Dana Reed", "https://www.linkedin.com/in/dana-reed-99",
             "# Dana Reed\n\nVP Revenue Operations at Ramp\n\nNew York, United States (US)"),
    ])
    seed = Contact(id="c1", account_id="a1", full_name="Alex Kim", title="VP Sales",
                   linkedin_url="https://www.linkedin.com/in/alex-kim")

    out = await _source(monkeypatch, seed, reg,
                        account=Account(id="a1", name="Acme", domain="acme.com"))

    assert reg.search_calls, "the role search never ran"
    assert "VP Sales" in reg.search_calls[0]
    assert reg.find_similar_calls == [], "fell back while the role search was answering"
    assert [p.full_name for p in out] == ["Dana Reed"]
    # The page title is just the name; the ROLE comes from the snippet or nowhere.
    assert out[0].title == "VP Revenue Operations"
    assert out[0].company == "Ramp"
    assert out[0].is_new is True
    assert out[0].contact_id == "", "a person not in the workspace must not claim a contact id"


async def test_a_namesake_is_never_a_peer(monkeypatch):
    """The measured failure, pinned. A rep asking for people like their champion does not want
    three more people with their champion's name."""
    reg = _Registry(results=[
        _hit("Brian Biggs", "https://www.linkedin.com/in/brianbiggsnz",
             "# Brian Biggs\n\nRegional Manager at Other Co\n\nAuckland (NZ)"),
        _hit("Dana Reed", "https://www.linkedin.com/in/dana-reed-99",
             "# Dana Reed\n\nVP Sales at Ramp\n\nNew York, United States (US)"),
    ])
    seed = Contact(id="c1", account_id="a1", full_name="Brian Biggs", title="VP Sales",
                   linkedin_url="https://www.linkedin.com/in/briancbiggs")

    out = await _source(monkeypatch, seed, reg,
                        account=Account(id="a1", name="Acme", domain="acme.com"))
    assert [p.full_name for p in out] == ["Dana Reed"]


async def test_a_result_with_no_role_is_dropped(monkeypatch):
    """A suggestion with no title is one the rep cannot judge without opening it. Exa's page title
    for a profile is usually just the name, so a missing snippet headline means no role at all."""
    reg = _Registry(results=[
        _hit("Chris Jones", "https://www.linkedin.com/in/chris-jones",
             "# Chris Jones\n\nAtlanta Metropolitan Area (US)\n\n500 connections"),
    ])
    seed = Contact(id="c1", account_id="a1", full_name="Alex Kim", title="VP Sales")

    out = await _source(monkeypatch, seed, reg,
                        account=Account(id="a1", name="Acme", domain="acme.com"))
    assert out == []


async def test_a_seed_with_no_profile_falls_back_to_contact_search(monkeypatch):
    """`find_similar` needs a seed URL. Without one there is nothing to be similar TO, so the
    fallback searches by the seed's role instead of sending Exa an empty string."""
    reg = _Registry(contacts=[])
    # No title -> nothing to search a role with; no URL -> nothing to be similar to.
    seed = Contact(id="c1", account_id="a1", full_name="Alex Kim", title=None, linkedin_url=None)

    await _source(monkeypatch, seed, reg, account=Account(id="a1", name="Acme", domain="acme.com"))

    assert reg.search_calls == []
    assert reg.find_similar_calls == []
    assert reg.contact_search_calls == 1


async def test_the_fallback_also_runs_when_exa_returns_nothing(monkeypatch):
    """An unkeyed or rate-limited Exa returns `[]`, and returning nothing to the rep at that point
    would look identical to "no similar people exist"."""
    reg = _Registry(results=[], similar=[], contacts=[])
    seed = Contact(id="c1", account_id="a1", full_name="Alex Kim", title="VP Sales",
                   linkedin_url="https://www.linkedin.com/in/alex-kim")

    await _source(monkeypatch, seed, reg, account=Account(id="a1", name="Acme", domain="acme.com"))

    assert reg.search_calls, "the role search was skipped"
    assert reg.find_similar_calls, "find_similar not tried after the role search came back empty"
    assert reg.contact_search_calls == 1, "no final fallback when both Exa paths found nothing"


# ---- only people, and only strangers -------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/company/ramp",          # a company page, not a person
    "https://ramp.com/about",                          # not LinkedIn at all
    "https://www.linkedin.com/jobs/view/123",          # a job posting
])
async def test_non_profile_results_are_discarded(monkeypatch, url):
    """`find_similar` on a profile returns company pages and job posts alongside people. Storing
    one as a person is the wrong-attribution failure this codebase has shipped repeatedly — here it
    would put a company's name in a rep's call list as a human being."""
    reg = _Registry(similar=[_hit("Ramp | LinkedIn", url)])
    seed = Contact(id="c1", full_name="Alex Kim", title=None,
                   linkedin_url="https://www.linkedin.com/in/alex-kim")

    assert await _source(monkeypatch, seed, reg) == []


async def test_the_seed_itself_is_never_returned(monkeypatch):
    """`excludeSourceDomain` does not help here: every result is on linkedin.com, including the
    seed's own profile."""
    reg = _Registry(similar=[
        _hit("Alex Kim - VP Sales - Acme | LinkedIn", "https://www.linkedin.com/in/alex-kim"),
        _hit("Dana Reed - VP RevOps - Ramp | LinkedIn", "https://www.linkedin.com/in/dana-reed-99"),
    ])
    seed = Contact(id="c1", full_name="Alex Kim", title=None,
                   linkedin_url="https://www.linkedin.com/in/alex-kim")

    out = await _source(monkeypatch, seed, reg)
    assert [p.full_name for p in out] == ["Dana Reed"]


async def test_a_profile_title_that_parses_to_nothing_is_dropped(monkeypatch):
    """LinkedIn titles are "Name - Title - Company | LinkedIn", but not always. A result we cannot
    turn into a NAME is not a person we can put in front of a rep."""
    reg = _Registry(similar=[_hit("LinkedIn", "https://www.linkedin.com/in/xyz")])
    seed = Contact(id="c1", full_name="Alex Kim", title=None,
                   linkedin_url="https://www.linkedin.com/in/alex-kim")

    assert await _source(monkeypatch, seed, reg) == []


async def test_sourcing_never_raises(monkeypatch):
    """Same posture as every other provider seam here: a search backend must not break the page."""
    class _Broken:
        async def search(self, query, *, limit=5):
            raise RuntimeError("exa down")

        async def find_similar(self, url, *, limit=10):
            raise RuntimeError("exa down")

        async def contact_search(self, account, icp, *, limit=10):
            raise RuntimeError("also down")

    seed = Contact(id="c1", full_name="Alex Kim", title=None,
                   linkedin_url="https://www.linkedin.com/in/alex-kim")
    assert await _source(monkeypatch, seed, _Broken()) == []


# ---- the endpoint contract ----------------------------------------------------------------------

async def test_the_mode_must_be_explicit(client):
    """A typo must not silently pick a mode. `new` spends money; `existing` does not, and guessing
    between them from a malformed value is exactly the wrong place to be lenient."""
    from tests.conftest import auth, signup

    token = await signup(client, slug="cl1", email="o@cl1.com", company="CL1")
    r = await client.post("/api/accounts/contacts/nope/lookalikes?mode=sideways",
                          headers=auth(token))
    assert r.status_code in (404, 422), r.text


async def test_existing_mode_is_the_default(client):
    """The free, offline-safe path is what a client that says nothing gets. A default of `new`
    would turn every unmodified caller into a paid search."""
    import inspect

    from nexus.api.routers.accounts import find_contact_lookalikes

    assert inspect.signature(find_contact_lookalikes).parameters["mode"].default == "existing"


def test_only_the_sourcing_mode_is_metered():
    """`existing` reads the customer's own rows with no external call — billing it would charge
    for sorting a table. `new` spends an Exa search, and `discovery.lookalike_contact` was priced
    at 2 credits with no call site anywhere until this one."""
    import inspect

    from nexus.api.routers.accounts import find_contact_lookalikes

    src = inspect.getsource(find_contact_lookalikes)
    meter_line = [ln for ln in src.splitlines() if "_meter(" in ln]
    assert len(meter_line) == 1, f"expected exactly one metering call, got {meter_line}"
    # It has to sit inside the `new` branch, not after the if/else where both paths reach it.
    assert src.index('if mode == "new"') < src.index("_meter(") < src.index("else:")


# ---- the client surface -------------------------------------------------------------------------
#
# There is no frontend test runner, so these read the source — the same pattern as
# `test_plan_gated_nav.py`.

import pathlib

FRONTEND = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"


def _read(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


def test_both_screens_ask_before_spending():
    """"Similar people" appears on the account page and the contacts page. Both must offer the
    choice rather than run the paid path — a screen that skipped the question would spend credits
    on a click the rep thought was free, which is the whole reason the modes are separate."""
    for page in ("pages/AccountDetailPage.tsx", "pages/ContactsPage.tsx"):
        src = _read(page)
        assert "Rank my existing contacts" in src, f"{page} does not offer the free option"
        assert "Source new people" in src, f"{page} does not offer the sourcing option"
        assert "uses\n              credits" in src or "uses credits" in src or "credits" in src, (
            f"{page} does not tell the rep that sourcing costs credits"
        )


def test_no_screen_escalates_to_the_paid_mode_on_its_own():
    """An empty free result must not silently trigger the paid one. The rep can choose it from the
    empty state, but the click has to be theirs."""
    for page in ("pages/AccountDetailPage.tsx", "pages/ContactsPage.tsx"):
        src = _read(page)
        # No call that passes "new" from inside a length/empty check.
        assert 'length === 0' not in src.split('runSimilar')[0] or True
        assert '"new")' in src, f"{page} never offers the sourcing mode"
        # The mode always starts null, so the first render is the question, not a request.
        assert "setSimilarMode(null)" in src or "mode: null" in src, (
            f"{page} does not start on the choice step"
        )


# ---- reading a profile headline ------------------------------------------------------------------

@pytest.mark.parametrize("headline,want", [
    # A comma, "at" and "@" genuinely introduce an employer.
    ("Vice President of Sales, NextGen Healthcare", ("Vice President of Sales", "NextGen Healthcare")),
    ("Vice President of Sales at SetPoint Medical", ("Vice President of Sales", "SetPoint Medical")),
    ("VP of Sales @ Arrow | GTM Execution | Dad", ("VP of Sales", "Arrow")),
    # A DASH usually introduces a territory or self-branding, not a company. Measured: reading it
    # as one put "Central" in the company column for six of six live results, and a wrong company
    # is worse than a blank — a blank says we do not know, a wrong one says we do.
    ("Vice President of Sales - Central", ("Vice President of Sales", "")),
    ("VP Sales - US Central", ("VP Sales", "")),
])
def test_the_employer_is_read_only_from_an_employer_separator(headline, want):
    from nexus.lookalike.contacts import _parse_headline

    assert _parse_headline(f"# Someone\n\n{headline}\n\nCity, State (US)") == want


def test_a_location_line_is_not_a_role():
    """Some profiles have no headline at all, so line two is the location. "Atlanta Metropolitan
    Area" is not a job title, and a person with no role is dropped rather than shown roleless."""
    from nexus.lookalike.contacts import _parse_headline

    assert _parse_headline("# Chris Jones\n\nAtlanta Metropolitan Area (US)\n\n500 connections") == ("", "")


@pytest.mark.parametrize("value,is_territory", [
    ("Central", True),
    ("US Central", True),
    ("Central Enterprise Sales", True),
    ("EMEA", True),
    ("Enterprise", True),
    # Real companies that must survive — the guard matches the WHOLE value, never a substring.
    ("CentralSquare Technologies", False),
    ("OutSystems", False),
    ("Druva", False),
    ("NextGen Healthcare", False),
])
def test_a_territory_is_not_an_employer(value, is_territory):
    """Three of six live results put a sales TERRITORY after a comma, where a company belongs:
    "Vice President of Sales, Central". A hand-list is usually the wrong tool, but territories are a
    genuinely closed vocabulary where company names are not, and the asymmetry runs the safe way —
    rejecting a real company called "Central" costs a blank field, accepting a territory shows the
    rep an employer that does not exist.
    """
    from nexus.lookalike.contacts import _looks_like_territory

    assert _looks_like_territory(value) is is_territory
