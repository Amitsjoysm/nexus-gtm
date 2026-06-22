"""Person-level personalization: role angle, contact-scoped signal preference, social insights,
and the Apify-ready provider seam. Offline."""
from __future__ import annotations

from types import SimpleNamespace

from nexus.models.account import Account, Contact
from nexus.personalization.brief import _role_angle, build_person_brief
from nexus.personalization.provider import (
    PersonalizationProvider,
    PersonInsights,
    StubPersonalizationProvider,
    refresh_person_insights,
    set_personalization_provider,
)
from tests.conftest import make_tenant, tenant_session


def _contact(**kw):
    base = dict(id="c1", full_name="Jane Doe", title="VP Sales", seniority="VP",
                linkedin_url=None, custom_fields={})
    base.update(kw)
    return SimpleNamespace(**base)


def _sig(title, strength, contact_id=None):
    return SimpleNamespace(title=title, strength=strength, contact_id=contact_id)


def test_role_angle_maps_persona():
    assert "function-level" in _role_angle("VP Sales", "VP")
    assert "executive" in _role_angle("CEO & Founder", "C-Level")
    assert "operational" in _role_angle("Sales Manager", "Manager")
    assert "process efficiency" in _role_angle("Head of RevOps", None)
    assert _role_angle(None, None)  # non-empty default


def test_brief_prefers_a_signal_tied_to_the_person():
    c = _contact()
    signals = [
        _sig("Company raised Series B", 0.9, contact_id=None),   # stronger, but account-level
        _sig("Jane was promoted to VP", 0.5, contact_id="c1"),   # weaker, but about her
    ]
    brief = build_person_brief(c, account=None, signals=signals)
    assert brief.signal_title == "Jane was promoted to VP"
    assert brief.signal_is_personal is True


def test_brief_falls_back_to_account_signal():
    c = _contact()
    brief = build_person_brief(c, None, [_sig("Company raised Series B", 0.9)])
    assert brief.signal_title == "Company raised Series B"
    assert brief.signal_is_personal is False


def test_brief_prompt_folds_in_person_and_insights():
    c = _contact(title="Head of RevOps", custom_fields={
        "personalization": {
            "headline": "Scaling RevOps at Acme",
            "recent_posts": ["Hiring 5 SDRs this quarter", "Loving our new data stack", "extra"],
            "interests": ["pipeline efficiency"],
        }
    })
    prompt = build_person_brief(c, None, []).to_prompt(max_posts=2)
    assert "Jane Doe" in prompt and "Head of RevOps" in prompt
    assert "Scaling RevOps at Acme" in prompt
    assert "Hiring 5 SDRs this quarter" in prompt
    assert "extra" not in prompt           # capped at max_posts=2
    assert "process efficiency" in prompt  # role angle present


async def test_refresh_insights_stub_is_noop():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
        ts.add(acc)
        await ts.flush()
        c = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe", custom_fields={})
        ts.add(c)
        await ts.flush()
        set_personalization_provider(StubPersonalizationProvider())
        try:
            out = await refresh_person_insights(ts, c)
        finally:
            set_personalization_provider(StubPersonalizationProvider())
    assert out is None
    assert "personalization" not in (c.custom_fields or {})


async def test_refresh_insights_persists_from_provider():
    class _FakeApify(PersonalizationProvider):
        name = "fake"

        async def fetch(self, *, full_name, linkedin_url=None, social_urls=None):
            return PersonInsights(headline="VP Sales @ Acme",
                                  recent_posts=["Just hit 120% of quota"], source="fake")

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
        ts.add(acc)
        await ts.flush()
        c = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe", custom_fields={})
        ts.add(c)
        await ts.flush()
        set_personalization_provider(_FakeApify())
        try:
            out = await refresh_person_insights(ts, c)
        finally:
            set_personalization_provider(StubPersonalizationProvider())
    assert out is not None
    p = (c.custom_fields or {}).get("personalization")
    assert p and p["recent_posts"] == ["Just hit 120% of quota"]
    assert p["fetched_at"] and p["source"] == "fake"
