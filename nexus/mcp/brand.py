# nexus/mcp/brand.py
"""The product name AI apps show customers, in one place.

Customers sign in to "InfoJoy GTM" (the login screen, the page title, the app's own title), so that
is the name Claude, ChatGPT and the marketplace listings must use; NEXUS is the codebase's name.
The frontend's ``lib/brand.ts`` holds the same two values for the AI apps page and consent screen.
"""
from __future__ import annotations

PRODUCT_NAME = "InfoJoy GTM"
#: The MCP server's machine identifier (``serverInfo.name``) and the key in client config files.
SERVER_ID = "infojoy-gtm"
