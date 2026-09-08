# tests/test_personalization_apify.py
"""Turning a LinkedIn profile scrape into something worth saying on a call.

The provider seam existed from the start and always returned the stub — `build_personalization_
provider` had a comment describing the Apify branch that was never written, so
`NEXUS_PERSONALIZATION_PROVIDER=apify` silently did nothing. Everything downstream was already
wired: `brief.to_prompt` folds headline, recent posts and interests into both the email
(`agents/messaging.py`) and the call script (`agents/call_script.py`).

Parsing is written against the lesson the phone actor taught the hard way: a hand-maintained list
of key spellings loses against third-party output. `phone_finder` returned the right number under
`first_mobile_number`, which was not in the list, so a working actor extracted nothing — silently,
reading as "this person has no phone". Here the sweep is by key shape and the *values* are what get
validated.

The stakes are higher than a blank field. These strings are spoken aloud by an SDR. Referencing a
reshare stub, or worse a stranger's post, is more damaging than saying nothing.
"""
from __future__ import annotations

import pytest

from nexus.personalization.apify_provider import parse_profile

URL = "https://www.linkedin.com/in/walterbenvenuto"
OTHER = "https://www.linkedin.com/in/someone-else"

REAL_POST = (
    "We just shipped our new pricing model after six months of customer interviews. "
    "The biggest lesson: nobody wants per-seat billing for an agent product."
)
SECOND_POST = (
    "Hiring two more solutions engineers in Q3. If you like untangling messy data "
    "migrations and talking to customers, come find me."
)


# ---- provider selection -----------------------------------------------------------------------

def test_the_apify_provider_is_actually_reachable_from_config():
    """The regression: this branch did not exist, so `apify` silently ran the stub."""
    from nexus.personalization.provider import build_personalization_provider

    assert build_personalization_provider("apify").name == "apify"
    assert build_personalization_provider("stub").name == "stub"
    assert build_personalization_provider("").name == "stub"


def test_an_unknown_provider_name_degrades_to_the_stub():
    """A typo costs personalization, not the ability to send email at all."""
    from nexus.personalization.provider import build_personalization_provider

    assert build_personalization_provider("apifyy").name == "stub"


def test_the_actor_is_registered():
    from nexus.integrations.apify import ACTORS
    from nexus.personalization.apify_provider import ACTOR

    assert ACTOR in ACTORS


# ---- parsing: shape tolerance -----------------------------------------------------------------

@pytest.mark.parametrize("row", [
    {"linkedin_url": URL, "posts": [REAL_POST]},
    {"linkedin_url": URL, "recentPosts": [REAL_POST]},
    {"linkedin_url": URL, "activities": [REAL_POST]},
    {"linkedin_url": URL, "updates": [{"text": REAL_POST}]},
    {"linkedin_url": URL, "articles": [{"content": REAL_POST}]},
    {"linkedin_url": URL, "recent_activity": [{"commentary": REAL_POST}]},
    {"linkedin_url": URL, "posts": [{"post": {"text": REAL_POST}}]},
])
def test_posts_are_found_under_any_key_spelling(row):
    """An actor swap must not silently produce zero posts — the phone-actor failure mode."""
    assert parse_profile([row], expect_linkedin_url=URL).recent_posts == [REAL_POST]


@pytest.mark.parametrize("key,value", [
    ("postCount", 42),
    ("posts_total", "17"),
    ("activityUrl", "https://linkedin.com/in/x/recent-activity"),
    ("post_id", "urn:li:activity:123"),
    ("lastPostDate", "2026-08-01"),
    ("postStatus", "published"),
])
def test_metadata_keys_do_not_become_posts(key, value):
    """The sweep is only safe because the value still has to read like a written post."""
    assert parse_profile([{"linkedin_url": URL, key: value}],
                         expect_linkedin_url=URL).recent_posts == []


def test_headline_and_summary_are_read():
    row = {
        "linkedin_url": URL,
        "headline": "VP Engineering at Acme | ex-Stripe",
        "about": "  I build   platform teams.  ",
    }
    got = parse_profile([row], expect_linkedin_url=URL)
    assert got.headline == "VP Engineering at Acme | ex-Stripe"
    assert got.summary == "I build platform teams."      # whitespace collapsed


def test_interests_are_read_from_lists_and_objects():
    row = {"linkedin_url": URL, "skills": ["Kubernetes", {"name": "Postgres"}],
           "topics": "developer tools, pricing"}
    got = parse_profile([row], expect_linkedin_url=URL)
    assert "Kubernetes" in got.interests
    assert "Postgres" in got.interests
    assert "developer tools" in got.interests


# ---- parsing: substance -----------------------------------------------------------------------

@pytest.mark.parametrize("junk", [
    "", "   ", "👏", "👏👏👏", "Congrats!", "Thanks!", "...", "#hiring",
    "https://lnkd.in/abcdefg", "+1", "This 👆",
])
def test_reaction_sized_content_is_not_a_post(junk):
    """An SDR opening 'I saw your post' and then referencing a thumbs-up reads as a bot, which is
    worse than referencing nothing."""
    assert parse_profile([{"linkedin_url": URL, "posts": [junk]}],
                         expect_linkedin_url=URL).recent_posts == []


