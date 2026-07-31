"""Website change monitoring: normalisation, hashing and diff summaries (M20).

The normaliser *is* the feature. Measured against live sites, hashing raw HTML is unstable across
two fetches seconds apart — linear.app/pricing and ramp.com/security both produced different digests
— because modern pages carry build ids, nonces, cache-busting asset URLs and inlined state that
change per request. A watcher built on raw hashing reports "pricing changed" on every run, which is
worse than no watcher: it trains whoever sees it to ignore the signal.

Every nuisance pinned below is one that actually appears in real pages.
"""
from __future__ import annotations

from nexus.ingestion.webwatch import content_hash, normalise, summarise_change


def _hash_differs(a: str, b: str) -> bool:
    return content_hash(a) != content_hash(b)


# ---- stability against per-request churn ------------------------------------------------------

def test_script_and_style_bodies_are_dropped_entirely():
    """Inlined JSON state and build manifests live in <script>. Stripping tags without removing the
    element body first would fold every build id into the digest."""
    a = '<html><body><h1>Pricing</h1><script>window.__BUILD__="abc123def456";</script></body></html>'
    b = '<html><body><h1>Pricing</h1><script>window.__BUILD__="zzz999yyy888";</script></body></html>'
    assert not _hash_differs(a, b)


def test_style_and_svg_noise_is_dropped():
    a = "<div><style>.x{color:#111}</style><svg><path d='M1 2'/></svg>Plans</div>"
    b = "<div><style>.x{color:#222}</style><svg><path d='M9 9'/></svg>Plans</div>"
    assert not _hash_differs(a, b)


def test_build_hashes_in_asset_urls_do_not_churn_the_digest():
    a = '<img src="/_next/static/9f2c1a7b3e4d5f60/logo.png">Enterprise'
    b = '<img src="/_next/static/0a1b2c3d4e5f6071/logo.png">Enterprise'
    assert not _hash_differs(a, b)


def test_cache_busting_query_strings_are_ignored():
    a = '<link href="/app.css?v=1699999999">Team plan'
    b = '<link href="/app.css?v=1700000000">Team plan'
    assert not _hash_differs(a, b)


def test_rendered_timestamps_are_ignored():
    a = "<p>Updated 2026-07-30T10:15:00Z</p>Starter"
    b = "<p>Updated 2026-07-31T22:41:13Z</p>Starter"
    assert not _hash_differs(a, b)


def test_whitespace_and_case_are_normalised():
    a = "<div>  Enterprise\n\n  Plan </div>"
    b = "<div>enterprise plan</div>"
    assert not _hash_differs(a, b)


# ---- but a REAL change must still register ----------------------------------------------------

def test_a_new_pricing_tier_is_detected():
    """The counterweight to everything above: normalisation must not be so aggressive that it hides
    the change it exists to find."""
    a = "<div><h2>Starter $20</h2><h2>Pro $50</h2></div>"
    b = "<div><h2>Starter $20</h2><h2>Pro $50</h2><h2>Enterprise custom</h2></div>"
    assert _hash_differs(a, b)


def test_a_price_change_is_detected():
    assert _hash_differs("<p>Pro $50 per seat</p>", "<p>Pro $65 per seat</p>")


def test_removed_content_is_detected():
    a = "<ul><li>SOC 2</li><li>ISO 27001</li><li>HIPAA</li></ul>"
    b = "<ul><li>SOC 2</li><li>ISO 27001</li></ul>"
    assert _hash_differs(a, b)


def test_normalise_keeps_the_readable_text():
    text = normalise("<div><h1>Pricing</h1><p>From $20/mo</p><script>x=1</script></div>")
    assert "pricing" in text and "from $20/mo" in text
    assert "x=1" not in text


# ---- change summaries -------------------------------------------------------------------------

def test_a_summary_names_what_was_added():
    """A rep needs a sentence to open with. "Content changed" is not one; "they added an Enterprise
    tier" is."""
    summary = summarise_change(
        "starter $20 per month pro $50 per month",
        "starter $20 per month pro $50 per month enterprise custom pricing",
    )
    assert "Enterprise custom pricing".lower() in summary.lower()
    assert "+3" in summary


def test_a_pure_removal_still_produces_a_summary():
    summary = summarise_change("soc 2 iso 27001 hipaa", "soc 2 iso 27001")
    assert "-1" in summary
    assert summary.strip()


def test_a_long_addition_is_truncated():
    summary = summarise_change("a", "a " + " ".join(f"word{i}" for i in range(200)))
    assert len(summary) < 400
    assert "…" in summary


def test_summarising_against_no_baseline_is_safe():
    assert summarise_change("", "brand new page content").strip()
    assert summarise_change("", "").strip()


# ---- fetch outcomes ---------------------------------------------------------------------------

async def test_a_missing_page_is_not_found_not_an_error():
    """Plenty of companies have no /security page. That is a fact about them, not a failure of
    ours, and conflating the two would make every small company look broken."""
    from nexus.ingestion.webwatch import check_page

    async def fetch(url):
        return 404, ""

    result = await check_page("acme.com", "security", ("/security", "/trust"), fetch=fetch)
    assert result.outcome == "not_found"


async def test_it_falls_through_to_the_next_conventional_path():
    from nexus.ingestion.webwatch import check_page

    seen: list[str] = []

    async def fetch(url):
        seen.append(url)
        return (200, "<h1>Plans</h1>") if url.endswith("/plans") else (404, "")

    result = await check_page("acme.com", "pricing", ("/pricing", "/plans"), fetch=fetch)
    assert result.outcome == "ok"
    assert result.url.endswith("/plans")
    assert seen[0].endswith("/pricing")      # tried the conventional path first


