# nexus/models/mcp.py
"""Connected AI apps: API keys, OAuth grants, and the OAuth server's clients, codes and tokens.

**Platform-global, deliberately NOT tenant-scoped.** A bearer arriving at ``/mcp`` or at
``/oauth/token`` is resolved before any workspace is known, the same reason ``user_mfa`` has no
``tenant_id``: ``scripts/apply_rls.py`` enrols every table having a ``tenant_id``, and under a policy
the lookup would return zero rows instead of failing, so every connected app would silently stop
working the moment RLS was applied. The workspace a connection belongs to is recorded as
``subject_tenant_id`` (the ``billing_audit_log`` convention), and every read filters on it in code.
That filter is the only isolation these rows have, which is why the connections API has a
cross-workspace test for every endpoint.

Nothing reversible is stored. API keys, codes and tokens are kept as SHA-256 digests of 256-bit
random secrets (a slow hash buys nothing at that entropy); a confidential client's secret likewise.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from nexus.core.db import Base, IdMixin, TimestampMixin, TZDateTime

#: What a connection may do. Chosen per connection, never more than the user's own role allows,
#: which the API itself enforces on every call because tools run as the user.
MCP_SCOPES = ("read", "research", "write")
CONNECTION_KINDS = ("api_key", "oauth")
CLIENT_KINDS = ("cimd", "dcr", "registered")


class McpConnection(IdMixin, TimestampMixin, Base):
    """One API key or one OAuth grant: the thing a user sees and revokes on the AI apps page."""

    __tablename__ = "mcp_connections"

    subject_tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(120), default="")
    # The OAuth client this grant was made to (`oauth_clients.client_id`). Empty for an API key.
    client_id: Mapped[str] = mapped_column(String(512), default="")
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    # First characters of an API key, so the page can say which key a row is. Never enough to use.
    key_prefix: Mapped[str] = mapped_column(String(16), default="")
    key_digest: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    revoked_by: Mapped[str] = mapped_column(String(32), default="")


class OAuthClient(IdMixin, TimestampMixin, Base):
    """An app that may ask a NEXUS user for access.

    ``cimd``: the client id is an https URL serving its own metadata (the spec's preferred way).
    ``dcr``: registered itself through /oauth/register (kept for older clients).
    ``registered``: created by a superadmin, with a secret (e.g. Anthropic-held credentials).
    """

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    client_name: Mapped[str] = mapped_column(String(200), default="")
    client_uri: Mapped[str] = mapped_column(String(512), default="")
    logo_uri: Mapped[str] = mapped_column(String(512), default="")
    redirect_uris: Mapped[list] = mapped_column(JSON, default=list)
    token_endpoint_auth_method: Mapped[str] = mapped_column(String(32), default="none")
    client_secret_digest: Mapped[str] = mapped_column(String(64), default="")
    # When a CIMD document must be fetched again (HTTP cache horizon, clamped).
    metadata_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class OAuthCode(TimestampMixin, Base):
    """A single-use authorization code: 60 seconds, bound to its client, redirect and PKCE."""

    __tablename__ = "oauth_codes"

    code_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(512))
    subject_tenant_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(String(32))
    connection_id: Mapped[str] = mapped_column(ForeignKey("mcp_connections.id"), index=True)
    redirect_uri: Mapped[str] = mapped_column(String(1024))
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    code_challenge: Mapped[str] = mapped_column(String(128))
    resource: Mapped[str] = mapped_column(String(512), default="")
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    used_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class OAuthToken(TimestampMixin, Base):
    """An access or refresh token. Refresh tokens rotate; every token of one grant shares a
    ``family_id``, so presenting a refresh token twice revokes the whole family (OAuth 2.1)."""

    __tablename__ = "oauth_tokens"

    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("mcp_connections.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    family_id: Mapped[str] = mapped_column(String(32), index=True)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    used_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
