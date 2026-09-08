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
# The activity feed, which the profile actor does not carry. Separate because no actor does both.
POSTS_ACTOR = "linkedin_posts"

# Keys that hold the person's own one-line positioning.
_HEADLINE_KEYS = ("headline", "occupation", "subTitle", "sub_title", "title", "jobTitle")
_SUMMARY_KEYS = ("summary", "about", "aboutSection", "bio", "description")
# Anything that names a stream of activity, matched by SEGMENT not substring — `updates` contains
# "date", which a substring exclusion silently dropped (see nexus/core/keys.py).
#: `content` is here because it is where the activity actor puts the post BODY, and without it the
#: sweep matched only `article` (a shared link's title) and `shareUrn` (an identifier) — so the
#: extractor returned everything about a post except the post. Measured on live output.
_POST_WANTED = frozenset({"post", "activity", "update", "article", "share", "feed", "content"})
#: `urn` joins the exclusions for the same run: `shareUrn` matched on "share" and its value is a
#: LinkedIn internal id. Matched by SEGMENT, so `contentAttributes` (["content","attributes"]) is
#: excluded by "attribute" while `content` itself is kept — the distinction a substring check
#: cannot make, which is why `nexus/core/keys.py` works on segments.
_POST_UNWANTED = frozenset({
    "count", "total", "url", "link", "id", "date", "time", "status", "type", "num", "number",
    "urn", "attribute", "image", "video",
})
# Where the text sits inside a post object, whatever the actor calls it.
# Where the text sits inside a post object, whatever the actor calls it.
#
# **`title` and `description` are deliberately absent, and that is the whole fix for shared links.**
# A post has no title; a shared ARTICLE does. When somebody quote-posts a link, the activity actor
# returns their commentary under `content` and the link's metadata under `article`, and reading
# `title` turned "GPT-6 Astra: Frontier intelligence for work | Microsoft Azure Blog" into a line
# the prompt presented as something the person said. An SDR opening on a headline the prospect did
# not write is the automation tell this module exists to avoid.
#
# Fixed HERE rather than by dropping `article` from the key sweep, because an actor may legitimately
# nest a real post under `articles: [{"content": ...}]` — pinned by
# `test_posts_are_found_under_any_key_spelling`. Narrowing the sweep is the mistake that made
# `phone_finder` miss real data; narrowing which key inside an object counts as PROSE is not.
_TEXT_KEYS = ("text", "content", "postText", "post_text", "commentary", "body", "message",
              "summary")
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

    **It must also reject IDENTIFIERS, not just short text.** Measured 2026-09-08 against the live
    activity actor: two of three extracted "posts" were
    `urn:li:ugcPost:7502385616106512385` — long enough, mostly letters, and completely meaningless
    to a rep. The key sweep had matched `shareUrn` on its "share" segment and the value gate waved
    the URN through.

    That is `phone_finder` again, in the other direction. The lesson recorded in CLAUDE.md is that
    both halves are required: "the sweep alone is reckless, the gate alone missed a real number."
    Here the sweep was reckless and the gate was too weak to catch it, so both are tightened.
    """
    if len(text) < _MIN_POST_CHARS:
        return False
    # An identifier, not prose. `urn:li:...`, a bare uuid, an object id.
    if text.lower().startswith(("urn:", "urn%3a")):
        return False
    # Prose has spaces. A single unbroken token of any length is an id, a URL or a hash — never
    # something a person wrote and never something worth reading back to them.
    if " " not in text.strip():
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


#: Where a row states whose profile it is. A profile scraper puts it at the top level; an ACTIVITY
#: scraper puts it under the post's `author`, because the row is a post rather than a person.
_IDENTITY_KEYS = ("linkedin_url", "linkedinUrl", "profileUrl", "profile_url", "url", "inputUrl",
                  "publicIdentifier", "public_identifier")
#: Nested objects that carry the identity one level down. Named rather than swept, because
#: descending into every object would let an identity anywhere in the row — a mentioned person, a
#: tagged company — satisfy a check whose entire job is to be strict.
_IDENTITY_CONTAINERS = ("author", "actor", "profile", "poster", "creator")


def _states_a_profile(item: dict) -> bool:
    """Whether this row names ANY profile, at either level. Distinguishes "not ours" from "silent"."""
    from nexus.people.store import normalise_linkedin

    for scope in (item, *(item.get(c) for c in _IDENTITY_CONTAINERS)):
        if not isinstance(scope, dict):
            continue
        for key in _IDENTITY_KEYS:
            value = scope.get(key)
            if isinstance(value, str) and (normalise_linkedin(value) or value.strip()):
                return True
    return False


def _row_is_about(item: dict, expect: str) -> bool:
    """Whether this dataset row is the profile we asked for.

    **Checks the nested author too.** The activity actor returns POSTS, not people, so the profile
    URL lives under `author.linkedinUrl` and never at the top level. Reading only the top level
    meant every post row "named no profile" and was therefore used — the benign single-result case
    swallowing the exact check that stops a stranger's words being read back to a prospect on a
    call. Found by test, and only after a second actor with a different row shape existed.
    """
    from nexus.people.store import normalise_linkedin

    for scope in (item, *(item.get(c) for c in _IDENTITY_CONTAINERS)):
        if not isinstance(scope, dict):
            continue
        for key in _IDENTITY_KEYS:
            value = scope.get(key)
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
        elif any(_states_a_profile(r) for r in rows):
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
        insights.recent_posts.extend(await self._recent_posts(client, url))
        return None if insights.is_empty() else insights

    async def _recent_posts(self, client, url: str) -> list[str]:
        """This person's own recent posts, from the activity actor. Never raises.

        **A second actor run, and therefore its own switch.** The profile actor's `updates` array
        came back empty on every profile measured — it scrapes a profile page, not a feed — and no
        actor in the store does both in one call (checked 2026-09-08:
        `harvestapi/linkedin-profile-scraper`'s two modes differ only on email search and its live
        output has no posts key). So posts cost a second run per contact, which is a spending
        decision rather than a default.

        **A failure here must not lose the profile half.** Headline and About are the cheap part
        and they work alone; taking them down because an activity scrape timed out would trade the
        reliable half for the optional one.
        """
        from nexus.core.config import get_settings

        settings = get_settings()
        if not getattr(settings, "personalization_posts_enabled", False):
            return []
        try:
            items = await client.run_actor(
                POSTS_ACTOR,
                {
                    "targetUrls": [url],
                    # Bounded by the same setting that bounds how many reach the prompt: fetching
                    # fifty to quote three is paying for forty-seven nobody reads.
                    "maxPosts": max(1, int(getattr(settings, "personalization_max_posts", 3))),
                    "postedLimit": getattr(settings, "personalization_posts_window", "3months"),
                    # A repost is not something this person wrote, and "I saw your post" about
                    # somebody else's is the automation tell `_is_substantive` exists to avoid.
                    # A quote post carries their own commentary, so it stays.
                    "includeReposts": False,
                    "includeQuotePosts": True,
                    # Reactions and comments are other people's words and are billed per item.
                    "scrapeReactions": False,
                    "scrapeComments": False,
                },
            )
        except Exception:
            logger.warning("recent-post fetch failed for %s", url, exc_info=True)
            return []
        # Reuses the SAME parser as the profile dataset, so the identity check, the substance floor
        # and the trimming cannot drift between the two actors.
        return parse_profile(items, expect_linkedin_url=url).recent_posts
