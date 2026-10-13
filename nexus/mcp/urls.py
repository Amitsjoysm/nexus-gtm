# nexus/mcp/urls.py
"""The addresses the MCP server and the OAuth server publish about themselves.

``base`` is the App public address (``app_base_url``) when set, which is what production must use:
Claude requires the protected-resource ``resource`` to equal the URL the user typed, exactly. A
local stack without it answers with the request's own origin, so it still works on localhost.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

from fastapi import Request

# Names that resolve only on this machine or this network, never on the public internet.
_LOCAL_SUFFIXES = (".localhost", ".local", ".test", ".internal", ".lan", ".home.arpa")


def base_url(request: Request | None = None) -> str:
    from nexus.core.config import get_settings

    configured = (get_settings().app_base_url or "").strip().rstrip("/")
    if configured:
        return configured
    if request is None:
        return ""
    return str(request.base_url).rstrip("/")


def mcp_url(request: Request | None = None) -> str:
    return f"{base_url(request)}/mcp"


def resource_metadata_url(request: Request | None = None) -> str:
    return f"{base_url(request)}/.well-known/oauth-protected-resource"


def internet_reachable(url: str) -> bool:
    """Could Claude's or ChatGPT's servers open this address? They connect from the internet, so a
    loopback, private or reserved IP, ``localhost``, a single-label container name or a local-only
    suffix cannot work, and the AI apps page says so instead of letting the steps fail silently.
    Decided from the address alone: no DNS lookup on a page load."""
    host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES) or "." not in host:
        return False
    return True
