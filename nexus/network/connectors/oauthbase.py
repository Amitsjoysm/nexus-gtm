"""Shared OAuth 2.0 (authorization-code + PKCE) machinery for real network connectors.

Subclasses set the provider endpoints/scopes and implement ``fetch``. This base owns: building the
authorize URL, exchanging a code for tokens, refreshing, and a configured httpx client (timeouts +
bounded retry/backoff on 429/5xx). A ``transport`` arg lets tests inject ``httpx.MockTransport`` so
the suite never touches the network.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import httpx

from nexus.network.connectors.base import NetworkSyncBatch, SourceAccountRef

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_RETRY_STATUS = {429, 500, 502, 503, 504}


class OAuthConnector:
    provider: str = ""
    authorize_url: str = ""
    token_url: str = ""
    scopes: list[str] = []
    extra_authorize_params: dict[str, str] = {}

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: httpx.BaseTransport | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._transport = transport  # tests inject MockTransport; prod leaves None (real network)

    # ---- OAuth ----
    def authorize_url_for(self, *, state: str, code_challenge: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            **self.extra_authorize_params,
        }
        return f"{self.authorize_url}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, code_verifier: str) -> dict:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": code_verifier,
            }
        )

    async def refresh(self, refresh_token: str) -> dict:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def _token_request(self, data: dict) -> dict:
        data = {"client_id": self.client_id, "client_secret": self.client_secret, **data}
        async with self._client() as c:
            resp = await c.post(self.token_url, data=data)
            resp.raise_for_status()
            return resp.json()

    # ---- HTTP ----
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport)

    async def _get_json(
        self, client: httpx.AsyncClient, url: str, *, token: str, params: dict | None = None
    ) -> httpx.Response:
        """GET with bearer auth + up to 3 bounded retries on 429/5xx."""
        last: httpx.Response | None = None
        for attempt in range(3):
            resp = await client.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code not in _RETRY_STATUS:
                return resp
            last = resp
            await asyncio.sleep(0.5 * (2**attempt))
        return last  # type: ignore[return-value]

    # ---- contract ----
    async def fetch(self, account: SourceAccountRef, since: str | None) -> NetworkSyncBatch:
        raise NotImplementedError
