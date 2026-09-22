"""Connected mailboxes: connect, list, edit, check, disconnect (spec §9 Settings → Mailboxes).

**A mailbox belongs to the SDR who connected it.** Replies go back to whoever sent the email, and
reading a mailbox is reading a person's mail, so an address already connected by a colleague cannot
be taken over by connecting it again; the colleague disconnects first. The same person reconnecting
(after ``needs_reauth``, or to change timezone) updates their row in place.

Statuses: ``connected``; ``needs_reauth`` (the grant was revoked or expired — the SDR reconnects);
``error`` (the APP is misconfigured, which reconnecting cannot fix); ``revoked`` (disconnected, tokens
deleted, row kept so history still names the mailbox).
"""
from __future__ import annotations

from datetime import datetime

from nexus.core.audit import record_audit
from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.engagement.mailboxes import tokens
from nexus.engagement.timekeeping import zone_or_none
from nexus.models.engagement import MailboxConnection

_UNSET = object()


class MailboxOwnedByColleague(ValueError):
    """The address is connected by someone else in this workspace."""


async def upsert_connection(
    ts: TenantSession, *, owner_user_id: str, provider: str, email: str, display_name: str,
    bundle: dict, scopes: list[str], timezone: str,
) -> MailboxConnection:
    address = (email or "").strip().lower()
    existing = await ts.first(MailboxConnection, MailboxConnection.email == address)
    zone = timezone if zone_or_none(timezone) else "UTC"
    if existing is not None and existing.owner_user_id != owner_user_id \
            and existing.status != "revoked":
        raise MailboxOwnedByColleague(address)
    if existing is None:
        existing = MailboxConnection(owner_user_id=owner_user_id, provider=provider, email=address,
                                     timezone=zone)
        ts.add(existing)
        action = "mailbox.connect"
    else:
        existing.owner_user_id = owner_user_id
        existing.provider = provider
        action = "mailbox.reconnect"
    existing.display_name = (display_name or "")[:200]
    existing.tokens = tokens.seal(bundle)
    existing.scopes = list(scopes)
    existing.status = "connected"
    existing.last_error = None
    if not existing.timezone or existing.timezone == "UTC":
        existing.timezone = zone
    await ts.flush()
    await record_audit(ts, action, actor_user_id=owner_user_id, target_type="mailbox",
                       target_id=existing.id, meta={"provider": provider})
    return existing


async def list_mailboxes(ts: TenantSession, *, user_id: str, team: bool) -> list[MailboxConnection]:
    where = [] if team else [MailboxConnection.owner_user_id == user_id]
    stmt = ts.select(MailboxConnection, *where).order_by(MailboxConnection.created_at)
    return list((await ts.session.scalars(stmt)).all())


async def update_mailbox(
    ts: TenantSession, connection: MailboxConnection, *, timezone=_UNSET, signature=_UNSET,
    reply_confidence=_UNSET,
) -> MailboxConnection:
    """Edit the SDR's own settings. Raises ``ValueError`` naming the problem; writes nothing then."""
    from nexus.engagement.settings import read_settings, validate_mailbox_confidence
    from nexus.models.identity import Tenant

    if timezone is not _UNSET:
        if zone_or_none(timezone) is None:
            raise ValueError(f"{timezone!r} is not a timezone name like Europe/London")
    if reply_confidence is not _UNSET:
        tenant = await ts.session.get(Tenant, ts.tenant_id)
        reply_confidence = validate_mailbox_confidence(
            read_settings(getattr(tenant, "email_settings", None)), reply_confidence
        )
    if timezone is not _UNSET:
        connection.timezone = timezone
    if signature is not _UNSET:
        connection.signature = (signature or "")[:4000]
    if reply_confidence is not _UNSET:
        connection.reply_confidence = reply_confidence
    await ts.flush()
    return connection


async def disconnect(ts: TenantSession, connection: MailboxConnection, *, actor_user_id: str) -> None:
    """Revoke at the provider (best effort), delete the tokens, keep the row as history."""
    from nexus.engagement.config import oauth_app
    from nexus.engagement.mailboxes import oauth

    bundle = tokens.unseal(connection.tokens)
    try:
        await oauth.revoke(await oauth_app(connection.provider), bundle)
    except Exception:  # a failed revoke must not keep the tokens: deleting them is the disconnect
        pass
    connection.tokens = {}
    connection.status = "revoked"
    connection.sync_cursor = None
    connection.notifications_expire_at = None
    connection.notification_subscription_id = None
    connection.last_error = None
    await ts.flush()
    await record_audit(ts, "mailbox.disconnect", actor_user_id=actor_user_id,
                       target_type="mailbox", target_id=connection.id,
                       meta={"provider": connection.provider,
                             "by_owner": actor_user_id == connection.owner_user_id})


async def check_connection(ts: TenantSession, connection: MailboxConnection):
    """Refresh the token and read the profile: proves the mailbox can be used right now."""
    from nexus.engagement.mailboxes.provider import ProviderError
    from nexus.engagement.mailboxes.registry import provider_for

    token = await tokens.fresh_access_token(ts, connection, force=True)
    profile = await provider_for(connection, token).profile()
    if profile.email and profile.email != connection.email:
        connection.status = "error"
        connection.last_error = (
            f"This connection now signs in as {profile.email}, not {connection.email}. "
            "Disconnect it and connect the right mailbox."
        )
        await ts.flush()
        raise ProviderError(connection.last_error)
    return profile


def sending_state(connection: MailboxConnection, *, now: datetime | None = None) -> tuple[bool, str]:
    """Whether this mailbox may send right now, and why not (pre-send check 4, spec §5)."""
    from nexus.core.db import ensure_aware

    now = now or utcnow()
    if connection.status != "connected":
        return False, f"mailbox {connection.status}"
    paused = ensure_aware(connection.paused_until)
    if paused is not None and paused > now:
        return False, "provider limit"
    return True, ""
