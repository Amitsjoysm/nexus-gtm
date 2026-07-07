"""The swappable network-connector seam.

A connector turns a member's connected provider account into a ``NetworkSyncBatch`` of raw
identities + touchpoints. ``FixtureConnector`` runs offline; real Google/Microsoft adapters
implement the same surface — only the bodies differ, so the graph never changes shape.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field


class RawIdentity(BaseModel):
    external_id: str
    email: str | None = None
    name: str | None = None
    title: str | None = None
    company: str | None = None
    handle: str | None = None
    relation: str = "contact"  # contact | email | calendar | linkedin_1st | follower
    # Provider-specific payload. String fields above are length-clamped at the ingest
    # boundary (nexus/network/service.ingest_batch), NOT rejected here — a single oversized
    # field must never fail a whole sync batch (mirrors nexus/ingestion/service clamping).
    raw: dict[str, Any] = Field(default_factory=dict)


class Touchpoint(BaseModel):
    person_external_id: str
    kind: str  # email_sent | email_received | meeting
    at: datetime


class NetworkSyncBatch(BaseModel):
    identities: list[RawIdentity] = Field(default_factory=list)
    touchpoints: list[Touchpoint] = Field(default_factory=list)
    next_cursor: str | None = None


class AuthChallenge(BaseModel):
    oauth_url: str | None = None
    state: str | None = None


class OAuthTokens(BaseModel):
    access_token: str = ""
    refresh_token: str = ""
    expires_at: datetime | None = None
    scopes: list[str] = Field(default_factory=list)


class SourceAccountRef(BaseModel):
    id: str
    provider: str
    external_account_id: str
    oauth: dict[str, Any] = Field(default_factory=dict)


# Protocol (not abc.ABC) so a real google.py / microsoft.py adapter can satisfy this seam by
# structural typing without importing or subclassing base.py.
class NetworkConnector(Protocol):
    provider: str

    async def begin_auth(self, redirect_uri: str) -> AuthChallenge: ...

    async def complete_auth(self, code: str) -> OAuthTokens: ...

    async def fetch(self, account: SourceAccountRef, since: str | None) -> NetworkSyncBatch: ...
