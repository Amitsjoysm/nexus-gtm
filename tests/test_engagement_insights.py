"""Insights for the engagement screens (spec §18.5, D25, D26).

The display rules are pure and tested at 1, 2 and 3 workspaces (spec §14). The endpoint runs on the
offline suite with no insights store configured, which is also a real case: a deployment that has
not set the store up still gets a likelihood from its own data. Reading profiles from a real store
is covered in `tests_integration/test_ledger_stores_pg.py`.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session


def _profile(workspaces: int, **over) -> dict:
    base = {"person_email": "jane@acme.io", "company_domain": "acme.io", "best_weekday": 1,
            "best_hour": 10, "median_response_s": 4 * 3600, "reply_propensity": 0.2,
            "last_reply_band": "same_day", "workspace_count": workspaces}
    return {**base, **over}


# ---- the display rules (D26) ---------------------------------------------------------------------

@pytest.mark.parametrize("workspaces", [1, 2])
def test_below_three_workspaces_only_the_last_replys_speed_band_is_shown(workspaces):
    from nexus.engagement.insights.rules import describe

    insight = describe(_profile(workspaces), viewer_consented=True)
    assert insight.level == "band" and insight.text == "Last replied the same day"
    # No weekday, hour, typical time or reply rate: any of them is a pattern.
    assert (insight.best_weekday, insight.best_hour, insight.typical_response_hours,
            insight.propensity) == (None, None, None, None)


def test_at_three_workspaces_the_pattern_is_shown():
    from nexus.engagement.insights.rules import describe

    insight = describe(_profile(3), viewer_consented=True)
    assert insight.level == "pattern"
    assert insight.text == "Usually replies on Tuesdays around 10am, typically within 4 hours"
    assert insight.propensity == 0.2


def test_a_workspace_that_has_not_opted_in_receives_nothing():
    from nexus.engagement.insights.rules import describe

    assert describe(_profile(9), viewer_consented=False).level == "none"
    assert describe(None, viewer_consented=True).level == "none"


def test_a_company_pattern_needs_three_workspaces_and_has_no_band():
    from nexus.engagement.insights.rules import describe

    company = {"company_domain": "acme.io", "best_weekday": 2, "best_hour": 14,
               "median_response_s": 86400 * 2, "reply_propensity": 0.1}
    assert describe({**company, "workspace_count": 2}, viewer_consented=True,
                    subject="company").level == "none"
    shown = describe({**company, "workspace_count": 3}, viewer_consented=True, subject="company")
    assert shown.text == "People there usually reply on Wednesdays around 2pm, typically within 2 days"


def test_nothing_that_could_name_another_workspace_is_read_or_returned():
    from nexus.engagement.insights import client
    from nexus.engagement.insights.rules import Insight

    assert not any("workspace" in key for key in Insight().as_dict())
    # The store's key lists never leave it: the client does not select them.
    assert "workspace_keys" not in client._PERSON_COLUMNS
    assert "workspace_keys" not in client._COMPANY_COLUMNS


# ---- likelihood and best time --------------------------------------------------------------------

def test_likelihood_comes_from_your_own_fit_and_signals():
    from nexus.engagement.insights.likelihood import likelihood

    strong = likelihood(fit=85, recent_signals=2, propensity=None)
    weak = likelihood(fit=20, recent_signals=0, propensity=None)
    assert (strong.band, weak.band) == ("high", "low")
    assert "Strong fit for your ICP (85)" in strong.reasons
    assert "2 signals in the last 30 days" in strong.reasons
    # Unscored is neutral, not zero: a new account is not a bad one.
    assert likelihood(fit=None, recent_signals=3, propensity=None).score == 0.7
    # And with nothing at all there is no answer, rather than "less likely to reply".
    blank = likelihood(fit=None, recent_signals=0, propensity=None)
    assert (blank.band, blank.score) == ("unknown", 0.0)


def test_responsiveness_moves_the_answer_only_when_a_pattern_is_allowed():
    from nexus.engagement.insights.likelihood import likelihood

    base = likelihood(fit=50, recent_signals=1, propensity=None)
    keen = likelihood(fit=50, recent_signals=1, propensity=0.3)
    quiet = likelihood(fit=50, recent_signals=1, propensity=0.01)
    assert quiet.score < base.score < keen.score
    assert "Replies to more outreach than most" in keen.reasons
    assert "Rarely replies to outreach" in quiet.reasons


def test_best_time_is_the_persons_then_the_companys_then_the_morning():
    from nexus.engagement.insights.best_time import for_campaign, for_person
    from nexus.engagement.insights.rules import Insight

    person = Insight(level="pattern", best_weekday=1, best_hour=10)
    company = Insight(level="pattern", best_hour=15)
    band_only = Insight(level="band", band="same_day")
    assert for_person(person, company).as_dict()["clock"] == "10:00"
    assert for_person(band_only, company).source == "company"
    assert for_person(band_only, None).source == "default"
    assert for_person(band_only, None).clock == "09:30"
    # One prolific replier does not set everyone's send time.
    assert for_campaign([person]).source == "default"
    assert for_campaign([person, Insight(level="pattern", best_hour=10)]).clock == "10:00"


# ---- the endpoints -------------------------------------------------------------------------------

@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


async def test_the_screens_get_likelihood_even_without_an_insights_store(client, engine_on):
    from nexus.core.db import utcnow
    from nexus.engagement.insights.client import clear_cache
    from nexus.models.account import Account, Contact
    from nexus.models.intelligence import AccountScore
    from nexus.models.signal import SignalEvent

    clear_cache()
    token = await signup(client, slug="insnostore", email="sam@insnostore.com", company="I")
    me = principal_from_token(token)
    async with tenant_session(me.tenant_id) as ts:
        good, poor = Account(name="Acme", domain="acme.io"), Account(name="Globex", domain="globex.com")
        ts.add(good)
        ts.add(poor)
        await ts.flush()
        ts.add(AccountScore(account_id=good.id, composite=85, computed_at=utcnow()))
        ts.add(AccountScore(account_id=good.id, composite=10, computed_at=utcnow() - timedelta(days=9)))
        ts.add(AccountScore(account_id=poor.id, composite=20, computed_at=utcnow()))
        for i in range(2):
            ts.add(SignalEvent(account_id=good.id, kind="funding", source="web", title=f"Round {i}",
                               strength=0.9, occurred_at=utcnow() - timedelta(days=3),
                               dedupe_key=f"ins-{i}"))
        jane = Contact(account_id=good.id, full_name="Jane", email="jane@acme.io")
        ken = Contact(account_id=poor.id, full_name="Ken", email="ken@globex.com")
        ts.add(jane)
        ts.add(ken)
        await ts.flush()
        ids = [jane.id, ken.id]

    r = await client.get("/api/engagement/insights/contacts", headers=auth(token),
                         params={"ids": ",".join(ids)})
    assert r.status_code == 200, r.text
    rows = {row["contact_id"]: row for row in r.json()}
    # The LATEST score counts, not an older one.
    assert rows[ids[0]]["likelihood"]["band"] == "high"
    assert rows[ids[1]]["likelihood"]["band"] == "low"
    assert rows[ids[0]]["person"]["level"] == "none"
    assert rows[ids[0]]["best_time"]["source"] == "default"

    too_many = ",".join(f"c{i}" for i in range(101))
    assert (await client.get("/api/engagement/insights/contacts", headers=auth(token),
                             params={"ids": too_many})).status_code == 422


async def test_insights_are_dark_with_the_engine(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    token = await signup(client, slug="insdark", email="sam@insdark.com", company="D")
    r = await client.get("/api/engagement/insights/contacts", headers=auth(token),
                         params={"ids": "x"})
    assert r.status_code == 404
