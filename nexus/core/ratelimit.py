"""Lightweight per-IP rate limiting for the auth endpoints (brute-force / abuse defense).

A FastAPI dependency: ``Depends(rate_limit("login"))``. It keeps a per-(bucket, client-IP) sliding
window in process memory and raises 429 once the window is full. Off by default
(``auth_rate_limit_enabled``) so local/dev and the offline test suite — which fire many rapid
signup/login calls — are unaffected; production turns it on. No external dependency.

This is in-process defense-in-depth (each uvicorn worker has its own counters); the authoritative,
cross-process limit in production belongs at the edge (Caddy ``rate_limit``) and/or a shared Valkey
counter. Both layers are cheap and complementary.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable

from fastapi import HTTPException, Request, status

# bucket:ip -> deque[monotonic timestamps within the window]
_HITS: dict[str, deque[float]] = {}
_MAX_KEYS = 50_000  # hard cap so a flood of unique IPs can't grow this unbounded


def _client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers, so request.client.host is already the real client IP
    # behind Caddy. Fall back to a constant if the transport has no peer (e.g. test ASGI).
    return request.client.host if request.client else "unknown"


def rate_limit(bucket: str) -> Callable[[Request], None]:
    """Build a dependency that limits ``bucket`` to N hits per window per client IP."""

    def _dep(request: Request) -> None:
        from nexus.core.config import get_settings

        s = get_settings()
        if not s.auth_rate_limit_enabled:
            return
        now = time.monotonic()
        window = s.auth_rate_limit_window_s
        key = f"{bucket}:{_client_ip(request)}"
        dq = _HITS.get(key)
        if dq is None:
            if len(_HITS) >= _MAX_KEYS:  # crude overflow guard
                _HITS.clear()
            dq = _HITS[key] = deque()
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= s.auth_rate_limit_max:
            retry = int(window - (now - dq[0])) + 1
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many attempts. Please wait and try again.",
                headers={"Retry-After": str(max(1, retry))},
            )
        dq.append(now)

    return _dep


def reset_rate_limits() -> None:
    """Clear all counters (test helper / manual reset)."""
    _HITS.clear()