def test_a_very_long_post_is_trimmed_not_dropped():
    """The opening sentences carry the subject; the whole essay would dominate the prompt."""
    essay = "We rebuilt our onboarding. " * 200
    got = parse_profile([{"linkedin_url": URL, "posts": [essay]}], expect_linkedin_url=URL)
    assert len(got.recent_posts) == 1
    assert 0 < len(got.recent_posts[0]) <= 400


def test_reshared_duplicates_collapse():
    got = parse_profile([{"linkedin_url": URL, "posts": [REAL_POST, REAL_POST, SECOND_POST]}],
                        expect_linkedin_url=URL)
    assert got.recent_posts == [REAL_POST, SECOND_POST]


# ---- parsing: identity ------------------------------------------------------------------------

def test_another_persons_row_never_supplies_posts():
    """Reading a stranger's post back to a prospect on a call is the worst failure this module
    can produce — a spoken version of the wrong-attribution bugs in nexus/companies/."""
    items = [
        {"linkedin_url": OTHER, "posts": ["Someone else's entirely different announcement here."]},
        {"linkedin_url": URL, "posts": [REAL_POST]},
    ]
    assert parse_profile(items, expect_linkedin_url=URL).recent_posts == [REAL_POST]


def test_only_foreign_rows_yield_nothing():
    items = [{"linkedin_url": OTHER, "posts": [REAL_POST], "headline": "Someone Else"}]
    got = parse_profile(items, expect_linkedin_url=URL)
    assert got.is_empty()


def test_a_row_naming_no_profile_is_still_used():
    """A single-result actor that does not echo the input back is the common, benign case."""
    assert parse_profile([{"posts": [REAL_POST]}], expect_linkedin_url=URL).recent_posts \
        == [REAL_POST]


def test_the_public_identifier_slug_counts_as_identity():
    """Some scrapers echo the slug rather than the full URL."""
    got = parse_profile([{"publicIdentifier": "walterbenvenuto", "posts": [REAL_POST]}],
                        expect_linkedin_url=URL)
    assert got.recent_posts == [REAL_POST]


# ---- it must never raise ----------------------------------------------------------------------

@pytest.mark.parametrize("items", [
    [], [{}], ["not a dict"], [None], [{"posts": None}], [{"posts": "a string"}],
    [{"posts": [None, 42, {"nope": True}]}],
])
def test_malformed_datasets_are_survivable(items):
    parse_profile(items, expect_linkedin_url=URL)      # must not raise


def test_deeply_nested_output_terminates():
    blob: dict = {"linkedin_url": URL, "posts": {}}
    node = blob["posts"]
    for _ in range(40):
        node["posts"] = {}
        node = node["posts"]
    node["text"] = REAL_POST
    parse_profile([blob], expect_linkedin_url=URL)     # must return, not hang


async def test_no_linkedin_url_means_no_fetch(monkeypatch):
    """A name search would return whoever matched. Without a profile there is no identity to
    verify against, so the provider declines rather than guessing."""
    from nexus.personalization.apify_provider import ApifyPersonalizationProvider

    called = []
    monkeypatch.setattr(
        "nexus.integrations.apify.get_apify_client",
        lambda: called.append(1),
    )
    got = await ApifyPersonalizationProvider().fetch(full_name="Walter B", linkedin_url="")
    assert got is None
    assert called == []


# ---- the point of all of it: it reaches the prompt --------------------------------------------

def test_insights_reach_the_email_and_call_prompt():
    """The whole chain's purpose. If `to_prompt` stops carrying posts, personalization is a
    database column nobody reads."""
    from nexus.personalization.brief import PersonBrief

    insights = parse_profile([{"linkedin_url": URL, "headline": "VP Eng at Acme",
                               "posts": [REAL_POST, SECOND_POST]}], expect_linkedin_url=URL)
    brief = PersonBrief(
        name="Walter", title="VP Engineering", seniority="vp", linkedin_url=URL,
        role_angle="technical scale", signal_title=None, signal_is_personal=False,
        insights=insights.as_dict(),
    )
    prompt = brief.to_prompt(max_posts=3)
    assert "VP Eng at Acme" in prompt
    assert "pricing model" in prompt
    assert "solutions engineers" in prompt


# ---- the About section is the personalization this actor actually returns -------------------------
#
# Measured 2026-09-08 against the live `dev_fusion/Linkedin-Profile-Scraper` on a paid Apify plan,
# two profiles (williamhgates, satyanadella). Both returned a headline and an About section; both
# returned `updates`, `interests` and `skills` as EMPTY LISTS. The actor's declared input schema
# accepts only `profileUrls` — there is no activity flag — so it scrapes a profile, not a feed.
#
# `PersonInsights.summary` was fetched, stored on `contact.custom_fields['personalization']`, and
# never read by `to_prompt`. So person-level personalization was running on one line of text while
# the richest field available sat unused: built, stored and unreachable, the same shape as
# `notification_preferences` having a table and no endpoint.

