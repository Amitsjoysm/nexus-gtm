"""Offline connector: holds a canned batch so the whole graph runs with zero external services."""
from __future__ import annotations

from nexus.network.connectors.base import (
    AuthChallenge,
    NetworkSyncBatch,
    OAuthTokens,
    SourceAccountRef,
)


class FixtureConnector:
    provider = "fixture"

    def __init__(self, batch: NetworkSyncBatch | None = None):
        self._batch = batch or NetworkSyncBatch()

    async def begin_auth(self, redirect_uri: str) -> AuthChallenge:
        return AuthChallenge(oauth_url=None, state="fixture")

    async def complete_auth(self, code: str) -> OAuthTokens:
        return OAuthTokens(access_token="fixture-token")

    async def fetch(self, account: SourceAccountRef, since: str | None) -> NetworkSyncBatch:
        return self._batch
