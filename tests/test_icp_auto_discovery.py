"""Daily ICP auto-discovery: strict ICP matching, dedupe, cap, idempotency. Offline."""
from __future__ import annotations

from nexus.discovery.auto import auto_discover_for_tenant
from nexus.integrations.company_search import CompanyCandidate
from nexus.models.account import Account
from nexus.models.intelligence import AccountScore
from nexus.models.relevance import RelevanceProfile
from nexus.workers.tasks import handle_discover_icp_accounts
from tests.conftest import make_tenant, tenant_session

_ICP = {
    "industries": ["saas"],
    "employee_min": 50,
    "employee_max": 5000,
    "countries": ["united states"],
}


def _searcher(candidates, *, seen: list | None = None):
    """A fake company search. Records the ``exclude_domains`` it was called with (when ``seen`` is
    given) so a test can assert the driver pushes already-tracked domains to the backend."""
    async def _search(icp, *, limit, exclude_domains=None):
        if seen is not None:
            seen.append(exclude_domains)
        excl = {d.lower() for d in (exclude_domains or [])}
        return [c for c in candidates if (c.domain or "").lower() not in excl][:limit]

    return _search


async def _with_profile(ts, tid):
    ts.add(RelevanceProfile(tenant_id=tid, icp=_ICP, value_props=[], product_context=""))
    await ts.flush()


async def test_keeps_only_strict_icp_matches():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)
        cands = [
            CompanyCandidate(name="Match Co", domain="match.com", industry="SaaS",
                             country="United States", employee_count=200),       # strong fit -> kept
            CompanyCandidate(name="Wrong Industry", domain="retail.com", industry="Retail",
                             country="France", employee_count=200),              # low fit -> screened out
            CompanyCandidate(name="Too Big", domain="big.com", industry="SaaS",
                             country="United States", employee_count=999999),    # out of band -> hard-filtered
        ]
        res = await auto_discover_for_tenant(
            ts, target_count=10, min_fit=60, pool_limit=50, search=_searcher(cands)
        )
        accounts = await ts.list(Account)
        scores = await ts.list(AccountScore)

    assert res["discovered"] == 1
    assert [a.domain for a in accounts] == ["match.com"]
    assert accounts[0].source == "auto_discovery"
    assert len(scores) == 1 and scores[0].composite >= 60  # ICP-fit persisted for the list badge


async def test_dedupes_existing_domain():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)
        ts.add(Account(tenant_id=tid, name="Already", domain="match.com"))
        await ts.flush()
        res = await auto_discover_for_tenant(
            ts, target_count=10, min_fit=60, pool_limit=50,
            search=_searcher([CompanyCandidate(name="Match", domain="match.com", industry="SaaS",
                                               country="United States", employee_count=200)]),
        )
    assert res["discovered"] == 0  # the existing domain is never re-surfaced


async def test_excludes_tracked_domains_so_repeat_runs_find_new_companies():
    """Regression for daily discovery drying up: the driver must tell the search backend which
    domains it already tracks (exclude_domains) so each run reaches NET-NEW companies."""
    tid = await make_tenant()
    pool = [
        CompanyCandidate(name=f"Co{i}", domain=f"co{i}.com", industry="SaaS",
                         country="United States", employee_count=100)
        for i in range(5)
    ]
    seen_exclusions: list = []
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)
        # Run 1: nothing tracked yet -> adds the first two.
        r1 = await auto_discover_for_tenant(
            ts, target_count=2, min_fit=60, pool_limit=50,
            search=_searcher(pool, seen=seen_exclusions),
        )
        # Run 2: the two added are now tracked -> must be excluded, so we get the NEXT two.
        r2 = await auto_discover_for_tenant(
            ts, target_count=2, min_fit=60, pool_limit=50,
            search=_searcher(pool, seen=seen_exclusions),
        )
        domains = sorted(a.domain for a in await ts.list(Account))

    assert r1["discovered"] == 2 and r2["discovered"] == 2
    assert domains == ["co0.com", "co1.com", "co2.com", "co3.com"]  # run 2 found new, not dupes
    # Run 1 saw no exclusions; run 2 was told about run 1's domains.
    assert seen_exclusions[0] in (None, [])
    assert set(seen_exclusions[1] or []) >= {"co0.com", "co1.com"}


