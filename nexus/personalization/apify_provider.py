# nexus/personalization/apify_provider.py
"""Apify-backed person insights: headline, summary, recent posts, interests.

This is the provider the seam in ``provider.py`` was built for. Everything downstream already
exists — ``brief.to_prompt`` folds `headline`, `recent_posts` and `interests` into both the email
(``agents/messaging.py``) and the call script (``agents/call_script.py``) — so lighting this up is
one env line: ``NEXUS_PERSONALIZATION_PROVIDER=apify``.

**Written defensively on purpose, because of what the phone actor taught.** The `phone_finder`
actor returned the right number under `first_mobile_number` / `mobile_numbers`, neither of which
was in the hand-maintained key list, so a working actor extracted nothing — silently, reading as
"this person has no phone". Actor output is not a contract and a fixed list of key spellings is a
losing game, so extraction here sweeps by key *shape* and validates the *values*.

Three guards, each carried over from a bug this codebase has already shipped:

* **Identity.** The row must be about the profile we asked for. A profile scraper called with a
  list returns a dataset, and taking row zero is how a rep ends up reading a stranger's posts back
  to a prospect — a more embarrassing version of the six wrong-attribution bugs in
  `nexus/companies/`, because the output is spoken aloud on a call.
* **Substance.** A post has to be text a human wrote. Empty strings, "…", reshare stubs and
  single-emoji reactions are not personalization material; referencing one makes the sender look
  like a bot, which is worse than not referencing anything.
* **Never raise.** ``refresh_person_insights`` already swallows exceptions, but a provider that
  throws on an unexpected shape would take the whole enrichment path down for one odd profile.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from nexus.core.keys import key_matches
from nexus.personalization.provider import PersonalizationProvider, PersonInsights

logger = logging.getLogger("nexus.personalization.apify")

# Registered in nexus/integrations/apify.py ACTORS. A logical name, so swapping the actor is one
# line there rather than an edit here.
ACTOR = "linkedin_profile"

# Keys that hold the person's own one-line positioning.
_HEADLINE_KEYS = ("headline", "occupation", "subTitle", "sub_title", "title", "jobTitle")
_SUMMARY_KEYS = ("summary", "about", "aboutSection", "bio", "description")
# Anything that names a stream of activity, matched by SEGMENT not substring — `updates` contains
# "date", which a substring exclusion silently dropped (see nexus/core/keys.py).
_POST_WANTED = frozenset({"post", "activity", "update", "article", "share", "feed"})
_POST_UNWANTED = frozenset({
    "count", "total", "url", "link", "id", "date", "time", "status", "type", "num", "number",
})
# Where the text sits inside a post object, whatever the actor calls it.
_TEXT_KEYS = ("text", "content", "postText", "post_text", "commentary", "description", "title",
              "body", "message", "summary")
_INTEREST_KEYS = ("interests", "topics", "skills", "endorsements", "categories")

# A post shorter than this is a reaction, a reshare stub or an emoji — nothing to write from.
_MIN_POST_CHARS = 25
# And one longer than this is an essay. Trimmed rather than dropped: the opening sentences carry
# the subject, and the whole thing would dominate the prompt and the token bill.
_MAX_POST_CHARS = 400


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    # Collapse the whitespace scrapers leave behind; a post full of newlines wrecks the prompt.
    return re.sub(r"\s+", " ", value).strip()


def _first_text(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        text = _clean(item.get(key))
        if text:
            return text
    return ""


def _is_substantive(text: str) -> bool:
    """Whether a post is worth putting in front of an LLM as 'their recent activity'.

    Deliberately strict. An SDR opening with "I saw your post" and then referencing a reshare stub
    or a bare congratulations is worse than not referencing anything at all — it reads as
    automation, which is the exact impression personalization exists to avoid.
    """
    if len(text) < _MIN_POST_CHARS:
        return False
    # Needs actual words, not just punctuation, emoji and a link.
    letters = sum(1 for ch in text if ch.isalpha())
    return letters >= _MIN_POST_CHARS // 2


def _collect_posts(value: Any, out: list[str], depth: int = 0) -> None:
    """Pull post text out of whatever shape the actor used: strings, or objects with a text key."""
    if depth > 3 or len(out) >= 25:
        return
    if isinstance(value, str):
        text = _clean(value)
        if _is_substantive(text):
            out.append(text[:_MAX_POST_CHARS])
        return
    if isinstance(value, list):
        for entry in value:
            _collect_posts(entry, out, depth + 1)
        return
    if isinstance(value, dict):
        text = _first_text(value, _TEXT_KEYS)
        if _is_substantive(text):
            out.append(text[:_MAX_POST_CHARS])
            return
        # Some actors nest the post one level down (`post: {...}`, `content: {...}`).
        for nested in value.values():
            if isinstance(nested, (list, dict)):
                _collect_posts(nested, out, depth + 1)


def _collect_interests(item: dict) -> list[str]:
    found: list[str] = []
    for key in _INTEREST_KEYS:
        value = item.get(key)
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, str):
                    text = _clean(entry)
                elif isinstance(entry, dict):
                    text = _first_text(entry, ("name", "title", "text", "skill"))
                else:
                    text = ""
                if text and text not in found:
                    found.append(text)
        elif isinstance(value, str):
            for part in value.split(","):
                text = _clean(part)
                if text and text not in found:
                    found.append(text)
    return found[:10]


def _row_is_about(item: dict, expect: str) -> bool:
    """Whether this dataset row is the profile we asked for."""
    from nexus.people.store import normalise_linkedin

    for key in ("linkedin_url", "linkedinUrl", "profileUrl", "profile_url", "url", "inputUrl",
                "publicIdentifier", "public_identifier"):
        value = item.get(key)
        if isinstance(value, str):
            if normalise_linkedin(value) == expect:
                return True
            # `publicIdentifier` is the slug alone, not a URL.
            if value.strip().lower() and expect.endswith("/" + value.strip().lower()):
                return True
    return False


def parse_profile(items: list[dict], *, expect_linkedin_url: str = "") -> PersonInsights:
    """Turn an actor dataset into insights. Never raises; an unusable dataset yields empty."""
    from nexus.people.store import normalise_linkedin

    rows = [i for i in items if isinstance(i, dict)]
    expect = normalise_linkedin(expect_linkedin_url) if expect_linkedin_url else ""
    if expect and rows:
        matched = [r for r in rows if _row_is_about(r, expect)]
        if matched:
            rows = matched
        elif any(
            isinstance(r.get(k), str) and normalise_linkedin(str(r.get(k)))
            for r in rows
            for k in ("linkedin_url", "linkedinUrl", "profileUrl", "profile_url", "url")
        ):
            # Every row names a profile and none is ours. Refuse rather than personalise a call
            # with someone else's posts.
            logger.info("personalization: dataset identified other profiles only; discarding")
            return PersonInsights(source="apify")

    insights = PersonInsights(source="apify")
    posts: list[str] = []
    for row in rows:
        insights.headline = insights.headline or _first_text(row, _HEADLINE_KEYS)
        insights.summary = insights.summary or _first_text(row, _SUMMARY_KEYS)
        for key, value in row.items():
            if isinstance(key, str) and key_matches(
                key, wanted=_POST_WANTED, unwanted=_POST_UNWANTED
            ):
                _collect_posts(value, posts)
        for interest in _collect_interests(row):
            if interest not in insights.interests:
                insights.interests.append(interest)

    # De-duplicate while preserving order: reshares mean the same text arrives twice.
    seen: set[str] = set()
    for post in posts:
        marker = post[:80].lower()
        if marker not in seen:
            seen.add(marker)
            insights.recent_posts.append(post)
    insights.summary = insights.summary[:600]
    return insights


class ApifyPersonalizationProvider(PersonalizationProvider):
    name = "apify"

    async def fetch(self, *, full_name: str, linkedin_url: str | None,
                    social_urls: list[str] | None = None) -> PersonInsights | None:
        from nexus.integrations.apify import ApifyNotConfigured, get_apify_client

        url = (linkedin_url or "").strip()
        if not url:
            # No profile means no identity to verify against, and a name search would return
            # whoever matched — the wrong-attribution failure this module is written to avoid.
            return None

        client = get_apify_client()
        if not client.configured:
            logger.info("personalization skipped: Apify is not configured")
            return None

        try:
            items = await client.run_actor(ACTOR, {"profileUrls": [url]})
        except ApifyNotConfigured:
            return None
        except Exception:
            # Includes the 403 an actor returns until its permissions are approved in the Apify
            # console. Logged with the provider's own reason (see integrations/apify.py) so the
            # operator is told what to fix rather than that "there were no insights".
            logger.warning("personalization fetch failed for %s", url, exc_info=True)
            return None

        insights = parse_profile(items, expect_linkedin_url=url)
        return None if insights.is_empty() else insights
