"""The InfoJoy Search API client. Total: it never raises; every failure is a status with a reason.

* **Keys** come from Provider keys (``infojoy_search``) through the resolver, which falls back to
  ``NEXUS_INFOJOY_SEARCH_API_KEYS``, re-read on its 30 s TTL so a key added in the panel reaches a
  running process without a restart. A key that answers 401/403 is recorded against its row
  (``record_rejection_from_response``, so it is red by morning) and the next key is tried; a 429
  tries the next key, then backs off once.
* **The token is a header** (``X-Api-Token``), never a query parameter: a URL is copied into every
  log line and exception that carries it.
* **The host is configuration**, never a request parameter, so there is nothing here to point at an
  internal address.
* **"Not configured" is not "nothing matched".** No key is ``not_configured`` and nothing is sent; an
  unreadable 200 is ``bad_response``, never an empty page.
* **Bounded**: 5 s to connect, 20 s to read, at most 500 rows a page (the provider's own limit,
  refused here before sending).

State is per call, never on the instance, so one client may serve concurrent requests.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Literal

import httpx

from nexus.prospector.records import ParsedPage, parse_page

logger = logging.getLogger("nexus.prospector.client")

PROVIDER = "infojoy_search"
MAX_PAGE_SIZE = 500
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
ENDPOINTS = {"contacts": "/api/search/contacts", "companies": "/api/search/companies"}

Status = Literal["ok", "not_configured", "invalid", "rejected", "busy", "unavailable",
                 "bad_response"]


@dataclass(slots=True)
class SearchOutcome:
    status: Status
    page: ParsedPage | None = None
    detail: str = ""
    #: Rows the provider returned (and billed), for the COGS record.
    vendor_rows: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def _clean(text: str, keys: list[str]) -> str:
    text = _CONTROL.sub(" ", str(text or "")).strip()
    for key in keys:
        if key:
            text = text.replace(key, "•••")
    return text[:300]


def _provider_words(resp: httpx.Response) -> str:
    """The provider's own sentence for a refused request."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text or ""
    if isinstance(body, dict):
        errors = body.get("Errors") or body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors[:5])
        for name in ("detail", "message", "error", "title"):
            if body.get(name):
                return str(body[name])
    return str(body)


class InfoJoyClient:
    def __init__(self, *, keys: list[str] | None = None, base_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout: httpx.Timeout = DEFAULT_TIMEOUT, backoff_s: float = 1.0) -> None:
        self._keys = keys
        self._base_url = base_url
        self._transport = transport
        self._timeout = timeout
        self._backoff_s = backoff_s

    async def _resolve_keys(self) -> list[str]:
        if self._keys is not None:
            return [k for k in self._keys if k]
        from nexus.providers import resolver

        try:
            return [k for k in await resolver.key_pool(PROVIDER) if k]
        except Exception:  # a settings read must never become an exception here
            logger.warning("could not resolve %s keys", PROVIDER, exc_info=True)
            return []

    def _url(self, view: str) -> str:
        from nexus.core.config import get_settings

        base = (self._base_url or get_settings().infojoy_search_base_url).rstrip("/")
        return f"{base}{ENDPOINTS[view]}"

    async def search(self, view: str, filters: dict, *, page: int, size: int,
                     count: bool = False) -> SearchOutcome:
        try:
            return await self._search(view, filters, page=page, size=size, count=count)
        except Exception:  # total: nothing escapes
            logger.warning("prospector search failed unexpectedly", exc_info=True)
            return SearchOutcome("unavailable", detail="The data provider could not be reached.")

    async def _search(self, view: str, filters: dict, *, page: int, size: int,
                      count: bool) -> SearchOutcome:
        if view not in ENDPOINTS:
            return SearchOutcome("invalid", detail=f"Unknown view {view!r}.")
        if size < 1 or size > MAX_PAGE_SIZE:
            return SearchOutcome("invalid", detail=f"PageSize cannot be greater than {MAX_PAGE_SIZE}.")
        keys = await self._resolve_keys()
        if not keys:
            return SearchOutcome(
                "not_configured",
                detail="Prospector is not set up: a superadmin adds an InfoJoy Search key in "
                       "Control plane > Provider keys.")
        body = {"PageNumber": max(1, int(page)), "PageSize": int(size),
                "IncludeTotalCount": bool(count), "Filters": dict(filters or {})}
        url = self._url(view)

        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            rejected = busy = 0
            for attempt in range(2):
                for key in keys:
                    try:
                        resp = await client.post(url, headers={"X-Api-Token": key}, json=body)
                    except httpx.TimeoutException:
                        return SearchOutcome("unavailable",
                                             detail="The data provider did not answer in time.")
                    except httpx.HTTPError as exc:
                        logger.warning("prospector transport error: %s", type(exc).__name__)
                        return SearchOutcome("unavailable",
                                             detail="The data provider could not be reached.")
                    outcome = await self._read(resp, key, keys)
                    if outcome is None:          # rotate
                        if resp.status_code == 429:
                            busy += 1
                        else:
                            rejected += 1
                        continue
                    return outcome
                if busy and attempt == 0 and self._backoff_s > 0:
                    await asyncio.sleep(self._backoff_s)
                    rejected = busy = 0
                    continue
                break
        if busy:
            return SearchOutcome("busy", detail="The data provider is busy. Try again shortly.")
        return SearchOutcome(
            "rejected", detail="The data provider rejected our key. A superadmin can check it "
                               "in Control plane > Provider keys.")

    async def _read(self, resp: httpx.Response, key: str, keys: list[str]) -> SearchOutcome | None:
        """An outcome, or None to try the next key."""
        status = resp.status_code
        if status == 200:
            try:
                parsed = parse_page(resp.json())
            except ValueError:
                parsed = None
            if parsed is None:
                return SearchOutcome("bad_response",
                                     detail="The data provider sent an answer we could not read.")
            return SearchOutcome("ok", page=parsed, vendor_rows=len(parsed.items))
        if status in (401, 403):
            from nexus.providers.service import record_rejection_from_response

            try:
                await record_rejection_from_response(PROVIDER, key, resp)
            except Exception:
                logger.warning("could not record a rejected %s key", PROVIDER, exc_info=True)
            return None
        if status == 429:
            return None
        if status in (400, 404, 409, 422):
            words = _clean(_provider_words(resp), keys) or "The data provider refused the search."
            return SearchOutcome("invalid", detail=words)
        return SearchOutcome("unavailable",
                             detail=f"The data provider failed (HTTP {status}). Try again shortly.")
