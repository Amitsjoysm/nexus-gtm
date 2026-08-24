# nexus/providers/resolver.py
"""Which keys a provider should use right now.

    key_pool(p) -> [pinned, ...other enabled rows by created_at]   if any rows exist
                -> the environment pool                             if none

**A worker must see a new key without restarting, and the worker is a separate container.** The API
process invalidating its own cache reaches nothing else, so correctness cannot depend on message
passing between processes. A short TTL cannot miss a message; its worst case is bounded lateness,
which is the right trade for a feature whose entire purpose is removing silent staleness.

Deliberately NOT Valkey pub/sub, though Valkey is already here: a dropped message means a worker
runs on a stale pool indefinitely and nothing says so — the exact failure class this replaces.

Two fallbacks, both biased the same way as the entitlement engine (unknown means allow):

* a database error falls back to the env pool rather than returning nothing, so a DB blip cannot
  take a provider offline when the env keys would have served;
* a single undecryptable row is skipped and logged loudly rather than being fatal, so one bad row
  cannot disable the rest of the pool.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("nexus.providers.resolver")

# Cross-process staleness bound. One indexed lookup per provider per 30s is negligible against the
# cost of an LLM call or a paid search, and it means a key added or pinned in the Control plane is
# live on every process within half a minute with nobody restarting anything.
POOL_TTL_S = 30.0


class PoolCache:
    """A per-process view of the key pools.

    One instance per process in normal use. Tests construct two to stand in for the API and the
    worker — a test with a single cache would pass while the worker stayed stale, which is the
    actual bug this class exists to prevent.
    """

    def __init__(self) -> None:
        self._pools: dict[str, tuple[float, list[str]]] = {}

    def invalidate(self, provider: str = "") -> None:
        """Drop cached pools. Immediate for THIS process; others wait out the TTL."""
        if provider:
            self._pools.pop(provider, None)
        else:
            self._pools.clear()

    async def managed_pool(self, provider: str) -> list[str]:
        """Only the operator-registered keys, ``[]`` when there are none. TTL-cached like
        :meth:`key_pool`, so a refresh on a hot path stays a dict lookup."""
        return await self._cached(provider, fall_back_to_env=False)

    async def key_pool(self, provider: str) -> list[str]:
        """The managed keys, or the environment pool when there are none."""
        return await self._cached(provider, fall_back_to_env=True)

    async def _cached(self, provider: str, *, fall_back_to_env: bool) -> list[str]:
        # One cache entry per provider holds the MANAGED keys; the env fallback is applied on the
        # way out. Caching the post-fallback value would mean two entries per provider that can
        # disagree about the same table.
        cached = self._pools.get(provider)
        if cached is None or (time.monotonic() - cached[0]) >= POOL_TTL_S:
            managed = await self._read(provider)
            self._pools[provider] = (time.monotonic(), managed)
        else:
            managed = cached[1]
        if managed:
            return list(managed)
        if not fall_back_to_env:
            return []
        from nexus.providers.catalog import env_pool

        return env_pool(provider)

    async def _read(self, provider: str) -> list[str]:
        """The operator-registered keys for this provider. Never the env fallback — the caller
        decides whether an empty result should fall back."""
        from sqlalchemy import select

        from nexus.core.db import get_platform_sessionmaker
        from nexus.models.provider_key import ProviderKey
        from nexus.providers.crypto import KeyUnsealable, unseal_key

        try:
            async with get_platform_sessionmaker()() as s:
                rows = list(
                    (
                        await s.scalars(
                            select(ProviderKey)
                            .where(
                                ProviderKey.provider == provider,
                                ProviderKey.enabled.is_(True),
                            )
                            .order_by(ProviderKey.preferred.desc(), ProviderKey.created_at)
                        )
                    ).all()
                )
        except Exception:
            logger.warning(
                "could not read provider keys for %s; the caller will fall back to the "
                "environment pool", provider, exc_info=True,
            )
            return []

        out: list[str] = []
        for row in rows:
            try:
                out.append(unseal_key(row.key_encrypted))
            except KeyUnsealable:
                logger.error(
                    "provider key %s (%s, ...%s) could not be decrypted — skipping it. The "
                    "encryption key changed or the row was altered; re-enter that key.",
                    row.id, provider, row.key_hint,
                )
        # An empty result here means either no rows or every row unreadable. Both are "nothing
        # managed", and `_cached` decides whether that falls back to the environment.
        return out


_CACHE = PoolCache()


async def key_pool(provider: str) -> list[str]:
    """The keys to use: the managed pool when it has any, the environment pool otherwise."""
    return await _CACHE.key_pool(provider)


async def managed_pool(provider: str) -> list[str]:
    """Only the keys an operator registered — ``[]`` when the table holds none.

    The distinction from :func:`key_pool` is load-bearing, and it was found by a failing test
    rather than by reasoning. `key_pool` falls back to the environment, so refreshing a provider
    against it would overwrite keys a caller passed **explicitly** —
    ``ExaSearchProvider(api_keys=[...])`` — with whatever the environment happened to hold. That
    broke a rotation test immediately, and it would have broken any deliberate construction in
    production the same way.

    "The database layers over the environment" has to mean the database wins *when it has something
    to say*, not that every refresh reasserts the environment over its caller.
    """
    return await _CACHE.managed_pool(provider)


def invalidate(provider: str = "") -> None:
    _CACHE.invalidate(provider)

