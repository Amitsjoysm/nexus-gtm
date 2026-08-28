# tests/test_shared_rate_limit.py
"""The auth rate limit has to be shared across processes to mean anything.

`nexus/core/ratelimit.py` kept counters in a module-level dict and its own docstring said the
authoritative cross-process limit "belongs at the edge (Caddy `rate_limit`)". `deploy/Caddyfile`
has no such block, and `caddy:2-alpine` does not ship the module that would provide one — so the
only limit in production was per-uvicorn-worker. The compose file runs two app replicas with two
workers each, so "10 attempts per minute" was really 40, and it reset on every deploy.

Valkey is already in the stack for the job queue. The counter moves there when it is reachable
and falls back to the in-process dict when it is not — a limiter that fails closed would turn a
cache blip into a total login outage, which is a worse failure than a weakened limit.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException


class _FakeRedis:
    """Enough of the async client for a fixed-window counter."""

    def __init__(self, *, broken: bool = False):
        self.store: dict[str, int] = {}
        self.expiries: dict[str, int] = {}
        self.broken = broken

    async def incr(self, key: str) -> int:
        if self.broken:
            raise ConnectionError("valkey down")
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key: str, ttl: int) -> None:
        if self.broken:
            raise ConnectionError("valkey down")
        self.expiries[key] = ttl


@pytest.fixture
def limiter(monkeypatch):
    from nexus.core import ratelimit
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "auth_rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "auth_rate_limit_max", 3)
    monkeypatch.setattr(get_settings(), "auth_rate_limit_window_s", 60)
    ratelimit.reset_rate_limits()
    yield ratelimit
    ratelimit.reset_rate_limits()
    ratelimit.set_shared_backend(None)


class _Req:
    def __init__(self, ip="1.2.3.4"):
        self.client = type("C", (), {"host": ip})()


async def test_two_processes_share_one_budget(limiter):
    """The point of the change: separate limiter instances must draw on the same counter."""
    shared = _FakeRedis()
    limiter.set_shared_backend(shared)

    dep_a = limiter.rate_limit("login")
    dep_b = limiter.rate_limit("login")     # stands in for the other uvicorn worker

    await dep_a(_Req())
    await dep_b(_Req())
    await dep_a(_Req())
    with pytest.raises(HTTPException) as exc:
        await dep_b(_Req())                  # 4th attempt against a max of 3
    assert exc.value.status_code == 429
    assert exc.value.headers.get("Retry-After")


async def test_separate_ips_and_buckets_do_not_share_a_budget(limiter):
    shared = _FakeRedis()
    limiter.set_shared_backend(shared)
    login = limiter.rate_limit("login")
    reset = limiter.rate_limit("reset_password")

    for _ in range(3):
        await login(_Req("1.1.1.1"))
    await login(_Req("2.2.2.2"))             # different IP, own budget
    await reset(_Req("1.1.1.1"))             # different bucket, own budget


async def test_an_unreachable_store_falls_back_instead_of_locking_everyone_out(limiter):
    """A limiter that fails closed turns a cache blip into a total login outage."""
    limiter.set_shared_backend(_FakeRedis(broken=True))
    dep = limiter.rate_limit("login")

    for _ in range(3):
        await dep(_Req())                    # served by the in-process fallback
    with pytest.raises(HTTPException):
        await dep(_Req())                    # and the fallback still limits


async def test_the_limit_is_still_off_when_disabled(limiter, monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "auth_rate_limit_enabled", False)
    limiter.set_shared_backend(_FakeRedis())
    dep = limiter.rate_limit("login")
    for _ in range(20):
        await dep(_Req())
