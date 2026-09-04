# tests/test_signal_strength_filter.py
"""The Signals list can ask for the event tier.

`_classify_news` already tiers correctly — funding 0.85, a leadership change 0.6, and a company's
own marketing post matches no needle and lands at 0.4, below the 0.5 alert floor so it creates no
Inbox task. Measured live on the RSS source:

    120  news     0.4   the company's own blog ("7 Reasons to leave On-Premise Data Centres")
     10  news     0.6   real events (acquires / partners / launches)
      3  funding  0.85
      1  hiring   0.6   a leadership change

So the scoring was never the problem. The problem was that the LIST had no way to ask for the
event tier, so all 134 arrived together and the four that mattered were buried under the rest.
That is what made signals read as noise.

Optional and unset by default: an existing caller sees exactly what it saw before.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def test_the_filter_is_optional(client):
    """No `min_strength` means the previous behaviour exactly. A default that silently hid weak
    signals would change what every existing caller sees, including the account timeline where a
    0.4 mention is legitimate context."""
    import inspect

    from nexus.api.routers.signals import list_signals

    assert inspect.signature(list_signals).parameters["min_strength"].default.default is None


async def test_the_filter_is_applied_server_side(client):
    """Server-side so pagination stays correct. Filtering in the client would page over unfiltered
    rows and hand back short pages that look like the end of the list."""
    import inspect

    from nexus.api.routers.signals import list_signals

    src = inspect.getsource(list_signals)
    assert "SignalEvent.strength >= min_strength" in src


async def test_it_returns_only_the_event_tier(client):
    """The behaviour a rep gets: ask for 0.5 and the marketing posts are gone."""
    from nexus.core.db import get_sessionmaker, utcnow
    from nexus.core.tenancy import TenantSession
    from nexus.models.signal import SignalEvent

    token = await signup(client, slug="sf1", email="o@sf1.com", company="SF1")
    h = auth(token)
    # The account the API creates carries the tenant id; reading it back beats guessing at a
    # /me payload shape.
    created = (await client.post("/api/accounts", headers=h,
                                 json={"name": "Acme", "domain": "acme.com"})).json()
    account_id = created["id"]

    async with get_sessionmaker()() as s:
        from nexus.models.account import Account as _A

        acct = await s.get(_A, account_id)
        tid = acct.tenant_id
        ts = TenantSession(s, tid)
        for kind, strength, title in (
            ("news", 0.4, "7 Reasons to leave On-Premise Data Centres"),
            ("news", 0.4, "Being a Designer"),
            ("funding", 0.85, "Acme raises $40M Series B"),
            ("hiring", 0.6, "Acme appoints new CRO"),
        ):
            ts.add(SignalEvent(
                tenant_id=tid, account_id=account_id, kind=kind, source="rss",
                title=title, strength=strength, occurred_at=utcnow(),
                dedupe_key=f"k:{title}",
            ))
        await s.commit()

    everything = (await client.get("/api/signals?limit=50", headers=h)).json()
    events = (await client.get("/api/signals?limit=50&min_strength=0.5", headers=h)).json()

    assert len(everything) == 4, everything
    titles = {s["title"] for s in events}
    assert titles == {"Acme raises $40M Series B", "Acme appoints new CRO"}, titles
    # Funding and leadership survive at full strength — the whole point of filtering rather than
    # suppressing the source.
    assert max(s["strength"] for s in events) == 0.85


# ---- the client surface --------------------------------------------------------------------------

def test_the_signals_page_defaults_to_the_event_tier_but_says_what_it_hides():
    """A page that quietly drops rows is how someone concludes collection is broken — which is the
    opposite of the complaint this is answering. The default filters, and the button names the
    number it is hiding so one click brings them back."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "frontend" / "src" / "pages" / "SignalsPage.tsx").read_text(encoding="utf-8")

    assert "useState(true)" in src and "eventsOnly" in src, "the page does not default to events"
    assert "min_strength: eventsOnly ? 0.5 : undefined" in src, "the filter is not sent"
    assert "hiddenCount" in src, "the page hides rows without saying how many"
    assert "weaker mention" in src, "no way back to the full list"
