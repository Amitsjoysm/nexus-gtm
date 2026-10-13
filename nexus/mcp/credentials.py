# nexus/mcp/credentials.py
"""Secrets for connected AI apps, and turning a bearer into the person it acts for.

Three kinds of secret, told apart by prefix so a pasted one is recognisable on sight (and by a
secret scanner): ``nxk_`` API keys, ``nxo_`` OAuth access tokens, ``nxr_`` OAuth refresh tokens.
Each is 32 random bytes; only its SHA-256 is stored.

:func:`resolve` is on every ``/mcp`` call. It re-reads the connection, the user and the membership
(through a 30 s cache, the same bound the session revocation check uses), so:

* a revoked or expired connection stops,
* a suspended user's apps stop,
* a demoted user's apps act with the NEW role, because the role is read from the membership row
  rather than remembered from when the key was made, and
* someone removed from the workspace loses access there even if the key is still listed.

The tables are platform-global and ``memberships`` is excluded from RLS (``scripts/apply_rls.py``),
so this runs on the ordinary session without a tenant binding, the way login does.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from nexus.core.db import get_sessionmaker, utcnow
from nexus.models.mcp import MCP_SCOPES, McpConnection

SCOPES = MCP_SCOPES
#: The OAuth scope string for each connection scope.
OAUTH_SCOPE = {"read": "nexus.read", "research": "nexus.research", "write": "nexus.write"}
SCOPE_FROM_OAUTH = {v: k for k, v in OAUTH_SCOPE.items()}

KEY_PREFIX = "nxk_"
ACCESS_PREFIX = "nxo_"
REFRESH_PREFIX = "nxr_"
_MAX_BEARER = 128

_CACHE_TTL_S = 30.0
_CACHE_MAX = 50_000
#: digest -> (actor, monotonic deadline)
_cache: dict[str, tuple["Actor", float]] = {}
#: connection id -> monotonic time last_used_at was written by this process
_touched: dict[str, float] = {}
_TOUCH_EVERY_S = 60.0


class CredentialError(Exception):
    """The bearer cannot act. ``reason`` is one of ``invalid``, ``expired``, ``revoked``,
    ``suspended``, ``no_membership``; the endpoint answers 401 for all of them."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: str
    tenant_id: str
    role: str
    token_version: int
    connection_id: str
    kind: str
    scopes: frozenset[str]
    name: str


def new_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def digest(secret: str) -> str:
    return hashlib.sha256((secret or "").encode("utf-8")).hexdigest()


def clean_scopes(scopes) -> list[str]:
    """Known scopes, in canonical order, without duplicates."""
    wanted = {str(s) for s in (scopes or [])}
    return [s for s in SCOPES if s in wanted]


async def create_api_key(session, *, user_id: str, tenant_id: str, name: str, scopes,
                         expires_in_days: int | None) -> tuple[McpConnection, str]:
    """A new key in ``session`` (the caller commits). Returns the row and the secret, which is
    never available again."""
    secret = new_secret(KEY_PREFIX)
    conn = McpConnection(
        subject_tenant_id=tenant_id, user_id=user_id, kind="api_key",
        name=(name or "").strip()[:120] or "API key", scopes=clean_scopes(scopes),
        key_prefix=secret[:12], key_digest=digest(secret),
        expires_at=(utcnow() + timedelta(days=expires_in_days)) if expires_in_days else None,
    )
    session.add(conn)
    await session.flush()
    return conn, secret


def forget(connection_id: str | None = None) -> None:
    """Drop cached resolutions: all of them, or those of one connection (revocation, a permission
    change). Other processes converge within the cache TTL."""
    if connection_id is None:
        _cache.clear()
        return
    for key in [k for k, (actor, _) in _cache.items() if actor.connection_id == connection_id]:
        _cache.pop(key, None)


def _well_formed(bearer: str) -> bool:
    if not bearer or len(bearer) > _MAX_BEARER:
        return False
    if not bearer.startswith((KEY_PREFIX, ACCESS_PREFIX)):
        return False
    body = bearer[4:]
    return len(body) >= 32 and all(c.isalnum() or c in "-_" for c in body)


async def _load(bearer: str, key: str) -> Actor:
    from nexus.models.identity import Membership, User
    from nexus.models.mcp import OAuthToken

    now = utcnow()
    async with get_sessionmaker()() as s:
        if bearer.startswith(KEY_PREFIX):
            conn = (await s.scalars(select(McpConnection).where(
                McpConnection.key_digest == key, McpConnection.kind == "api_key"))).first()
            if conn is None:
                raise CredentialError("invalid")
        else:
            tok = await s.get(OAuthToken, key)
            if tok is None or tok.kind != "access":
                raise CredentialError("invalid")
            if tok.revoked_at is not None:
                raise CredentialError("revoked")
            if tok.expires_at <= now:
                raise CredentialError("expired")
            conn = await s.get(McpConnection, tok.connection_id)
            if conn is None:
                raise CredentialError("invalid")
        if conn.revoked_at is not None:
            raise CredentialError("revoked")
        if conn.expires_at is not None and conn.expires_at <= now:
            raise CredentialError("expired")
        user = await s.get(User, conn.user_id)
        if user is None:
            raise CredentialError("invalid")
        if user.suspended_at is not None:
            raise CredentialError("suspended")
        membership = (await s.scalars(select(Membership).where(
            Membership.user_id == conn.user_id,
            Membership.tenant_id == conn.subject_tenant_id))).first()
        if membership is None:
            raise CredentialError("no_membership")
        return Actor(
            user_id=conn.user_id, tenant_id=conn.subject_tenant_id, role=membership.role or "rep",
            token_version=int(user.token_version or 0), connection_id=conn.id, kind=conn.kind,
            scopes=frozenset(clean_scopes(conn.scopes)), name=conn.name,
        )


async def resolve(bearer: str) -> Actor:
    """The actor for this bearer, or :class:`CredentialError`."""
    bearer = (bearer or "").strip()
    if not _well_formed(bearer):
        raise CredentialError("invalid")
    key = digest(bearer)
    cached = _cache.get(key)
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        return cached[0]
    actor = await _load(bearer, key)
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = (actor, now + _CACHE_TTL_S)
    return actor


async def touch(connection_id: str) -> None:
    """Record use, at most once a minute per process. Never raises: a failed bookkeeping write
    must not fail the call it describes."""
    now = time.monotonic()
    last = _touched.get(connection_id)
    if last is not None and now - last < _TOUCH_EVERY_S:
        return
    _touched[connection_id] = now
    if len(_touched) > _CACHE_MAX:
        _touched.clear()
    try:
        async with get_sessionmaker()() as s:
            conn = await s.get(McpConnection, connection_id)
            if conn is not None:
                conn.last_used_at = utcnow()
                await s.commit()
    except Exception:  # pragma: no cover - bookkeeping only
        import logging

        logging.getLogger("nexus.mcp").warning("could not record use of %s", connection_id,
                                               exc_info=True)