async def test_respects_target_count_cap():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)
        cands = [
            CompanyCandidate(name="A", domain="a.com", industry="SaaS", country="United States", employee_count=100),
            CompanyCandidate(name="B", domain="b.com", industry="SaaS", country="United States", employee_count=100),
        ]
        res = await auto_discover_for_tenant(
            ts, target_count=1, min_fit=60, pool_limit=50, search=_searcher(cands)
        )
    assert res["discovered"] == 1  # stopped at the daily cap


async def test_enriches_candidates_before_scoring(monkeypatch):
    """Search returns sparse firmographics (no headcount/tech); our crawler must fill them BEFORE
    scoring so a candidate that would miss the bar un-enriched clears it once enriched."""
    from nexus.core.config import get_settings
    from nexus.enrichment.account import set_account_enricher

    icp = {"industries": ["saas"], "employee_min": 50, "employee_max": 5000,
           "countries": ["united states"], "required_tech": ["aws"]}

    class _FakeEnricher:  # stands in for the web crawler — fills the blanks search left
        async def enrich(self, ts, account, **kw):
            account.employee_count = 200
            account.tech_stack = ["aws"]
            return ["employee_count", "tech_stack"]

        async def enrich_batch(self, ts, accounts, *, concurrency, user_id=None, meter=True):
            # Discovery enriches candidates through the batch entry point and passes
            # `meter=False`: the candidates are weighed, most are discarded, and the sweep bills
            # `discovery.account_added` for the ones it actually delivers instead.
            for account in accounts:
                await self.enrich(ts, account)

    s = get_settings()
    monkeypatch.setattr(s, "icp_discovery_enrich_candidates", True)
    set_account_enricher(_FakeEnricher())
    try:
        tid = await make_tenant()
        async with tenant_session(tid) as ts:
            ts.add(RelevanceProfile(tenant_id=tid, icp=icp, value_props=[], product_context=""))
            await ts.flush()
            # Sparse candidate: right industry/geo but no headcount, no tech → ~63 un-enriched (< 70).
            cand = CompanyCandidate(name="Sparse", domain="sparse.com", industry="SaaS",
                                    country="United States", employee_count=None)
            res = await auto_discover_for_tenant(
                ts, target_count=5, min_fit=70, pool_limit=50, search=_searcher([cand])
            )
            accts = await ts.list(Account)
            scores = await ts.list(AccountScore)
    finally:
        set_account_enricher(None)

    assert res["discovered"] == 1  # crawled headcount + tech pushed it over min_fit
    assert accts[0].employee_count == 200 and accts[0].tech_stack == ["aws"]
    assert scores[0].composite >= 70


async def test_no_icp_is_a_noop():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        res = await auto_discover_for_tenant(
            ts, target_count=10, min_fit=60, pool_limit=50,
            search=_searcher([CompanyCandidate(name="X", domain="x.com")]),
        )
    assert res["discovered"] == 0 and res.get("skipped") == "no_icp"


async def test_handler_skips_when_disabled(monkeypatch):
    # Explicitly off (don't rely on ambient .env, where these switches may be enabled):
    # with either flag False the handler must do no work and touch no network.
    from nexus.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "automation_enabled", False)
    monkeypatch.setattr(s, "icp_discovery_enabled", False)
    res = await handle_discover_icp_accounts({})
    assert res == {"skipped": "disabled"}


