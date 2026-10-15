"""The two clean-ups for what was stored before the 2026-10-15 accuracy fixes.

Both are dry runs until asked, and both touch only what the product itself got wrong: a funding
signal the classifier now reads differently, and an account discovery added that is a publisher.
"""
from __future__ import annotations

from scripts.archive_publisher_accounts import sweep as archive_sweep
from scripts.reclassify_funding_signals import reclassified
from scripts.reclassify_funding_signals import sweep as reclassify_sweep
from nexus.models.account import Account
from nexus.models.signal import SignalEvent
from tests.conftest import make_tenant, tenant_session


async def _signals(ts, tid):
    acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
    ts.add(acc)
    await ts.flush()
    rows = {
        "toe": ("rss", "Why You Should Do Toe Raises, Not Just Calf Raises", "https://x.com/a"),
        "creator": ("rss", "The series creator noted the date", "https://x.com/b"),
        "latka": ("web_news", "Acme Revenue 2024: $170M, $101M Raised - LATKA",
                  "https://getlatka.com/companies/acme"),
        "real": ("web_news", "Acme raises $40M Series B", "https://techcrunch.com/acme"),
        "outlook": ("dork", "Acme posts results, raises outlook", "https://x.com/c"),
    }
    out = {}
    for key, (source, title, url) in rows.items():
        sig = SignalEvent(tenant_id=tid, account_id=acc.id, kind="funding", source=source,
                          title=title, url=url, strength=0.85, dedupe_key=f"k:{key}")
        ts.add(sig)
        out[key] = sig
    await ts.flush()
    return out


def test_today_s_rules_decide_what_changes():
    assert reclassified("rss", "Acme raises $40M Series B", None) is None
    assert reclassified("web_news", "Acme raises $40M", "https://getlatka.com/companies/acme") \
        == ("news", 0.4)
    assert reclassified("dork", "Acme raises outlook", None)[0] != "funding"


async def test_a_dry_run_reports_and_writes_nothing():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        sigs = await _signals(ts, tid)
        result = await reclassify_sweep(session=ts.session)
        assert result["scanned"] == 5 and result["changed"] == 4
        assert {s.kind for s in sigs.values()} == {"funding"}, "a dry run wrote"


async def test_apply_changes_kind_and_strength_only():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        sigs = await _signals(ts, tid)
        await reclassify_sweep(apply=True, session=ts.session)
        assert sigs["real"].kind == "funding" and sigs["real"].strength == 0.85
        for key in ("toe", "creator", "latka", "outlook"):
            assert sigs[key].kind != "funding", key
            assert sigs[key].strength < 0.85, key
        assert sigs["latka"].title.startswith("Acme Revenue"), "the title must be untouched"
        assert (await reclassify_sweep(session=ts.session))["changed"] == 0, "not idempotent"


async def _accounts(ts, tid):
    rows = {
        "wapo": ("washingtonpost.com", "Newspaper Publishing", "auto_discovery"),
        "time": ("time.com", "Book and Periodical Publishing", "discovery"),
        "deadline": ("deadline.com", "Online Audio and Video Media", "auto_discovery"),
        "saas": ("acme.io", "Software Development", "auto_discovery"),
        "imported": ("nyt.com", "Newspaper Publishing", "csv_import"),
    }
    out = {}
    for key, (domain, industry, source) in rows.items():
        acc = Account(tenant_id=tid, name=domain, domain=domain, industry=industry, source=source)
        ts.add(acc)
        out[key] = acc
    await ts.flush()
    return out


async def test_only_discovered_publishers_are_archived():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        accounts = await _accounts(ts, tid)
        dry = await archive_sweep(session=ts.session)
        assert len(dry["archived"]) == 2 and not any(a.is_archived for a in accounts.values())
        assert dry["review"] == ["deadline.com (Online Audio and Video Media)"]

        await archive_sweep(apply=True, session=ts.session)
        assert accounts["wapo"].is_archived and accounts["time"].is_archived
        assert "publisher" in accounts["wapo"].custom_fields["archived_reason"]
        assert not accounts["deadline"].is_archived, "a review industry was archived"
        assert not accounts["saas"].is_archived
        assert not accounts["imported"].is_archived, "a customer's own import was archived"
        assert (await archive_sweep(session=ts.session))["archived"] == []
