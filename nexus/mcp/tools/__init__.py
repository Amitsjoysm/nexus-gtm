# nexus/mcp/tools/__init__.py
"""The MCP tool catalogue. ``TOOLS`` is the fixed order ``tools/list`` answers in."""
from nexus.mcp.tools.catalog import TOOLS, by_name
from nexus.mcp.tools.registry import ToolSpec, describe, load_prices

__all__ = ["TOOLS", "ToolSpec", "by_name", "describe", "load_prices"]