async def test_handler_no_icp_tenant_does_not_consume_daily_slot(monkeypatch):
    """A tenant with automation on but no ICP yet is re-checked cheaply each tick (no network),
    and its per-interval slot is NOT consumed — so discovery fires the moment an ICP is added."""
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    s = get_settings()
    monkeypatch.setattr(s, "automation_enabled", True)
    monkeypatch.setattr(s, "icp_discovery_enabled", True)

    tid = await make_tenant()
    async with get_sessionmaker()() as sess:
        t = await sess.get(Tenant, tid)
        t.automation_enabled = True  # opted in, but no RelevanceProfile/ICP defined
        await sess.commit()

    await handle_discover_icp_accounts({})

    async with get_sessionmaker()() as sess:
        t = await sess.get(Tenant, tid)
        assert t.icp_discovery_last_run_at is None  # slot preserved (no ICP -> no run)


async def test_handler_uses_per_tenant_daily_count(monkeypatch):
    """The SDR-selected per-workspace daily target overrides the platform default."""
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    s = get_settings()
    monkeypatch.setattr(s, "automation_enabled", True)
    monkeypatch.setattr(s, "icp_discovery_enabled", True)

    tid = await make_tenant()
    async with get_sessionmaker()() as sess:
        t = await sess.get(Tenant, tid)
        t.automation_enabled = True
        t.icp_daily_count = 5
        await sess.commit()
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)

    calls: list[dict] = []

    async def fake_discover(ts, *, target_count, min_fit, pool_limit, search=None):
        calls.append({"target": target_count, "pool": pool_limit})
        return {"discovered": 0, "screened": 0, "account_ids": []}

    import nexus.discovery.auto as auto_mod

    monkeypatch.setattr(auto_mod, "auto_discover_for_tenant", fake_discover)
    await handle_discover_icp_accounts({})

    assert calls and calls[0]["target"] == 5
    assert calls[0]["pool"] == 5 * s.icp_discovery_pool_multiplier


async def test_handler_raises_paused_alert_once_for_no_icp_tenant(monkeypatch):
    """A no-ICP tenant gets ONE standing in-app alert (not one per tick), and it auto-acks
    once discovery actually runs."""
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.alerts import Alert
    from nexus.models.identity import Tenant

    s = get_settings()
    monkeypatch.setattr(s, "automation_enabled", True)
    monkeypatch.setattr(s, "icp_discovery_enabled", True)

    tid = await make_tenant()
    async with get_sessionmaker()() as sess:
        t = await sess.get(Tenant, tid)
        t.automation_enabled = True  # opted in, no ICP defined
        await sess.commit()

    await handle_discover_icp_accounts({})
    await handle_discover_icp_accounts({})  # second tick must not duplicate

    async with tenant_session(tid) as ts:
        alerts = await ts.list(Alert, Alert.source == "icp_discovery", Alert.status == "open")
        assert len(alerts) == 1
        assert "no ICP" in alerts[0].title

    # Define the ICP -> next run resolves the standing alert.
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)

    async def fake_discover(ts, *, target_count, min_fit, pool_limit, search=None):
        return {"discovered": 0, "screened": 0, "account_ids": []}

    import nexus.discovery.auto as auto_mod

    monkeypatch.setattr(auto_mod, "auto_discover_for_tenant", fake_discover)
    await handle_discover_icp_accounts({})

    async with tenant_session(tid) as ts:
        assert await ts.list(Alert, Alert.source == "icp_discovery", Alert.status == "open") == []


# ---- post-enrichment ICP re-screen (pipeline calls this after the crawler fills headcount) ----

async def _discovered_account(ts, tid, *, employee_count=None, source="auto_discovery"):
    acc = Account(
        tenant_id=tid, name="Screened Co", domain="screened.example",
        industry="SaaS", employee_count=employee_count, source=source,
    )
    ts.add(acc)
    await ts.flush()
    return acc


