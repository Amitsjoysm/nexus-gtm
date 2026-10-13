# nexus/mcp/urls.py
"""The addresses the MCP server and the OAuth server publish about themselves.

``base`` is the App public address (``app_base_url``) when set, which is what production must use:
Claude requires the protected-resource ``resource`` to equal the URL the user typed, exactly. A
local stack without it answers with the request's own origin, so it still works on localhost.
"""
from __future__ import annotations

from fastapi import Request


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
