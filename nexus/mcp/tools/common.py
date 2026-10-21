# nexus/mcp/tools/common.py
"""What every tool module shares: argument schema pieces, links into the app, and outcomes.

Kept apart from ``catalog.py`` so a tool module can be small and single-purpose without importing
the whole catalogue.
"""
from __future__ import annotations

from nexus.mcp.gateway import ToolOutcome
from nexus.mcp.tools.registry import ID_PATTERN

ID = {"type": "string", "pattern": ID_PATTERN}
IDS = {"type": "array", "items": {"type": "string", "pattern": ID_PATTERN}, "maxItems": 500}


def obj(properties: dict, required: tuple[str, ...] = ()) -> dict:
    schema: dict = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


def base_url() -> str:
    from nexus.core.config import get_settings

    return (get_settings().app_base_url or "").rstrip("/")


def link(path: str) -> str | None:
    """A link into the app for the user to open, when the deployment knows its own address."""
    base = base_url()
    return f"{base}{path}" if base else None


def fail(out: ToolOutcome) -> ToolOutcome:
    return ToolOutcome(False, None, out.message, out.status)


def ok(data) -> ToolOutcome:
    return ToolOutcome(True, data)


def refused(message: str, status: int = 422) -> ToolOutcome:
    return ToolOutcome(False, None, message, status)
