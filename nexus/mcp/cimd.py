# nexus/mcp/cimd.py
"""OAuth Client ID Metadata Documents: an app's client id is an https URL serving its own metadata.

The MCP spec's preferred way for an app with no prior relationship to identify itself (2025-11-25,
and the 2026-07-28 revision deprecates Dynamic Client Registration in its favour); Claude uses it by
default when the server advertises it.

Fetching a URL a stranger chose is an SSRF primitive, so: https only, a path required (the draft's
rule), the resolve-then-check host guard ``sources.safety`` already applies to source DSNs and the
fetch service, no redirects followed, a 5 s timeout and a 64 KB cap. The document must name itself:
its ``client_id`` must equal the URL exactly, or anyone could serve metadata claiming to be another
app.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

MAX_BYTES = 64 * 1024
TIMEOUT_S = 5.0
MIN_CACHE_S = 3600
MAX_CACHE_S = 86400


class CimdError(Exception):
    """The client id URL could not be used. The message is shown on the error page."""


@dataclass(frozen=True)
class ClientMetadata:
    client_id: str
    client_name: str
    client_uri: str
    logo_uri: str
    redirect_uris: list[str]
    token_endpoint_auth_method: str
    cache_seconds: int


def looks_like_cimd(client_id: str) -> bool:
    parsed = urlparse(client_id or "")
    return parsed.scheme == "https" and bool(parsed.hostname) and parsed.path not in ("", "/")


def _cache_seconds(cache_control: str) -> int:
    match = re.search(r"max-age=(\d+)", cache_control or "")
    seconds = int(match.group(1)) if match else MIN_CACHE_S
    return max(MIN_CACHE_S, min(MAX_CACHE_S, seconds))


async def _get(url: str) -> tuple[int, dict[str, str], bytes]:
    """One GET, capped. Separate so tests can replace the network."""
    async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False) as client:
        async with client.stream("GET", url, headers={"Accept": "application/json"}) as resp:
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise CimdError("the client metadata document is too large")
                chunks.append(chunk)
            return resp.status_code, dict(resp.headers), b"".join(chunks)


async def fetch(url: str) -> ClientMetadata:
    from nexus.core.config import get_settings
    from nexus.mcp.oauth import valid_redirect
    from nexus.sources.safety import _is_blocked_host

    if not looks_like_cimd(url):
        raise CimdError("a client id URL must be https and have a path")
    host = urlparse(url).hostname or ""
    blocked, why = _is_blocked_host(host, allow_private=get_settings().env in ("local", "test"))
    if blocked:
        raise CimdError(f"refusing to fetch client metadata from this host: {why}")
    try:
        status, headers, body = await _get(url)
    except CimdError:
        raise
    except Exception as exc:
        raise CimdError(f"could not fetch the client metadata document: {exc}") from exc
    if status != 200:
        raise CimdError(f"the client metadata document answered {status}")
    try:
        doc = json.loads(body)
    except ValueError as exc:
        raise CimdError("the client metadata document is not JSON") from exc
    if not isinstance(doc, dict):
        raise CimdError("the client metadata document is not a JSON object")
    if doc.get("client_id") != url:
        raise CimdError("the document's client_id does not match its URL")
    name = doc.get("client_name")
    redirects = doc.get("redirect_uris")
    if not isinstance(name, str) or not name.strip():
        raise CimdError("the document has no client_name")
    if not isinstance(redirects, list) or not redirects or not all(
            isinstance(r, str) and valid_redirect(r) for r in redirects):
        raise CimdError("the document's redirect_uris are missing or not allowed")
    lower = {k.lower(): v for k, v in headers.items()}
    return ClientMetadata(
        client_id=url, client_name=name.strip()[:200],
        client_uri=str(doc.get("client_uri") or "")[:512],
        logo_uri=str(doc.get("logo_uri") or "")[:512],
        redirect_uris=[r for r in redirects][:20],
        token_endpoint_auth_method=str(doc.get("token_endpoint_auth_method") or "none"),
        cache_seconds=_cache_seconds(lower.get("cache-control", "")),
    )