def test_the_about_section_reaches_the_prompt():
    """THE fix. Without it, the only person-level content this actor reliably supplies is dropped."""
    from nexus.personalization.brief import PersonBrief

    brief = PersonBrief(
        name="Satya Nadella", title="Chairman and CEO", seniority="c_level",
        linkedin_url="https://www.linkedin.com/in/satyanadella/",
        role_angle="executive outcomes", signal_title=None, signal_is_personal=False,
        insights={"headline": "Chairman and CEO at Microsoft",
                  "summary": "I define my mission as empowering every person to achieve more."},
    )
    prompt = brief.to_prompt()
    assert "empowering every person" in prompt
    assert "Chairman and CEO at Microsoft" in prompt


def test_the_summary_does_not_double_its_full_stop():
    """Observed in the live prompt: "…achieve more..". Cosmetic in isolation, but this block is
    read by a model that is being told to write carefully."""
    from nexus.personalization.brief import PersonBrief

    brief = PersonBrief(
        name="A", title=None, seniority=None, linkedin_url=None, role_angle="x",
        signal_title=None, signal_is_personal=False,
        insights={"summary": "I build things."},
    )
    assert ".." not in brief.to_prompt()


def test_a_long_about_section_is_trimmed_not_dropped():
    """The opening sentences carry who the person says they are; the whole thing would dominate a
    prompt whose other half is the account's signals. Same rule as a long post."""
    from nexus.personalization.brief import PersonBrief

    long_about = ("I lead revenue at a logistics company. " * 40).strip()
    brief = PersonBrief(
        name="A", title=None, seniority=None, linkedin_url=None, role_angle="x",
        signal_title=None, signal_is_personal=False, insights={"summary": long_about},
    )
    prompt = brief.to_prompt()
    assert "I lead revenue" in prompt
    assert len(prompt) < 900, "an essay-length About section swamped the prompt"


def test_an_empty_post_list_does_not_produce_an_empty_instruction():
    """This actor returns `updates: []` every time. An instruction reading "Reference, naturally,
    their recent activity: ." tells the model to invent one."""
    from nexus.personalization.brief import PersonBrief

    brief = PersonBrief(
        name="A", title=None, seniority=None, linkedin_url=None, role_angle="x",
        signal_title=None, signal_is_personal=False,
        insights={"headline": "VP Sales", "recent_posts": [], "interests": []},
    )
    prompt = brief.to_prompt()
    assert "recent activity" not in prompt
    assert "Interests:" not in prompt


# ---- the provider must follow the runtime setting ------------------------------------------------

def test_the_provider_is_rebuilt_when_the_setting_changes(monkeypatch):
    """`personalization_provider` is a runtime setting now, so an operator can switch a per-contact
    paid actor run off from the Control plane without a redeploy.

    A build-once cache would have made that toggle inert after the first contact enriched — the
    panel reading "off" while the actor keeps firing. That is exactly the failure
    `nexus/runtime_config` documents: "the toggle reads 'off' and the feature keeps running"."""
    from nexus.core.config import get_settings
    from nexus.personalization import provider as mod

    mod.set_personalization_provider(None)  # type: ignore[arg-type]
    mod._provider = None
    mod._provider_for = None

    monkeypatch.setattr(get_settings(), "personalization_provider", "apify")
    assert mod.get_personalization_provider().name == "apify"

    monkeypatch.setattr(get_settings(), "personalization_provider", "stub")
    assert mod.get_personalization_provider().name == "stub", (
        "the provider was memoized and ignored the runtime change"
    )


def test_an_explicitly_installed_provider_is_never_rebuilt_over(monkeypatch):
    """The test seam has to win outright, or every suite that installs a double would have it
    silently replaced by whatever the environment says."""
    from nexus.core.config import get_settings
    from nexus.personalization import provider as mod
    from nexus.personalization.provider import StubPersonalizationProvider

    sentinel = StubPersonalizationProvider()
    sentinel.name = "sentinel"
    mod.set_personalization_provider(sentinel)
    monkeypatch.setattr(get_settings(), "personalization_provider", "apify")
    assert mod.get_personalization_provider() is sentinel

    mod._provider = None
    mod._provider_for = None


def test_the_provider_setting_is_runtime_switchable_and_declares_its_cost():
    """Every catalog entry states its `effect`, and anything medium or high risk states its
    `warning` — a toggle whose result nobody can state in a sentence is a trap. This one spends
    money per contact, so it is high risk and says so."""
    from nexus.runtime_config.catalog import CATALOG

    spec = CATALOG.get("personalization_provider")
    assert spec is not None, "personalization_provider is not switchable at runtime"
    assert spec.risk == "high" and spec.warning
    assert "per contact" in spec.warning
