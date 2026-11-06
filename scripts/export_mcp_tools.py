#!/usr/bin/env python
"""Write the MCP tool definitions Microsoft's certification package carries.

    python scripts/export_mcp_tools.py

Writes ``distribution/microsoft/mcptools.json``: what ``tools/list`` answers to a connection with
every permission, minus live prices (a research tool says it "costs credits, as in the app"; the
server names the number when it answers). ASCII only, as Microsoft requires of the package.
``tests/test_mcp_distribution.py`` fails when the file and the catalogue disagree, so run this
after adding, renaming or redescribing a tool, then resubmit the Microsoft package.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "distribution" / "microsoft" / "mcptools.json"


def tool_definitions() -> dict:
    sys.path.insert(0, str(ROOT))
    from nexus.mcp.tools import TOOLS, describe

    return {"tools": [describe(tool, {}) for tool in TOOLS]}


def render() -> str:
    return json.dumps(tool_definitions(), indent=2, ensure_ascii=True) + "\n"


if __name__ == "__main__":
    OUT.write_text(render(), encoding="ascii", newline="\n")
    print(f"{OUT.relative_to(ROOT)}: {len(tool_definitions()['tools'])} tools")
