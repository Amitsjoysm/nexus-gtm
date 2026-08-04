# nexus/integrations/apify.py
"""Apify actors as a provider seam.

Apify is a marketplace of scrapers, each addressed by an actor id. We use it for the things that
have no compliant public API — a phone number behind a LinkedIn profile, a Crunchbase organisation
page — and expect to add more (outreach personalisation, call research). So the point of this module
is that **adding an actor is a line in `ACTORS`, not a new integration**.

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
ACTORS: dict[str, str] = {
    # LinkedIn profile URL(s) -> contact phone numbers.
    "phone_finder": "5lnEEZaNNBD8VeFAN",
    # Crunchbase organisation URL -> firmographics + funding history.
    "crunchbase_org": "PPUtGNTB6xB9dJ2di",
    # Country / employee size / industry / name -> company list, for net-new ICP discovery.
    "company_search": "ayZno82KNVAVaWMpg",
}


class ApifyError(RuntimeError):
    """An actor run failed after retries and key rotation."""


class ApifyNotConfigured(ApifyError):
    """No API key. Raised rather than returning [] so a missing key is never read as "no results".

    That distinction is the whole reason this is a separate class: an empty result and an
    unconfigured integration look identical to a caller, and this codebase has already shipped one
    source that silently found nothing forever.
    """


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

    async def run_actor(
        self, actor: str, run_input: dict, *, timeout: float | None = None,
    ) -> list[dict]:
        """Run an actor to completion and return its dataset items.

        ``actor`` is a logical name from ``ACTORS`` or a raw actor id, so a caller experimenting
        with a new actor does not have to edit this file first.
        """
        if not self.api_keys:
            raise ApifyNotConfigured(
                "Apify is not configured. Set NEXUS_APIFY_API_KEY or NEXUS_APIFY_API_KEYS."
            )
        actor_id = ACTORS.get(actor, actor)
        url = f"{_BASE}/{actor_id}/run-sync-get-dataset-items"
        keys = self.api_keys
        backoff_used = 0

        for attempt in range(len(keys) + len(_RETRY_BACKOFFS)):
            try:
                async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
                    resp = await client.post(
                        url, json=run_input, params={"token": keys[self._key_idx]},
                    )
                if resp.status_code in (401, 403):
                    # A bad key is not a transient failure. Rotate past it — one revoked key in a
                    # pool must not take the whole integration down — but never retry it.
                    logger.warning("Apify key %d rejected for actor %s", self._key_idx, actor_id)
                    if len(keys) > 1:
                        self._key_idx = (self._key_idx + 1) % len(keys)
                        continue
                    raise ApifyError(f"Apify rejected the API key for actor {actor_id}")
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
