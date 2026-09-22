"""The one place a connected mailbox becomes a provider object."""
from __future__ import annotations

from collections.abc import Callable

from nexus.engagement.mailboxes.provider import MailProvider

#: The test seam, in the mould of `set_crm_connector` and `set_task_queue`: when installed it builds
#: the provider for every connection and token refresh is skipped. Production never sets it, and
#: what the real Gmail and Graph providers do is proved against the real services in
#: `tests_live/engagement/`, not here.
_factory: Callable[[object], MailProvider] | None = None


def set_provider_factory(factory: Callable[[object], MailProvider] | None) -> None:
    global _factory
    _factory = factory


def make_provider(provider: str, *, access_token: str, email: str = "") -> MailProvider:
    if provider == "google":
        from nexus.engagement.mailboxes.gmail import GmailProvider

        return GmailProvider(access_token=access_token, email=email)
    if provider == "microsoft":
        from nexus.engagement.mailboxes.graph import GraphProvider

        return GraphProvider(access_token=access_token, email=email)
    raise ValueError(f"unknown mailbox provider {provider!r}")


def provider_for(connection, access_token: str) -> MailProvider:
    if _factory is not None:
        return _factory(connection)
    return make_provider(connection.provider, access_token=access_token, email=connection.email)


async def open_provider(ts, connection) -> MailProvider:
    """A provider with a fresh access token. Raises ``AuthExpired`` (and marks the connection
    ``needs_reauth``) when the grant is gone."""
    if _factory is not None:
        return _factory(connection)
    from nexus.engagement.mailboxes.tokens import fresh_access_token

    return provider_for(connection, await fresh_access_token(ts, connection))