async def test_a_dead_host_never_raises():
    from nexus.ingestion.webwatch import check_page

    async def explode(url):
        raise RuntimeError("dns failure")

    result = await check_page("acme.com", "pricing", ("/pricing",), fetch=explode)
    assert result.outcome == "error"


# ---- the source (needs a baseline, so it needs the session) ------------------------------------

async def _account(tid: str):
    from nexus.models.account import Account
    from tests.conftest import tenant_session

    async with tenant_session(tid) as ts:
        acct = Account(tenant_id=tid, name="Acme Corp", domain="acme.com")
        ts.add(acct)
        await ts.flush()
        return acct.id


def _page(body: str):
    async def fetch(url):
        return (200, body) if "/pricing" in url else (404, "")
    return fetch


async def test_the_first_sighting_is_a_baseline_not_an_event():
    """"This company has a pricing page" is not news. Emitting on first sight would announce a
    change for every page of every account on the first run."""
    from nexus.ingestion.sources import WebsiteWatchSignalSource
    from nexus.models.account import Account
    from nexus.models.page_snapshot import PageSnapshot
    from tests.conftest import make_tenant, tenant_session

    tid = await make_tenant(slug="ww1")
    aid = await _account(tid)
    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == aid)
        src = WebsiteWatchSignalSource(fetch=_page("<h1>Starter $20</h1>"),
                                       page_kinds=("pricing",))
        out = await src.bind_session(ts).fetch(acct)
        assert out == []
        assert src.last_provenance["pricing"] == "baseline"
        snap = await ts.first(PageSnapshot, PageSnapshot.account_id == aid)
        assert snap is not None and snap.content_hash


async def test_an_unchanged_page_emits_nothing():
    from nexus.ingestion.sources import WebsiteWatchSignalSource
    from nexus.models.account import Account
    from tests.conftest import make_tenant, tenant_session

    tid = await make_tenant(slug="ww2")
    aid = await _account(tid)
    body = "<h1>Starter $20</h1>"
    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == aid)
        src = WebsiteWatchSignalSource(fetch=_page(body), page_kinds=("pricing",))
        await src.bind_session(ts).fetch(acct)          # baseline
        assert await src.bind_session(ts).fetch(acct) == []


async def test_a_real_change_produces_a_signal_describing_it():
    from nexus.ingestion.sources import WebsiteWatchSignalSource
    from nexus.models.account import Account
    from nexus.models.page_snapshot import PageSnapshot
    from tests.conftest import make_tenant, tenant_session

    tid = await make_tenant(slug="ww3")
    aid = await _account(tid)
    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == aid)
        src = WebsiteWatchSignalSource(fetch=_page("<h1>Starter $20</h1>"),
                                       page_kinds=("pricing",))
        await src.bind_session(ts).fetch(acct)

        changed = WebsiteWatchSignalSource(
            fetch=_page("<h1>Starter $20</h1><h2>Enterprise custom</h2>"),
            page_kinds=("pricing",),
        )
        out = await changed.bind_session(ts).fetch(acct)

    assert len(out) == 1
    assert out[0].kind == "website_change"
    assert "pricing" in out[0].title
    assert "enterprise custom" in out[0].body.lower()
    # Pricing outranks other pages: it is a commercial decision, not a copy edit.
    assert out[0].strength == 0.75

    async with tenant_session(tid) as ts:
        snap = await ts.first(PageSnapshot, PageSnapshot.account_id == aid)
        assert snap.change_count == 1
        assert snap.last_changed_at is not None


async def test_re_running_after_a_change_does_not_re_alert():
    """The dedupe key is the new digest, so the sweep is idempotent but a genuinely new change
    still fires."""
    from nexus.ingestion.sources import WebsiteWatchSignalSource
    from nexus.models.account import Account
    from tests.conftest import make_tenant, tenant_session

    tid = await make_tenant(slug="ww4")
    aid = await _account(tid)
    async with tenant_session(tid) as ts:
        acct = await ts.first(Account, Account.id == aid)
        await WebsiteWatchSignalSource(
            fetch=_page("<p>a</p>"), page_kinds=("pricing",)
        ).bind_session(ts).fetch(acct)
        first = await WebsiteWatchSignalSource(
            fetch=_page("<p>a b</p>"), page_kinds=("pricing",)
        ).bind_session(ts).fetch(acct)
        second = await WebsiteWatchSignalSource(
            fetch=_page("<p>a b</p>"), page_kinds=("pricing",)
        ).bind_session(ts).fetch(acct)

    assert len(first) == 1 and second == []


async def test_without_a_session_it_emits_nothing():
    """No session means no baseline, and without a baseline every page looks new."""
    from nexus.ingestion.sources import WebsiteWatchSignalSource
    from nexus.models.account import Account

    src = WebsiteWatchSignalSource(fetch=_page("<p>x</p>"))
    assert await src.fetch(Account(tenant_id="t", name="Acme", domain="acme.com")) == []


def test_it_is_opt_in(monkeypatch):
    """Up to four page fetches per account per refresh — the heaviest source in the pipeline."""
    from nexus.core.config import get_settings
    from nexus.ingestion.service import get_ingestion_service, set_ingestion_service
    from nexus.ingestion.sources import WebsiteWatchSignalSource

    settings = get_settings()
    monkeypatch.setattr(settings, "signal_sources", "web,rss")
    set_ingestion_service(None)
    try:
        assert not any(
            isinstance(s, WebsiteWatchSignalSource) for s in get_ingestion_service().sources
        )
        monkeypatch.setattr(settings, "signal_sources", "web,website")
        set_ingestion_service(None)
        assert any(
            isinstance(s, WebsiteWatchSignalSource) for s in get_ingestion_service().sources
        )
    finally:
        set_ingestion_service(None)
