# tests/test_web_news_attribution.py
"""`WebNewsSource` must be about the account it is filed under.

Customers reported that signals "are not matching with the company at all". Measured against the
live database, 164 web_news signals contained:

    29  directory / data-vendor profile pages (PitchBook, Crunchbase org, ZoomInfo, Owler)
    32  headlines naming a DIFFERENT company than the account they were filed under
    15  of those profile pages classified as `funding` — the strongest class in the system

Samples, each filed against the named account:

    The D. E. Shaw Group  <- "Arcesium - Wikipedia"
    Allscripts            <- "Netsmart Technologies | Private Equity | GI Partners"   (funding)
    PCC Ltd               <- "PulseSync Pte Ltd - LinkedIn"
    CompuGroup Medical    <- "About us - m.Doc"
    QualStaff Resources   <- "Funding To HR Software Startups Rises As M&A Activity"  (funding)
    LGI Healthcare        <- "LGI Healthcare Solutions 2026 Company Profile: Valuation, Funding"

**Every one of these is a failure the DORK path already guards against.** CLAUDE.md documents all
three filters as having been added after a live false positive — name must be in the TITLE, the
event is classified from the TITLE alone, and directory hosts are excluded. They were applied to
`dorks.py` and never back-ported to `WebNewsSource`, which is the broad-query sibling running on
every account refresh.

None of the three costs a request: they are string checks on hits already in memory, and
classifying the title alone is strictly less work than classifying title plus snippet.
"""
from __future__ import annotations

import pytest

from nexus.models.account import Account


class _Browser:
    """Returns a canned SERP. `search` is the only method `WebNewsSource` calls."""

    def __init__(self, hits):
        self._hits = hits
        self.queries: list[str] = []

    async def search(self, query, limit=6):
        self.queries.append(query)
        return list(self._hits)


async def _fetch(account: Account, hits: list[dict]):
    from nexus.ingestion.sources import WebNewsSource

    return await WebNewsSource(_Browser(hits)).fetch(account)


# ---- the account must be named in the HEADLINE --------------------------------------------------

async def test_a_headline_about_a_different_company_is_dropped():
    """The account was matched against `title + snippet`, so an article that merely MENTIONS the
    company in its body was filed as an event about it. Live: an Arcesium story on D. E. Shaw."""
    out = await _fetch(
        Account(name="The D. E. Shaw Group", domain="deshaw.com"),
        [{"title": "Arcesium - Wikipedia",
          "snippet": "Arcesium is a financial technology firm spun off from the D. E. Shaw Group.",
          "url": "https://en.wikipedia.org/wiki/Arcesium"}],
    )
    assert out == [], f"filed a story about another company: {[s.title for s in out]}"


async def test_an_industry_round_up_is_dropped():
    """The documented failure, from the dork path, reproduced here: a sector article that lists the
    account among many scored `funding` — the strongest class, which creates an Inbox task."""
    out = await _fetch(
        Account(name="QualStaff Resources", domain="qualstaff.com"),
        [{"title": "Funding To HR Software Startups Rises As M&A Activity Heats Up",
          "snippet": "Vendors including QualStaff Resources saw renewed investor interest.",
          "url": "https://news.crunchbase.com/venture/ai-hr-software-startups"}],
    )
    assert out == [], f"an industry round-up became an event: {[(s.kind, s.title) for s in out]}"


async def test_a_headline_that_does_name_the_account_is_kept():
    """The guard must not cost the common case, which is the whole point of the source."""
    out = await _fetch(
        Account(name="Catalis", domain="catalisgov.com"),
        [{"title": "Catalis Expands Suite of Technology Solutions with Axiomatic Acquisition",
          "snippet": "The government technology provider announced the deal on Tuesday.",
          "url": "https://example.com/catalis-acquires"}],
    )
    assert len(out) == 1, "the guard dropped a headline that does name the account"
    # `news` at 0.6 is correct: the classifier has no separate `acquisition` kind — "acquires" and
    # "acquisition" are `news` needles. Asserted so a future reshuffle of the kind vocabulary has
    # to come past this test rather than silently downgrading a real acquisition to a weak mention.
    assert out[0].kind == "news"
    assert out[0].strength >= 0.6


