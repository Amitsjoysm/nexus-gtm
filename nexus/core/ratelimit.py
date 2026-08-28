"""Lightweight per-IP rate limiting for the auth endpoints (brute-force / abuse defense).

A FastAPI dependency: ``Depends(rate_limit("login"))``. It keeps a per-(bucket, client-IP) sliding
window in process memory and raises 429 once the window is full. **ON by default** since M13:
"secure once someone remembers to enable it" is not a security posture, and an unthrottled login
endpoint is a credential-stuffing target. Tests that legitimately fire many rapid auth calls opt
out via the ``no_auth_rate_limit`` fixture. No external dependency.

**The counter is shared when Valkey is reachable, and per-process when it is not.** It was only
ever per-process, and the docstring pointed at a Caddy ``rate_limit`` block as the authoritative
layer — a block ``deploy/Caddyfile`` does not contain and ``caddy:2-alpine`` cannot run without a
custom xcaddy build. So "10 attempts per minute" was really 10 per uvicorn worker: two replicas of
two workers made it 40, and every deploy reset it.

The fallback direction is deliberate. A limiter that failed closed when its store was unreachable
would turn a Valkey blip into a total login outage — a worse failure than a temporarily weakened
limit, and one that arrives during exactly the incidents when people most need to sign in.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable

from fastapi import HTTPException, Request, status

# bucket:ip -> deque[monotonic timestamps within the window]
_HITS: dict[str, deque[float]] = {}
_MAX_KEYS = 50_000  # hard cap so a flood of unique IPs can't grow this unbounded

logger = logging.getLogger("nexus.core.ratelimit")


def _client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers, so request.client.host is already the real client IP
    # behind Caddy. Fall back to a constant if the transport has no peer (e.g. test ASGI).
    return request.client.host if request.client else "unknown"


# The shared counter, when one is configured. Set at startup from the Valkey the job queue
# already uses; None means "in-process only", which is what tests and single-process runs get.
_shared = None


def set_shared_backend(client) -> None:
    """Install (or clear) the shared counter store. Called from the API lifespan."""
    global _shared
    _shared = client


def _too_many(retry_after: int) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many attempts. Please wait and try again.",
        headers={"Retry-After": str(max(1, retry_after))},
    )


async def _shared_count(key: str, window: int) -> int | None:
    """Hits in the current window from the shared store, or None if it is unusable.

    A fixed window rather than the sliding one the in-process path uses: it costs one INCR
    instead of a read-modify-write on a list, and the difference — a burst straddling a window
    boundary — is immaterial for a brute-force guard and not worth a Lua script.
    """
    if _shared is None:
        return None
    try:
        slot = int(time.time()) // max(1, window)
        redis_key = f"nexus:rl:{key}:{slot}"
        hits = int(await _shared.incr(redis_key))
        if hits == 1:
            # Only on the first hit of a window; re-arming the TTL on every request would let a
            # steady stream of attempts keep the key alive indefinitely.
            await _shared.expire(redis_key, window * 2)
        return hits
    except Exception:
        # Unreachable store: fall through to the in-process counter. Never fail closed.
        logger.warning("shared rate-limit store unavailable; using in-process counters",
                       exc_info=True)
        return None


def _local_exceeded(key: str, window: int, limit: int) -> int | None:
    """Sliding-window check against the in-process deque. Returns retry-after when over."""
    now = time.monotonic()
    dq = _HITS.get(key)
    if dq is None:
        if len(_HITS) >= _MAX_KEYS:  # crude overflow guard
            _HITS.clear()
        dq = _HITS[key] = deque()
    while dq and now - dq[0] > window:
        dq.popleft()
    if len(dq) >= limit:
        return int(window - (now - dq[0])) + 1
    dq.append(now)
    return None


def rate_limit(bucket: str) -> Callable[[Request], None]:
    """Build a dependency that limits ``bucket`` to N hits per window per client IP."""

    async def _dep(request: Request) -> None:
        from nexus.core.config import get_settings

        s = get_settings()
        if not s.auth_rate_limit_enabled:
            return
        window = s.auth_rate_limit_window_s
        limit = s.auth_rate_limit_max
        key = f"{bucket}:{_client_ip(request)}"

        hits = await _shared_count(key, window)
        if hits is not None:
            if hits > limit:
                raise _too_many(window)
            return

        retry = _local_exceeded(key, window, limit)
        if retry is not None:
            raise _too_many(retry)

    return _dep


def reset_rate_limits() -> None:
    """Clear all counters (test helper / manual reset)."""
    _HITS.clear()
