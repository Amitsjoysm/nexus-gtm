# nexus/providers/service.py
"""CRUD for provider keys.

Everything runs on the platform sessionmaker: the table carries no ``tenant_id``, so a
tenant-bound session would return zero rows without erroring.

``status`` is written in exactly two places here — :func:`mark_tested` and
:func:`mark_failed_by_digest`. None of the mutation functions accept it, so an admin cannot mark a
dead key working by hand. Same rule as ``nexus/sources/service.py``.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from nexus.core.db import get_platform_sessionmaker, utcnow
from nexus.models.provider_key import ProviderKey
from nexus.providers.catalog import PROVIDERS
from nexus.providers.crypto import key_digest, key_hint, seal_key

logger = logging.getLogger("nexus.providers.service")


class DuplicateKey(ValueError):
    """This exact key is already registered for this provider."""


class UnknownProvider(ValueError):
    """No such provider in the catalog."""


async def list_keys(provider: str = "") -> list[ProviderKey]:
    """Every key, pinned first within each provider — the order the resolver will use."""
    async with get_platform_sessionmaker()() as s:
        stmt = select(ProviderKey).order_by(
            ProviderKey.provider, ProviderKey.preferred.desc(), ProviderKey.created_at
        )
        if provider:
            stmt = stmt.where(ProviderKey.provider == provider)
        return list((await s.scalars(stmt)).all())


async def add_key(provider: str, label: str, secret: str, *, user_id: str = "") -> ProviderKey:
    if provider not in PROVIDERS:
        raise UnknownProvider(f"unknown provider {provider!r}")
    secret = (secret or "").strip()
    if not secret:
        # A blank key would produce a row that looks configured and resolves to nothing — the
        # exact state this feature exists to make impossible.
        raise ValueError("an empty key cannot be stored")
    digest = key_digest(secret)
    async with get_platform_sessionmaker()() as s:
        existing = (
            await s.scalars(
                select(ProviderKey).where(
                    ProviderKey.provider == provider, ProviderKey.key_digest == digest
                )
            )
        ).first()
        if existing is not None:
            raise DuplicateKey(f"that key is already registered for {provider}")
        row = ProviderKey(
            provider=provider, label=(label or "").strip(),
            key_encrypted=seal_key(secret), key_hint=key_hint(secret), key_digest=digest,
            created_by_user_id=user_id or None,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
    _invalidate(provider)
    return row


async def update_label(key_id: str, label: str) -> ProviderKey | None:
    async with get_platform_sessionmaker()() as s:
        row = await s.get(ProviderKey, key_id)
        if row is None:
            return None
        row.label = (label or "").strip()
        await s.commit()
        await s.refresh(row)
        return row


async def delete_key(key_id: str) -> bool:
    async with get_platform_sessionmaker()() as s:
        row = await s.get(ProviderKey, key_id)
        if row is None:
            return False
        provider = row.provider
        await s.delete(row)
        await s.commit()
    _invalidate(provider)
    return True


async def set_enabled(key_id: str, enabled: bool) -> ProviderKey | None:
    """Disabling is never refused — during an incident "stop using this" must not be blocked.

    Disabling also clears the pin, so the resolver never has to reason about a pinned key it is
    not allowed to use.
    """
    async with get_platform_sessionmaker()() as s:
        row = await s.get(ProviderKey, key_id)
        if row is None:
            return None
        row.enabled = bool(enabled)
        if not row.enabled:
            row.preferred = False
        provider = row.provider
        await s.commit()
        await s.refresh(row)
    _invalidate(provider)
    return row


async def prefer_key(key_id: str) -> ProviderKey | None:
    """Pin this key so it is tried first. Implies enabling it, and unpins every sibling."""
    async with get_platform_sessionmaker()() as s:
        row = await s.get(ProviderKey, key_id)
        if row is None:
            return None
        siblings = (
            await s.scalars(select(ProviderKey).where(ProviderKey.provider == row.provider))
        ).all()
        for sib in siblings:
            sib.preferred = sib.id == key_id
        row.enabled = True
        provider = row.provider
        await s.commit()
        await s.refresh(row)
    _invalidate(provider)
    return row


async def mark_tested(key_id: str, *, status: str, depth: str,
                      error: str = "", error_status: int | None = None) -> None:
    """One of the two writers of ``status``."""
    async with get_platform_sessionmaker()() as s:
        row = await s.get(ProviderKey, key_id)
        if row is None:
            return
        row.status = status
        row.last_depth = depth
        row.last_error = (error or "")[:500]
        row.last_error_status = error_status
        row.last_tested_at = utcnow()
        provider = row.provider
        await s.commit()
    _invalidate(provider)


async def mark_failed_by_digest(provider: str, digest: str, *, error: str,
                                error_status: int | None) -> None:
    """Record a RUNTIME rejection against whichever row holds this key.

    Keyed by digest because the rotating caller holds the plaintext, not the row id. This is what
    makes the panel show production reality rather than only what the last manual test said — a key
    that a crawl found revoked at 3am is red by morning without anyone testing it.
    """
    async with get_platform_sessionmaker()() as s:
        row = (
            await s.scalars(
                select(ProviderKey).where(
                    ProviderKey.provider == provider, ProviderKey.key_digest == digest
                )
            )
        ).first()
        if row is None:
            return          # the key came from the env pool, not a managed row
        row.status = "failed"
        row.last_error = (error or "")[:500]
        row.last_error_status = error_status
        row.last_tested_at = utcnow()
        await s.commit()
    _invalidate(provider)


def _invalidate(provider: str) -> None:
    """Drop this process's cached pool. Other processes pick the change up on the TTL."""
    from nexus.providers.resolver import invalidate

    invalidate(provider)
