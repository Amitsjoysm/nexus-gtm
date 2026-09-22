"""The one place a connected mailbox becomes a provider object."""
from __future__ import annotations

from nexus.engagement.mailboxes.provider import MailProvider


def make_provider(provider: str, *, access_token: str, email: str = "") -> MailProvider:
    if provider == "google":
        from nexus.engagement.mailboxes.gmail import GmailProvider

        return GmailProvider(access_token=access_token, email=email)
    if provider == "microsoft":
        from nexus.engagement.mailboxes.graph import GraphProvider

        return GraphProvider(access_token=access_token, email=email)
    raise ValueError(f"unknown mailbox provider {provider!r}")


def provider_for(connection, access_token: str) -> MailProvider:
    return make_provider(connection.provider, access_token=access_token, email=connection.email)


async def open_provider(ts, connection) -> MailProvider:
    """A provider with a fresh access token. Raises ``AuthExpired`` (and marks the connection
    ``needs_reauth``) when the grant is gone."""
    from nexus.engagement.mailboxes.tokens import fresh_access_token

    return provider_for(connection, await fresh_access_token(ts, connection))
