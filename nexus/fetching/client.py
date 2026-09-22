"""Talks to nexus-fetch. Total: a failure is an empty answer plus a reason, never an exception.

Signal collection must degrade, not stop — the posture every provider seam in this codebase takes.
`last_failure` carries the reason so the crawl-history row can record `error` rather than `empty`:
a dead service and a quiet market must never look alike.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("nexus.fetching.client")


class FetchClient:
    def __init__(self, *, base_url: str = "", token: str = "", timeout_s: float = 20.0,
                 http: httpx.AsyncClient | None = None):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.timeout_s = float(timeout_s)
        self._http = http
        #: Why the LAST call could not answer, empty when it could.
        self.last_failure = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    async def _call(self, method: str, path: str, payload: dict | None = None) -> dict | None:
        if not self.configured:
            self.last_failure = "not configured"
            return None
        client = self._http or httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_s)
        try:
            response = await client.request(method, path, json=payload,
                                            headers={"X-Fetch-Token": self.token})
            if response.status_code == 503:
                self.last_failure = "blocked"
                return None
            if response.status_code >= 400:
                self.last_failure = f"service returned {response.status_code}"
                return None
            body = response.json()
            self.last_failure = ""
            return body if isinstance(body, dict) else None
        except Exception as exc:
            self.last_failure = f"unreachable: {type(exc).__name__}"
            logger.warning("nexus-fetch %s %s failed: %r", method, path, exc)
            return None
        finally:
            if self._http is None:
                await client.aclose()

    async def search(self, query: str, *, limit: int = 5, recency_days: int = 0) -> list[dict]:
        body = await self._call("POST", "/search", {"query": query, "limit": limit,
                                                    "recency_days": recency_days})
        hits = (body or {}).get("hits")
        return [h for h in hits if isinstance(h, dict)] if isinstance(hits, list) else []

    async def fetch_page(self, url: str, *, mode: str = "http") -> dict | None:
        return await self._call("POST", "/fetch",
                                {"url": url, "mode": mode, "timeout_s": self.timeout_s})

    async def health(self) -> dict | None:
        """The service's own health payload, or ``None`` with ``last_failure`` set."""
        return await self._call("GET", "/health")


_client: FetchClient | None = None
#: True when a caller installed the client deliberately (the test seam). An explicit client is never
#: rebuilt from settings, or every injected double would be replaced on its first use.
_explicit = False


def get_fetch_client() -> FetchClient:
    """Process-wide client built from settings, rebuilt when the URL, token or timeout changes.

    Settings are read on every call: the ingestion service is built once and keeps its sources, so a
    client captured at import would ignore a URL changed in the Control plane until a restart.
    """
    global _client
    from nexus.core.config import get_settings

    if _explicit and _client is not None:
        return _client
    settings = get_settings()
    url = (settings.fetch_service_url or "").strip().rstrip("/")
    token = (settings.fetch_service_token or "").strip()
    timeout = float(settings.fetch_service_timeout_s)
    if (_client is None or _client.base_url != url or _client.token != token
            or _client.timeout_s != timeout):
        _client = FetchClient(base_url=url, token=token, timeout_s=timeout)
    return _client


def set_fetch_client(client: FetchClient | None) -> None:
    """Test seam, mirroring `set_search_provider`. ``None`` rebuilds from settings on next use."""
    global _client, _explicit
    _client = client
    _explicit = client is not None
