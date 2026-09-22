"""One HTTP path for both mailbox providers: bearer auth, timeouts, and error mapping.

``classify_error`` is pure — status, headers and body text in, a ``ProviderError`` out — so the
rules for "this is a quota, pause until X" are tested offline on the cases they encode, and the real
payloads are exercised by the live suite.

Retry policy is deliberately NOT here. A GET can be retried blindly; a send cannot (spec §5): it
must be reconciled against the Sent folder first. So this module raises, and each caller decides.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from nexus.engagement.mailboxes.provider import (
    AuthExpired,
    NotFound,
    ProviderError,
    ProviderLimit,
    TransientError,
)

TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: When a limit carries no reset time. A quota that resets daily and says so is honoured exactly;
#: one that says nothing gets an hour, which is long enough not to hammer and short enough that a
#: burst limit does not stall a campaign for a day.
DEFAULT_LIMIT_BACKOFF = timedelta(hours=1)
DAILY_LIMIT_BACKOFF = timedelta(hours=24)

_RETRY_AFTER_ISO = re.compile(r"retry after (\d{4}-\d{2}-\d{2}T[0-9:.]+Z)", re.IGNORECASE)
_DAILY = re.compile(r"daily|per day|dailylimit|submission quota|recipient.*limit", re.IGNORECASE)
_LIMIT = re.compile(
    r"rate ?limit|quota|too many|throttl|userRateLimitExceeded|MailboxConcurrency",
    re.IGNORECASE,
)


def _retry_after(headers: dict, body: str, now: datetime) -> datetime | None:
    raw = (headers.get("retry-after") or headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return now + timedelta(seconds=int(raw))
    match = _RETRY_AFTER_ISO.search(body or "")
    if match:
        try:
            return datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def classify_error(status: int, headers: dict, body: str, *, now: datetime | None = None
                   ) -> ProviderError:
    """The ``ProviderError`` a non-2xx response means."""
    now = now or datetime.now(timezone.utc)
    text = (body or "")[:500]
    if status == 401:
        return AuthExpired(text, status=status)
    if status == 404:
        return NotFound(text, status=status)
    if status == 429 or (status == 403 and _LIMIT.search(text)):
        retry_at = _retry_after(headers, text, now)
        if retry_at is None:
            retry_at = now + (DAILY_LIMIT_BACKOFF if _DAILY.search(text) else DEFAULT_LIMIT_BACKOFF)
        return ProviderLimit(text, status=status, retry_at=retry_at)
    if status >= 500:
        return TransientError(text, status=status)
    return ProviderError(text, status=status)


async def request(
    method: str, url: str, *, token: str, params: dict | None = None, json: dict | None = None,
    content: bytes | str | None = None, headers: dict | None = None,
) -> httpx.Response:
    """Issue one authenticated request; raise the mapped ``ProviderError`` on any non-2xx."""
    merged = {"Authorization": f"Bearer {token}", **(headers or {})}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(method, url, params=params, json=json, content=content,
                                        headers=merged)
    except httpx.HTTPError as exc:
        raise TransientError(f"{type(exc).__name__}: could not reach the provider") from exc
    if resp.status_code >= 400:
        raise classify_error(resp.status_code, dict(resp.headers), resp.text)
    return resp
