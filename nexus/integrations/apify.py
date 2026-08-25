# nexus/integrations/apify.py
"""Apify actors as a provider seam.

Apify is a marketplace of scrapers, each addressed by an actor id. We use it for the things that
have no compliant public API — a phone number behind a LinkedIn profile, the headline and recent
posts that make outreach personal — and expect to add more. So the point of this module is that
**adding an actor is a line in `ACTORS`, not a new integration**.

The corollary, learned by leaving two actors registered and uncalled for months: *removing* one is
also a line, so an actor with no consumer gets deleted rather than kept warm. See the note above
``ACTORS``.

Three properties carried over from the search-provider seam, each of which was learned the hard way
elsewhere in this codebase:

* **Inert until keyed.** No key means a clear ``ApifyNotConfigured``, never a fake success and never
  a silent empty list at the point where a caller is entitled to an error. Signal-collection callers
  that legitimately want graceful degradation catch it themselves.
* **Key rotation, sticky.** A crawl issues several actor calls per account and one free-tier key is
  exhausted quickly. ``NEXUS_APIFY_API_KEYS`` is a comma-separated pool; a 429 rotates to the next
  key and stays there until that one also limits, exactly like ``exa_api_keys``.
* **No new dependency.** The published snippet uses ``apify_client``; this uses ``httpx``, which is
  already vendored. The REST call it makes is the same one that library wraps.

``run-sync-get-dataset-items`` is used rather than start-run-then-poll: it blocks until the actor
finishes and returns the dataset in one response, which is what the synchronous ``.call()`` in the
published snippet does. It caps at five minutes server-side, which is why ``timeout`` here is
generous compared with the 8s signal-source budget — an actor run is not an HTTP fetch.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger("nexus.integrations.apify")

_BASE = "https://api.apify.com/v2/acts"
# An actor run is a scraper session, not a request. Apify's own sync endpoint caps at 300s.
_TIMEOUT = 180.0
_RETRY_BACKOFFS = (1.0, 3.0)
_RETRY_STATUS = (500, 502, 503, 504)

# Logical name -> actor id. Adding an actor is one entry here plus a caller; nothing else changes.
#
# **Every entry must have a caller** — pinned by `test_every_registered_actor_has_a_real_caller`.
# A registered actor with no consumer is the worst of the three states: it does nothing, and it
# still prints as a capability in `scripts/verify_apify_actors.py`, which is the one place an
# operator looks to answer "what can this integration do". Two such entries were removed on
# 2026-08-20 (`crunchbase_org` = PPUtGNTB6xB9dJ2di, `company_search` = ayZno82KNVAVaWMpg), and the
# reasons are worth keeping, because "add Crunchbase enrichment" is a recurring idea:
#
# * `crunchbase_org` keys on a **Crunchbase organisation URL**. Shared-company identity is the
#   normalised domain and nothing else (`companies/resolution.py`), so there is no domain ->
#   Crunchbase-URL step to feed it, and `crunchbase.com` is in `_NON_COMPANY_HOSTS` because the
#   discovery path deliberately filters directory aggregators out. Its output would land in
#   `companies`, which carries no tenant_id — a wrong firmographic there is wrong for *every*
#   tenant, in a subsystem that has already shipped six wrong-attribution bugs.
# * `company_search` had 42 total runs across 2 users when it was registered. An abandoned actor is
#   a dependency that disappears without notice, and this one would have fed net-new Account
#   creation.
#
# Neither could be exercised even once: every configured account 403s
# `full-permission-actor-not-approved` and the key that used to work now 401s. Wiring a parser
# against an output shape nobody has observed is how `phone_finder` shipped reading key spellings
# the actor never emitted — a working actor that extracted nothing, silently. Re-adding either is
# one line plus a consumer; doing it on a guess is the part that costs.
ACTORS: dict[str, str] = {
    # LinkedIn profile URL(s) -> contact phone numbers.
    "phone_finder": "5lnEEZaNNBD8VeFAN",
    # LinkedIn profile URL(s) -> headline, summary, recent posts, interests. Feeds person-level
    # personalization for email and call scripts (nexus/personalization/apify_provider.py).
    # NOTE: like phone_finder, this actor requires its permissions to be approved once per Apify
    # account in the console before any run succeeds — an unapproved account gets a 403, not a
    # bad-key error.
    "linkedin_profile": "2SyF0bVxmgGr8IVCZ",
}


class ApifyError(RuntimeError):
    """An actor run failed after retries and key rotation."""


class ApifyNotConfigured(ApifyError):
    """No API key. Raised rather than returning [] so a missing key is never read as "no results".

    That distinction is the whole reason this is a separate class: an empty result and an
    unconfigured integration look identical to a caller, and this codebase has already shipped one
    source that silently found nothing forever.
    """


def _describe_error(resp) -> str:
    """Apify's own error text, so the reason survives into our exception.

    Apify returns ``{"error": {"type": ..., "message": ...}}``. The type is the actionable part:
    `full-permission-actor-not-approved` needs a click in the console, a genuinely bad token needs
    a new token, and the two are indistinguishable from the status code alone.
    """
    try:
        err = (resp.json() or {}).get("error") or {}
        kind, message = err.get("type", ""), err.get("message", "")
        if kind and message:
            return f"{kind}: {message}"
        return kind or message or f"HTTP {resp.status_code}"
    except Exception:
        return (resp.text or f"HTTP {resp.status_code}")[:200]


class ApifyClient:
    """Runs Apify actors with a rotating key pool."""

    def __init__(self, api_keys: list[str] | None = None, *, timeout: float = _TIMEOUT):
        self.api_keys = [k.strip() for k in (api_keys or []) if k and k.strip()]
        self._key_idx = 0
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_keys)

    @property
    def api_key(self) -> str:
        """The key currently in use. Rotates within the pool on rate-limit."""
        return self.api_keys[self._key_idx] if self.api_keys else ""

    async def _refresh_keys(self) -> None:
        """Re-read the managed pool so a key added in the Control plane reaches a RUNNING process.

        The worker is a separate container, so nothing the API does can invalidate its memory.
        Uses the MANAGED pool, not `key_pool`: the latter falls back to the environment,
        so refreshing against it would overwrite keys a caller passed explicitly. The
        lookup is TTL-cached, so this is a dict read on all but one call in thirty
        seconds. Resets the index so the operator's PINNED key is tried first after a change —
        rotation is the failure path, not the resting state.
        """
        try:
            from nexus.providers.resolver import managed_pool

            pool = await managed_pool("apify")
        except Exception:  # key management must never break the call it exists to serve
            return
        if pool and pool != self.api_keys:
            self.api_keys = pool
            self._key_idx = 0

    async def run_actor(
        self, actor: str, run_input: dict, *, timeout: float | None = None,
    ) -> list[dict]:
        """Run an actor to completion and return its dataset items.

        ``actor`` is a logical name from ``ACTORS`` or a raw actor id, so a caller experimenting
        with a new actor does not have to edit this file first.
        """
        # Before the configured check, so adding the FIRST key from the Control plane works on a
        # client that was constructed with an empty pool.
        await self._refresh_keys()
        if not self.api_keys:
            raise ApifyNotConfigured(
                "Apify is not configured. Add a key in the Control plane, or set "
                "NEXUS_APIFY_API_KEY / NEXUS_APIFY_API_KEYS."
            )
        actor_id = ACTORS.get(actor, actor)
        url = f"{_BASE}/{actor_id}/run-sync-get-dataset-items"
        keys = self.api_keys
        backoff_used = 0
        last_auth_error = ""

        for attempt in range(len(keys) + len(_RETRY_BACKOFFS)):
            try:
                async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
                    resp = await client.post(
                        url, json=run_input, params={"token": keys[self._key_idx]},
                    )
                if resp.status_code in (401, 403):
                    # A bad key is not a transient failure. Rotate past it — one revoked key in a
                    # pool must not take the whole integration down — but never retry it.
                    #
                    # Carry the provider's own reason forward. 401 and 403 arrive here together but
                    # mean different things and need different fixes, and the most common 403 is
                    # not a bad key at all: `full-permission-actor-not-approved`, where the actor
                    # demands full account access nobody has approved in the Apify console. An
                    # operator told "key rejected" rotates credentials that were never wrong.
                    last_auth_error = _describe_error(resp)
                    logger.warning("Apify key %d rejected (%s) for actor %s: %s",
                                   self._key_idx, resp.status_code, actor_id, last_auth_error)
                    # Uses `_describe_error`, not the generic extractor, so the row records Apify's
                    # own `error.type` — `full-permission-actor-not-approved` is a console approval
                    # nobody clicked, and a panel that called it a bad key would send the operator
                    # to rotate credentials that were never wrong.
                    from nexus.providers.service import record_runtime_rejection

                    await record_runtime_rejection(
                        "apify", keys[self._key_idx],
                        error=last_auth_error, error_status=resp.status_code,
                    )
                    if len(keys) > 1:
                        self._key_idx = (self._key_idx + 1) % len(keys)
                        continue
                    raise ApifyError(
                        f"Apify refused actor {actor_id} ({resp.status_code}): {last_auth_error}"
                    )
                if resp.status_code == 429:
                    if len(keys) > 1:
                        self._key_idx = (self._key_idx + 1) % len(keys)
                    # Pause only after cycling the whole pool, so a hard cap is not hammered.
                    if (len(keys) == 1 or (attempt + 1) % len(keys) == 0) \
                            and backoff_used < len(_RETRY_BACKOFFS):
                        await asyncio.sleep(_RETRY_BACKOFFS[backoff_used])
                        backoff_used += 1
                    continue
                if resp.status_code in _RETRY_STATUS and backoff_used < len(_RETRY_BACKOFFS):
                    await asyncio.sleep(_RETRY_BACKOFFS[backoff_used])
                    backoff_used += 1
                    continue
                resp.raise_for_status()
                payload = resp.json()
                # The sync endpoint returns the dataset as a bare array.
                return [item for item in payload if isinstance(item, dict)] \
                    if isinstance(payload, list) else []
            except (ApifyError, asyncio.CancelledError):
                raise
            except Exception as exc:
                if backoff_used < len(_RETRY_BACKOFFS):
                    await asyncio.sleep(_RETRY_BACKOFFS[backoff_used])
                    backoff_used += 1
                    continue
                logger.warning("Apify actor %s failed after %d attempts: %r",
                               actor_id, attempt + 1, exc)
                raise ApifyError(f"Apify actor {actor_id} failed: {exc}") from exc

        # Blaming rate limits unconditionally is how a permanent auth problem gets read as a
        # transient one: every key 403ing on `full-permission-actor-not-approved` produced
        # "pool exhausted by rate limits", which sends an operator to wait rather than to fix.
        if last_auth_error:
            raise ApifyError(
                f"Apify actor {actor_id}: all {len(keys)} key(s) refused — {last_auth_error}"
            )
        raise ApifyError(f"Apify actor {actor_id}: {len(keys)}-key pool exhausted by rate limits")


_client: ApifyClient | None = None


def get_apify_client() -> ApifyClient:
    """Process-wide client, so key rotation state is shared across callers.

    A fresh client per call would restart at key 0 every time and hammer an already-limited key.
    """
    global _client
    if _client is None:
        from nexus.core.config import get_settings

        _client = ApifyClient(get_settings().apify_api_key_list)
    return _client


def set_apify_client(client: ApifyClient | None) -> None:
    """Test seam. Passing None restores the configured client on next use."""
    global _client
    _client = client