async def test_rescreen_archives_out_of_band_discovered_account():
    from nexus.discovery.auto import rescreen_discovered_account

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)
        acc = await _discovered_account(ts, tid, employee_count=8)  # band is 50-5000
        assert await rescreen_discovered_account(ts, acc) is True
        cf = acc.custom_fields or {}
        assert cf.get("archived") is True
        assert cf.get("archived_reason") == "icp_size_band"
        # Idempotent: already archived -> no second action.
        assert await rescreen_discovered_account(ts, acc) is False


async def test_rescreen_keeps_in_band_and_unknown_headcount():
    from nexus.discovery.auto import rescreen_discovered_account

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)
        in_band = await _discovered_account(ts, tid, employee_count=200)
        assert await rescreen_discovered_account(ts, in_band) is False
        unknown = Account(tenant_id=tid, name="U", domain="u.example",
                          source="auto_discovery", employee_count=None)
        ts.add(unknown)
        await ts.flush()
        assert await rescreen_discovered_account(ts, unknown) is False
        assert not (in_band.custom_fields or {}).get("archived")


async def test_rescreen_never_touches_manual_accounts():
    from nexus.discovery.auto import rescreen_discovered_account

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)
        manual = await _discovered_account(ts, tid, employee_count=8, source=None)
        assert await rescreen_discovered_account(ts, manual) is False
        assert not (manual.custom_fields or {}).get("archived")


async def test_rescreen_spares_engaged_accounts():
    from nexus.discovery.auto import rescreen_discovered_account
    from nexus.models.account import Contact

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)
        acc = await _discovered_account(ts, tid, employee_count=8)
        ts.add(Contact(tenant_id=tid, account_id=acc.id, full_name="Working Rep Contact"))
        await ts.flush()
        assert await rescreen_discovered_account(ts, acc) is False
        assert not (acc.custom_fields or {}).get("archived")


# ---- reaching the number set in Settings (2026-09-30) ---------------------------------------------
# Reported: "new accounts matching the ICP are not updating daily to the number set in Settings".
# Measured on the local deploy: a workspace set to 10 a day received 1-3 a day for two weeks. The
# sweep ran once, delivered what one pass found, consumed the day's slot and recorded nothing, so a
# short day was neither topped up nor explained.

from datetime import datetime, timedelta, timezone  # noqa: E402

T0 = datetime(2026, 9, 30, 7, 0, tzinfo=timezone.utc)


async def _opted_in(monkeypatch, daily: int = 5, slug: str | None = None) -> str:
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker
    from nexus.models.identity import Tenant

    s = get_settings()
    monkeypatch.setattr(s, "automation_enabled", True)
    monkeypatch.setattr(s, "icp_discovery_enabled", True)
    monkeypatch.setattr(s, "icp_discovery_max_attempts", 3)
    monkeypatch.setattr(s, "icp_discovery_retry_hours", 3)
    tid = await make_tenant(slug) if slug else await make_tenant()
    async with get_sessionmaker()() as sess:
        t = await sess.get(Tenant, tid)
        t.automation_enabled = True
        t.icp_daily_count = daily
        await sess.commit()
    async with tenant_session(tid) as ts:
        await _with_profile(ts, tid)
    return tid


def _fake(monkeypatch, answers: list, calls: list):
    """Each call takes the next answer: an int is how many were delivered (with a failed web
    search, so a retry is worthwhile), a dict is returned as is, an exception is raised."""
    import nexus.discovery.auto as auto_mod

    async def fake(ts, *, target_count, min_fit, pool_limit, search=None):
        calls.append((ts.tenant_id, target_count))
        answer = answers.pop(0) if answers else 0
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, dict):
            return answer
        return {"discovered": answer, "screened": answer, "account_ids": [],
                "sources": {"web": answer}, "discarded": {"low_fit": 4},
                "notes": {"web": "failed"}}

    monkeypatch.setattr(auto_mod, "auto_discover_for_tenant", fake)


async def _tick(at: datetime):
    await handle_discover_icp_accounts({"now_iso": at.isoformat()})


