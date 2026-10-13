#!/usr/bin/env python
"""Talk to a running MCP endpoint with the OFFICIAL MCP client, in both protocol dialects.

Our server is a hand-written implementation of the protocol (`nexus/mcp/protocol.py`); this is the
check that a real client agrees with it. Run it against a local preview, staging or production:

    pip install "mcp==2.3.0"
    python scripts/mcp_conformance.py --url http://127.0.0.1:9129/mcp --key nxk_...

It lists tools and calls two read-only ones (`get_credit_balance`, `search`), so it spends no
credits and changes nothing. It runs twice: `--mode auto` (the stateless 2026-07-28 revision, via
`server/discover`) and `--mode legacy` (the `initialize` handshake), unless one mode is given.
Exit code 0 means every step passed. The key is read from `--key` or `MCP_KEY`; it is never printed.

Deliberately not a pytest file: the suite's conftest imports the app, and the official SDK pulls
packages (httpx2 among them) that the pinned web stack does not need at run time.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys


def _attr(obj, snake: str, camel: str):
    """SDK 2.x models use snake_case (`is_error`); 1.x used the wire names (`isError`)."""
    return getattr(obj, snake, getattr(obj, camel, None))


async def check(url: str, key: str, mode: str) -> list[str]:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    failures: list[str] = []
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60.0)
    async with Client(streamable_http_client(url, http_client=http), mode=mode,
                      raise_exceptions=True) as client:
        listed = await client.list_tools()
        tools = {t.name: t for t in listed.tools}
        print(f"[{mode}] {len(tools)} tools: {', '.join(sorted(tools))}")
        for name, tool in tools.items():
            ann = tool.annotations
            read_only = _attr(ann, "read_only_hint", "readOnlyHint") if ann else None
            destructive = _attr(ann, "destructive_hint", "destructiveHint") if ann else None
            if ann is None or not ann.title or read_only is None:
                failures.append(f"{name}: missing title or readOnlyHint")
            elif read_only is False and destructive is None:
                failures.append(f"{name}: write tool without destructiveHint")
        if "get_credit_balance" in tools:
            result = await client.call_tool("get_credit_balance", {})
            content = _attr(result, "structured_content", "structuredContent")
            ok = (not _attr(result, "is_error", "isError") and isinstance(content, dict)
                  and "balance" in content)
            print(f"[{mode}] get_credit_balance: {'ok' if ok else 'FAILED'}")
            if not ok:
                failures.append("get_credit_balance did not return a balance")
        else:
            failures.append("get_credit_balance not listed (does the key have Read?)")
        if "search" in tools:
            result = await client.call_tool("search", {"query": "a", "limit": 3})
            content = _attr(result, "structured_content", "structuredContent") or {}
            ok = not _attr(result, "is_error", "isError") and "results" in content
            print(f"[{mode}] search: {'ok' if ok else 'FAILED'}")
            if not ok:
                failures.append("search did not return results")
        if "fetch" in tools:
            try:
                bad = await client.call_tool("fetch", {"id": "account:../../etc"})
                if not _attr(bad, "is_error", "isError"):
                    failures.append("fetch accepted a path-traversal id")
            except Exception:
                pass  # refused as invalid params, which is the right answer
            print(f"[{mode}] path-traversal id refused: ok")
    return failures


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True, help="the MCP endpoint, ending in /mcp")
    parser.add_argument("--key", default=os.environ.get("MCP_KEY", ""),
                        help="an API key (nxk_...); or set MCP_KEY")
    parser.add_argument("--mode", choices=("auto", "legacy", "both"), default="both")
    args = parser.parse_args()
    if not args.key:
        parser.error("give --key or set MCP_KEY")
    modes = ("auto", "legacy") if args.mode == "both" else (args.mode,)
    failures: list[str] = []
    for mode in modes:
        try:
            failures += [f"[{mode}] {f}" for f in await check(args.url, args.key, mode)]
        except Exception as exc:  # a protocol failure is exactly what this script is for
            failures.append(f"[{mode}] {type(exc).__name__}: {exc}")
    for f in failures:
        print("FAIL", f)
    print("PASS" if not failures else f"{len(failures)} failure(s)")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
