"""Session revocation: end the tokens a user already holds.

Access tokens are stateless JWTs, so nothing about issuing one records that it exists and nothing
about a later event reaches it. Before this, suspending a user, resetting their password,
demoting them or removing them from a workspace all took effect only when their current token
expired — up to ``access_token_ttl_min`` (60 minutes by default) of continued access after the
moment access was supposed to stop.

The mechanism is one integer per user, stamped into the token as ``tv``. Bumping it invalidates
every token issued before the bump at once. Chosen over a denylist because a denylist needs a
store, an expiry sweep and a lookup that must be right on every request; this needs a column.

**A token with no ``tv`` claim is accepted.** Those were issued by the previous release, and
refusing them would log out every active user the moment this deploys. They age out on their own
within one token TTL.
"""
from __future__ import annotations

import logging

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from nexus.models.identity import User

logger = logging.getLogger("nexus.auth.sessions")


async def current_token_version(db: AsyncSession, user_id: str) -> int:
    """The version a freshly issued token for this user should carry."""
    user = await db.get(User, user_id)
    return int(getattr(user, "token_version", 0) or 0) if user is not None else 0


async def revoke_user_sessions(db: AsyncSession, user_id: str) -> int:
    """Invalidate every access token already issued to this user. Returns the new version.

    Written as an UPDATE ... + 1 rather than read-modify-write so two concurrent revocations
    cannot land on the same number — which would leave one of them not actually revoking.

    Does NOT commit: the caller owns the transaction, so revocation lands atomically with the
    change that motivated it. A suspension that committed while its revocation rolled back would
    be a suspended user with a working token.
    """
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(token_version=User.token_version + 1)
    )
    await db.flush()
    version = await current_token_version(db, user_id)
    logger.info("revoked sessions for user %s (token_version -> %s)", user_id, version)
    return version


async def revoke_by_email(db: AsyncSession, email: str) -> int:
    """Same, for the paths that hold an address rather than an id."""
    from sqlalchemy import select

    user = (
        await db.scalars(select(User).where(User.email == (email or "").strip().lower()))
    ).first()
    if user is None:
        return 0
    return await revoke_user_sessions(db, user.id)
