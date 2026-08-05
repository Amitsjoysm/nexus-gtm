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
