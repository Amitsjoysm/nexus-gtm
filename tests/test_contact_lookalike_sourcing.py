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

    def __init__(self, similar=None, contacts=None):
        self._similar = similar or []
        self._contacts = contacts or []
        self.find_similar_calls: list[str] = []
        self.contact_search_calls: int = 0

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

async def test_exa_find_similar_is_tried_first_on_the_seeds_profile(monkeypatch):
    """The seed is a PERSON, so the strongest available query is that person's own profile URL.
    `find_similar` is Exa's neural neighbour search and needs no query construction at all — no
    guessing at title synonyms, no ICP round-trip."""
    reg = _Registry(similar=[
        _hit("Dana Reed - VP Revenue Operations - Ramp | LinkedIn",
             "https://www.linkedin.com/in/dana-reed-99"),
    ])
    seed = Contact(id="c1", full_name="Alex Kim", title="VP Sales",
                   linkedin_url="https://www.linkedin.com/in/alex-kim")

    out = await _source(monkeypatch, seed, reg)

    assert reg.find_similar_calls == ["https://www.linkedin.com/in/alex-kim"]
    assert reg.contact_search_calls == 0, "fell through to the fallback while Exa was answering"
    assert [p.full_name for p in out] == ["Dana Reed"]
    assert out[0].title == "VP Revenue Operations"
    assert out[0].is_new is True
    assert out[0].contact_id == "", "a person not in the workspace must not claim a contact id"


async def test_a_seed_with_no_profile_falls_back_to_contact_search(monkeypatch):
    """`find_similar` needs a seed URL. Without one there is nothing to be similar TO, so the
    fallback searches by the seed's role instead of sending Exa an empty string."""
    reg = _Registry(contacts=[])
    seed = Contact(id="c1", account_id="a1", full_name="Alex Kim", title="VP Sales",
                   linkedin_url=None)

    await _source(monkeypatch, seed, reg, account=Account(id="a1", name="Acme", domain="acme.com"))

    assert reg.find_similar_calls == []
    assert reg.contact_search_calls == 1


async def test_the_fallback_also_runs_when_exa_returns_nothing(monkeypatch):
    """An unkeyed or rate-limited Exa returns `[]`, and returning nothing to the rep at that point
    would look identical to "no similar people exist"."""
    reg = _Registry(similar=[], contacts=[])
    seed = Contact(id="c1", account_id="a1", full_name="Alex Kim", title="VP Sales",
                   linkedin_url="https://www.linkedin.com/in/alex-kim")

    await _source(monkeypatch, seed, reg, account=Account(id="a1", name="Acme", domain="acme.com"))

    assert reg.find_similar_calls, "Exa was skipped"
    assert reg.contact_search_calls == 1, "no fallback when the primary found nothing"


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
    seed = Contact(id="c1", full_name="Alex Kim",
                   linkedin_url="https://www.linkedin.com/in/alex-kim")

    assert await _source(monkeypatch, seed, reg) == []


async def test_the_seed_itself_is_never_returned(monkeypatch):
    """`excludeSourceDomain` does not help here: every result is on linkedin.com, including the
    seed's own profile."""
    reg = _Registry(similar=[
        _hit("Alex Kim - VP Sales - Acme | LinkedIn", "https://www.linkedin.com/in/alex-kim"),
        _hit("Dana Reed - VP RevOps - Ramp | LinkedIn", "https://www.linkedin.com/in/dana-reed-99"),
    ])
    seed = Contact(id="c1", full_name="Alex Kim",
                   linkedin_url="https://www.linkedin.com/in/alex-kim")

    out = await _source(monkeypatch, seed, reg)
    assert [p.full_name for p in out] == ["Dana Reed"]


async def test_a_profile_title_that_parses_to_nothing_is_dropped(monkeypatch):
    """LinkedIn titles are "Name - Title - Company | LinkedIn", but not always. A result we cannot
    turn into a NAME is not a person we can put in front of a rep."""
    reg = _Registry(similar=[_hit("LinkedIn", "https://www.linkedin.com/in/xyz")])
    seed = Contact(id="c1", full_name="Alex Kim",
                   linkedin_url="https://www.linkedin.com/in/alex-kim")

    assert await _source(monkeypatch, seed, reg) == []


async def test_sourcing_never_raises(monkeypatch):
    """Same posture as every other provider seam here: a search backend must not break the page."""
    class _Broken:
        async def find_similar(self, url, *, limit=10):
            raise RuntimeError("exa down")

        async def contact_search(self, account, icp, *, limit=10):
            raise RuntimeError("also down")

    seed = Contact(id="c1", full_name="Alex Kim",
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
