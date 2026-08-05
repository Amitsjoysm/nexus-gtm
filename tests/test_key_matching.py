# tests/test_key_matching.py
"""Matching third-party JSON keys by meaning, not by substring.

This module exists because a substring regex produced two silent misses in one afternoon:

* `_POST_KEY_EXCLUDE` contained "date" to skip `lastPostDate`. It also matched **`updates`** —
  up-DATE-s — so one of the commonest names for a LinkedIn activity feed was dropped.
* Singularising by stripping a trailing "s" turned **`activities`** into `activitie`, matching
  nothing, so that spelling was dropped too.

Both failures are invisible: the actor runs, returns the right data, and the parser finds nothing.
That is the same shape as the `phone_finder` bug (`first_mobile_number` absent from a hand-written
key list) and it is why matching happens here, once, with tests.
"""
from __future__ import annotations

import pytest

from nexus.core.keys import key_matches, key_segments

POSTS = frozenset({"post", "activity", "update", "article", "share", "feed"})
NOT_POSTS = frozenset({"count", "total", "url", "link", "id", "date", "time", "status", "type"})


@pytest.mark.parametrize("key,expected", [
    ("updates", {"updates"}),
    ("lastPostDate", {"last", "post", "date"}),
    ("first_mobile_number", {"first", "mobile", "number"}),
    ("activityUrl", {"activity", "url"}),
    ("recent-activity", {"recent", "activity"}),
    ("phone2", {"phone", "2"}),
    ("", set()),
])
def test_segmentation(key, expected):
    assert key_segments(key) == expected


@pytest.mark.parametrize("key", [
    "posts", "recentPosts", "recent_posts", "activities", "activity", "recent_activity",
    "updates", "postUpdates", "articles", "shares", "feed", "activityFeed",
])
def test_activity_keys_match(key):
    """`updates` and `activities` are the two that regressed. Both are common actor spellings."""
    assert key_matches(key, wanted=POSTS, unwanted=NOT_POSTS), key


@pytest.mark.parametrize("key", [
    "postCount", "posts_total", "activityUrl", "post_id", "lastPostDate", "postStatus",
    "postType", "activityTime", "shareLink", "updated_at", "created_at", "name", "headline",
])
def test_metadata_and_unrelated_keys_do_not_match(key):
    assert not key_matches(key, wanted=POSTS, unwanted=NOT_POSTS), key


def test_unwanted_wins_over_wanted():
    """A key naming both concepts is excluded — `postCount` is a number, not a post."""
    assert not key_matches("postCount", wanted=POSTS, unwanted=NOT_POSTS)


def test_a_word_ending_in_double_s_is_not_singularised():
    """`address` must not become `addres`, or exclusion lists start matching by accident."""
    assert key_segments("addressStatus") == {"address", "status"}
    assert not key_matches("addressStatus", wanted=frozenset({"addres"}), unwanted=frozenset())


PHONES = frozenset({"phone", "mobile", "tel", "telephone", "msisdn", "whatsapp", "cell"})
NOT_PHONES = frozenset({"status", "country", "code", "type", "url", "id", "carrier", "count"})


@pytest.mark.parametrize("key", [
    "phone", "phone_number", "phoneNumber", "mobile", "mobile_number", "telephone",
    "first_mobile_number", "mobile_numbers", "phoneNumbers", "cell_phone", "work_phone",
    "whatsapp_number", "msisdn",
])
def test_phone_keys_match(key):
    """`first_mobile_number` and `mobile_numbers` are what the live actor actually returns."""
    assert key_matches(key, wanted=PHONES, unwanted=NOT_PHONES), key


@pytest.mark.parametrize("key", [
    "phone_status", "phoneType", "mobile_country_code", "phone_carrier", "mobile_url",
    "phone_id", "phoneCount",
])
def test_phone_lookalike_keys_do_not_match(key):
    assert not key_matches(key, wanted=PHONES, unwanted=NOT_PHONES), key
