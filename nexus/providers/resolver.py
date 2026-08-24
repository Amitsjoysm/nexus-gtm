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

    async def key_pool(self, provider: str) -> list[str]:
        cached = self._pools.get(provider)
        if cached is not None and (time.monotonic() - cached[0]) < POOL_TTL_S:
            return list(cached[1])
        pool = await self._read(provider)
        self._pools[provider] = (time.monotonic(), pool)
        return list(pool)

    async def _read(self, provider: str) -> list[str]:
        from sqlalchemy import select

        from nexus.core.db import get_platform_sessionmaker
        from nexus.models.provider_key import ProviderKey
        from nexus.providers.catalog import env_pool
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
                "could not read provider keys for %s; falling back to the environment pool",
                provider, exc_info=True,
            )
            return env_pool(provider)

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
        # An empty result means every managed row was unreadable, which is a worse state than
        # having none — so fall through to the env pool rather than returning nothing.
        return out or env_pool(provider)


_CACHE = PoolCache()


async def key_pool(provider: str) -> list[str]:
    return await _CACHE.key_pool(provider)


def invalidate(provider: str = "") -> None:
    _CACHE.invalidate(provider)