async def test_a_short_day_is_topped_up_later_the_same_day(monkeypatch):
    tid = await _opted_in(monkeypatch, daily=5)
    calls: list = []
    _fake(monkeypatch, [2, 3], calls)

    await _tick(T0)
    await _tick(T0 + timedelta(hours=1))           # inside the retry gap: nothing
    await _tick(T0 + timedelta(hours=3, minutes=1))
    await _tick(T0 + timedelta(hours=7))           # the day's number is met: nothing
    assert [n for _t, n in calls] == [5, 3]

    await _tick(T0 + timedelta(hours=24, minutes=1))
    assert [n for _t, n in calls] == [5, 3, 5], "a new day asks for the full number again"
    assert tid in {t for t, _ in calls}


async def test_a_market_with_nothing_new_is_not_asked_again_the_same_day(monkeypatch):
    await _opted_in(monkeypatch, daily=5)
    calls: list = []
    nothing = {"discovered": 0, "screened": 0, "account_ids": [], "sources": {},
               "discarded": {"already_held": 30}, "notes": {"linkedin": "nothing_new"}}
    _fake(monkeypatch, [nothing], calls)

    await _tick(T0)
    await _tick(T0 + timedelta(hours=4))
    assert len(calls) == 1, "a pass that found nothing new and hit no failure is the answer"


async def test_top_ups_are_capped_per_day(monkeypatch):
    await _opted_in(monkeypatch, daily=10)
    calls: list = []
    _fake(monkeypatch, [1, 1, 1, 1, 1], calls)
    for hours in (0, 3.1, 6.2, 9.3, 12.4):
        await _tick(T0 + timedelta(hours=hours))
    assert [n for _t, n in calls] == [10, 9, 8], "at most icp_discovery_max_attempts a day"


async def test_one_failing_workspace_does_not_stop_the_others(monkeypatch):
    a = await _opted_in(monkeypatch, daily=5, slug="icpfaila")
    b = await _opted_in(monkeypatch, daily=5, slug="icpfailb")
    calls: list = []
    import nexus.discovery.auto as auto_mod

    async def fake(ts, *, target_count, min_fit, pool_limit, search=None):
        calls.append(ts.tenant_id)
        if ts.tenant_id == a:
            raise RuntimeError("provider exploded")
        return {"discovered": 5, "screened": 5, "account_ids": []}

    monkeypatch.setattr(auto_mod, "auto_discover_for_tenant", fake)
    await _tick(T0)
    assert set(calls) == {a, b}
    await _tick(T0 + timedelta(minutes=5))
    assert calls.count(a) == 1, "a failed workspace waits out the retry gap, it is not hammered"


async def test_each_pass_is_recorded_with_its_reasons(monkeypatch):
    from nexus.models.prospecting import ProspectRun

    tid = await _opted_in(monkeypatch, daily=5)
    _fake(monkeypatch, [2], [])
    await _tick(T0)
    async with tenant_session(tid) as ts:
        [run] = await ts.list(ProspectRun, ProspectRun.kind == "daily")
    assert (run.requested, run.delivered, run.status) == (5, 2, "done")
    assert run.notes == {"web": "failed"} and run.discarded == {"low_fit": 4}


async def test_settings_show_what_today_delivered_and_why_it_fell_short(client, monkeypatch):
    from nexus.core.security import create_access_token

    tid = await _opted_in(monkeypatch, daily=5)
    _fake(monkeypatch, [2], [])
    await handle_discover_icp_accounts({})
    token = create_access_token(user_id="owner-user", tenant_id=tid, role="owner")
    r = await client.get("/api/workspace/automation", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    today = r.json()["today"]
    assert (today["target"], today["delivered"], today["attempts"]) == (5, 2, 1)
    assert "web search" in today["short_reason"] and "below your fit threshold" in today["short_reason"]


def test_the_settings_screen_shows_today():
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages"
            / "SettingsPage.tsx").read_text(encoding="utf-8")
    assert "settings.today" in page