# ---- directory and data-vendor pages are never events -------------------------------------------

@pytest.mark.parametrize("url", [
    "https://pitchbook.com/profiles/company/43009-30",
    "https://www.crunchbase.com/organization/catalis",
    "https://www.zoominfo.com/c/catalis/123456",
    "https://www.owler.com/company/catalis",
    "https://tracxn.com/d/companies/catalis",
])
async def test_a_company_profile_page_is_never_a_signal(url):
    """A PitchBook profile exists for every company, always, and reports no event. It was being
    stored as `funding` because the page title literally contains the word — 15 times live.

    Host-matched, not title-matched: the title varies by vendor and the host is the fact that
    settles it. Note this list is NOT `_NON_COMPANY_HOSTS` from `company_search.py`, which also
    blocks TechCrunch and Forbes — correct when asking "is this hit a company?", wrong here, where
    trade press is the primary source of real funding news.
    """
    out = await _fetch(
        Account(name="Catalis", domain="catalisgov.com"),
        [{"title": "Catalis 2026 Company Profile: Valuation, Funding & Investors",
          "snippet": "Catalis is a government technology company.", "url": url}],
    )
    assert out == [], f"a directory page became a signal: {url}"


async def test_trade_press_is_still_allowed():
    """The counterweight. TechCrunch is where real funding news lives, and blocking it to stop
    PitchBook would cost far more than it saves."""
    out = await _fetch(
        Account(name="Vanta", domain="vanta.com"),
        [{"title": "Vanta raises $150M Series C led by Wellington",
          "snippet": "The compliance automation company said it will expand.",
          "url": "https://techcrunch.com/2026/09/01/vanta-series-c/"}],
    )
    assert len(out) == 1 and out[0].kind == "funding"


# ---- the event comes from the headline ----------------------------------------------------------

async def test_the_event_is_classified_from_the_title_alone():
    """A headline states what happened; a snippet recalls everything the company has ever done.
    Live: a product page whose body mentioned an earlier round was stored as `funding`."""
    out = await _fetch(
        Account(name="Vanta", domain="vanta.com"),
        [{"title": "Vanta Delivers: the Vanta control framework",
          "snippet": "Since raising its Series B funding round, Vanta has grown rapidly.",
          "url": "https://vanta.com/products/framework"}],
    )
    assert len(out) == 1
    assert out[0].kind != "funding", (
        "a product page was scored as a funding event because its BODY recalled an old round"
    )


# ---- the dork path needs the same host guard -----------------------------------------------------

async def test_the_dork_path_also_refuses_a_directory_page():
    """The dork path already had the two filters WebNewsSource was missing — name in the title,
    event from the title — which is why it produced ONE bad signal live against that source's
    twenty-nine. The one it produced was a directory page, because those pass both gates: they name
    the account, and their title carries the event word ("... Company Profile: Valuation, Funding").

    Checked BEFORE `self_evident`, which exists to skip the text gates for a result that is about
    the company by construction (its own ATS board). A PitchBook profile is the opposite of that.
    """
    from nexus.ingestion.dorks import select_dorks
    from nexus.ingestion.sources import DorkedSearchSource
    from nexus.core.db import utcnow

    src = DorkedSearchSource()
    dork = select_dorks(has_domain=True, limit=1)[0]
    hit = {
        "title": "Catalis 2026 Company Profile: Valuation, Funding & Investors",
        "snippet": "Catalis raised funding. Government technology.",
        "url": "https://pitchbook.com/profiles/company/43009-30",
    }
    out = src._to_signal(
        dork, hit, account=Account(name="Catalis", domain="catalisgov.com"),
        anchor="catalisgov.com", now=utcnow(), seen=set(),
    )
    assert out is None, "a PitchBook profile became a dork signal"
