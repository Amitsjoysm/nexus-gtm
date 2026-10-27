"""Drafts say only what is true and worth saying (SDR feedback, 2026-10-26, D8).

Reported: AI personalization was generic and sometimes wrong. Measured: "Industry" and "Location"
were often the ICP's own words stamped onto a web find, "Known tech" listed DMARC and Let's Encrypt,
57% of accounts with signals had only generic news among the eight a draft read, and the person's
job title counted as "a fact used", so a draft with no real observation passed the check.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from nexus.agents.copy import account_facts
from nexus.agents.facts import citable_signals, looks_stamped, useful_tech
from nexus.models.account import Account

ICP = {"industries": ["Oil & Gas", "Manufacturing"], "countries": ["United States"]}
BEFORE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _web_find(**kw):
    a = Account(tenant_id="t", name="Lincoln Mfg", domain="lincolnmfg.com",
                source="auto_discovery", industry="Oil & Gas", country="United States", **kw)
    a.created_at = BEFORE
    return a


def test_infrastructure_is_not_tech_worth_mentioning():
    assert useful_tech(["DMARC", "Let's Encrypt", "HubSpot", "Cloudflare", "Salesforce"]) == [
        "HubSpot", "Salesforce"]
    assert "DMARC" not in account_facts(Account(tenant_id="t", name="A",
                                                tech_stack=["DMARC", "HubSpot"]))


def test_a_stamped_industry_and_country_are_left_out():
    account = _web_find()
    assert looks_stamped(account, "industry", ICP) and looks_stamped(account, "country", ICP)

    text = account_facts(account, ICP)

    assert "Oil & Gas" not in text and "United States" not in text
    assert "Company: Lincoln Mfg" in text


def test_a_label_with_a_recorded_source_is_kept():
    account = _web_find(field_sources={"industry": {"source": "linkedin"}})
    assert not looks_stamped(account, "industry", ICP)
    assert "Industry: Oil & Gas" in account_facts(account, ICP)


def test_a_web_find_after_the_fix_and_other_sources_are_never_suspected():
    later = _web_find()
    later.created_at = datetime(2026, 10, 27, tzinfo=timezone.utc)
    imported = _web_find()
    imported.source = "csv_import"
    other = _web_find()
    other.industry = "Machinery Manufacturing"       # not the ICP's first industry

    assert not any(looks_stamped(a, "industry", ICP) for a in (later, imported, other))


def _signal(kind, strength, days):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(kind=kind, strength=strength, title=f"{kind} {strength}",
                           occurred_at=now - timedelta(days=days), dated="event", body=None)


def test_a_weak_mention_is_not_a_reason_to_write(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "signal_window_default", 0)   # all time
    kept = citable_signals([_signal("funding", 0.85, 3), _signal("news", 0.4, 1),
                            _signal("hiring", 0.6, 20)])

    assert [s.kind for s in kept] == ["funding", "hiring"]


# ---- campaign drafts ----------------------------------------------------------------------------

async def _pack(slug, *, signals=(), title="Operations Manager"):
    from nexus.engagement.drafting.context import build_context
    from nexus.models.account import Contact
    from nexus.models.engagement import MailboxConnection
    from nexus.models.signal import SignalEvent
    from tests.conftest import make_tenant, seed_relevance_profile, tenant_session

    from nexus.models.identity import User

    tid = await make_tenant(slug)
    async with tenant_session(tid) as ts:
        await seed_relevance_profile(ts, icp=ICP)
        user = User(email=f"sam@{slug}.io", full_name="Sam Rep", password_hash="x")
        ts.session.add(user)
        await ts.session.flush()
        account = Account(tenant_id=tid, name="Acme Ops", domain="acme-ops.io",
                          tech_stack=["DMARC", "HubSpot"])
        mailbox = MailboxConnection(owner_user_id=user.id, provider="google",
                                    email=f"sam@{slug}.io",
                                    display_name="Sam Rep", status="connected",
                                    timezone="UTC")
        ts.add(account)
        ts.add(mailbox)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=account.id, full_name="Ana Example",
                          title=title, email="ana@acme-ops.io")
        ts.add(contact)
        for i, (kind, strength, url) in enumerate(signals):
            ts.add(SignalEvent(account_id=account.id, kind=kind, source="web", strength=strength,
                               title=f"Acme Ops {kind}", url=url, dated="event",
                               occurred_at=datetime.now(timezone.utc) - timedelta(days=3 + i),
                               dedupe_key=f"{slug}-{i}"))
        await ts.flush()
        return await build_context(ts, enrollment=SimpleNamespace(contact_timezone="UTC"),
                                   contact=contact, account=account, mailbox=mailbox)


async def test_a_campaign_draft_cites_events_not_mentions():
    pack = await _pack("df1", signals=[("funding", 0.85, "https://news.example/acme-round"),
                                       ("news", 0.4, "https://acme-ops.io/blog")])

    assert "Acme Ops funding" in pack.text and "Acme Ops news" not in pack.text
    assert "DMARC" not in pack.text


async def test_the_title_is_a_fact_only_when_nothing_else_is_known():
    with_signal = await _pack("df2", signals=[("funding", 0.85, "https://news.example/r")])
    bare = await _pack("df3")

    assert "Operations Manager" not in with_signal.facts and with_signal.facts
    assert bare.facts == ["Operations Manager"]


async def test_the_facts_offered_carry_their_links():
    pack = await _pack("df4", signals=[("funding", 0.85, "https://news.example/acme-round")])

    assert {"label": "Acme Ops funding", "url": "https://news.example/acme-round",
            "kind": "signal"} in pack.sources


async def test_the_review_queue_shows_what_each_draft_rests_on():
    from nexus.engagement.sequences.service import (
        create_campaign,
        draft_first_emails,
        enroll,
        review_queue,
    )
    from tests.conftest import tenant_session
    from tests.test_engagement_sequences import STEPS, _world

    tid, user_id, mailbox_id, ids = await _world("df5", contacts=1)
    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Q4", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        await enroll(ts, campaign, ids)
        await draft_first_emails(ts, campaign, user_id=user_id)
        (_enrollment, message), = await review_queue(ts, campaign)

    assert {"label": "Acme Robotics raises $40M Series B", "url": "", "kind": "signal"} \
        in message.draft_facts


class _Profiles:
    def __init__(self):
        self.asked = []

    async def fetch(self, *, full_name, linkedin_url, social_urls=None):
        from nexus.personalization.provider import PersonInsights

        self.asked.append(linkedin_url)
        return PersonInsights(headline="Scaling field operations", source="test")


async def _draft_for(slug, monkeypatch, *, on, stored=None):
    from nexus.core.config import get_settings
    from nexus.engagement.drafting.drafter import _fresh_insights
    from nexus.models.account import Contact
    from nexus.personalization import provider
    from tests.conftest import make_tenant, tenant_session

    profiles = _Profiles()
    monkeypatch.setattr(provider, "get_personalization_provider", lambda: profiles)
    monkeypatch.setattr(get_settings(), "personalization_before_campaign_draft", on)
    tid = await make_tenant(slug)
    async with tenant_session(tid) as ts:
        account = Account(tenant_id=tid, name="Acme", domain="acme.io")
        ts.add(account)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=account.id, full_name="Ana Example",
                          linkedin_url="https://www.linkedin.com/in/ana-example",
                          custom_fields={"personalization": stored} if stored else {})
        ts.add(contact)
        await ts.flush()
        await _fresh_insights(ts, contact)
        return profiles.asked, (contact.custom_fields or {}).get("personalization")


async def test_linkedin_is_read_before_a_campaign_draft_when_turned_on(monkeypatch):
    asked, stored = await _draft_for("df6", monkeypatch, on=True)
    assert asked == ["https://www.linkedin.com/in/ana-example"]
    assert stored["headline"] == "Scaling field operations"


async def test_it_is_not_read_when_off_or_recently_read(monkeypatch):
    from datetime import datetime, timezone

    asked, _ = await _draft_for("df7", monkeypatch, on=False)
    assert asked == []
    fresh = {"headline": "x", "fetched_at": datetime.now(timezone.utc).isoformat()}
    asked, _ = await _draft_for("df8", monkeypatch, on=True, stored=fresh)
    assert asked == [], "read within 30 days: not bought again"
