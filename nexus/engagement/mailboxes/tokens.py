"""Sealed token bundles and "give me a working access token for this mailbox".

Tokens are sealed with the same Fernet helper the network connectors use
(``nexus.network.crypto``, key ``network_token_enc_key`` or derived from ``secret_key``) and never
leave the server. A bundle that cannot be unsealed reads as empty, which ``fresh_access_token``
turns into ``needs_reauth`` — reconnecting fixes it, a crash would not.
"""
from __future__ import annotations

import time

from nexus.core.db import utcnow
from nexus.engagement.mailboxes.provider import AuthExpired, ProviderError, TransientError
from nexus.network.crypto import seal_tokens, unseal_tokens

#: Refresh this many seconds before expiry, so a token never expires mid-request.
EXPIRY_SKEW_S = 120
#: A connected mailbox is refreshed at least this often, so a revoked grant is noticed within a day
#: rather than at the moment a campaign tries to send.
LIVENESS_INTERVAL_S = 24 * 3600


def seal(bundle: dict) -> dict:
    return seal_tokens(bundle)


def unseal(stored: dict | None) -> dict:
    return unseal_tokens(stored)


def needs_refresh(bundle: dict, *, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    if not bundle.get("access_token"):
        return True
    return int(bundle.get("expires_at") or 0) - EXPIRY_SKEW_S <= now


def liveness_due(bundle: dict, *, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    return now - int(bundle.get("refreshed_at") or 0) >= LIVENESS_INTERVAL_S


async def fresh_access_token(ts, connection, *, force: bool = False) -> str:
    """A usable access token for ``connection``, refreshing and re-sealing when needed.

    On a revoked or expired grant the connection is marked ``needs_reauth`` (in the caller's
    transaction) and ``AuthExpired`` is raised.
    """
    from nexus.engagement.config import oauth_app
    from nexus.engagement.mailboxes import oauth

    if connection.status in ("revoked",):
        raise AuthExpired("this mailbox was disconnected")
    bundle = unseal(connection.tokens)
    if not force and not needs_refresh(bundle):
        return bundle["access_token"]
    app = await oauth_app(connection.provider)
    try:
        data = await oauth.refresh(app, bundle.get("refresh_token", ""))
    except AuthExpired as exc:
        connection.status = "needs_reauth"
        connection.last_error = f"Reconnect this mailbox: {exc.detail}"[:500]
        await ts.flush()
        raise
    except TransientError:
        raise  # the provider is unreachable; nothing is known about the grant
    except ProviderError as exc:
        # invalid_client and friends: the APP is misconfigured (a rotated client secret), which no
        # amount of reconnecting by the SDR can fix. Reported as `error` so the screen says so.
        connection.status = "error"
        connection.last_error = f"The mailbox app is misconfigured: {exc.detail}"[:500]
        await ts.flush()
        raise
    bundle = oauth.bundle_from_response(data, previous=bundle)
    connection.tokens = seal(bundle)
    if connection.status != "connected":
        connection.status = "connected"
    connection.last_error = None
    connection.updated_at = utcnow()
    await ts.flush()
    return bundle["access_token"]
