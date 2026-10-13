# nexus/mcp/server.py
"""Answers one authenticated JSON-RPC request: what each MCP method does in NEXUS.

The route (``api/routers/mcp.py``) has already authenticated the caller, checked the workspace may
use AI apps, and applied the rate limit; this module decides the answer. Faults the client caused
are JSON-RPC errors; a tool that ran and was refused (not enough credits, a record that does not
exist, a permission this connection lacks) is a tool RESULT with ``isError: true``, because that is
something the model should read and explain, not a protocol failure.
"""
from __future__ import annotations

import json
import logging

from nexus.mcp import protocol, safety, schema
from nexus.mcp.credentials import Actor
from nexus.mcp.gateway import Gateway, ToolOutcome
from nexus.mcp.tools import TOOLS, by_name, describe, load_prices
from nexus.mcp.brand import PRODUCT_NAME, SERVER_ID

logger = logging.getLogger("nexus.mcp.server")

SERVER_INFO = {"name": SERVER_ID, "title": PRODUCT_NAME, "version": "1.0.0"}
CAPABILITIES = {"tools": {"listChanged": False}}
#: How long a client may cache tools/list (2026-07-28 `CacheableResult`). The list changes only
#: when the connection's permissions do, and a permission change takes effect within the
#: credential cache anyway.
LIST_TTL_MS = 300_000

_SCOPE_WORDS = {
    "read": "read workspace data",
    "research": "run research that spends credits",
    "write": "add or change records",
}


def _allowed_tools(actor: Actor):
    return [t for t in TOOLS if t.scope in actor.scopes]


def _tool_result(data: object, *, is_error: bool) -> dict:
    if not isinstance(data, dict):
        data = {"items": data}
    data = safety.cap_result(safety.clean(data))
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}],
        "structuredContent": data,
        "isError": is_error,
    }


def _ids(args: dict) -> dict:
    """Just the identifiers from a call, for the audit row: never free text a model wrote."""
    return {k: v for k, v in args.items()
            if k == "id" or k.endswith("_id") or k.endswith("_ids")}


async def _audit(actor: Actor, tool: str, args: dict, outcome: str) -> None:
    """One `mcp.tool` row for a research or write call. Never raises."""
    try:
        from nexus.core.audit import record_audit
        from nexus.core.db import get_sessionmaker
        from nexus.core.tenancy import TenantSession, apply_rls

        async with get_sessionmaker()() as s:
            await apply_rls(s, actor.tenant_id)
            ts = TenantSession(s, actor.tenant_id)
            await record_audit(ts, "mcp.tool", actor_user_id=actor.user_id,
                               target_type="mcp_tool", target_id=tool,
                               meta={"connection_id": actor.connection_id, "outcome": outcome,
                                     "args": _ids(args)})
            await s.commit()
    except Exception:  # pragma: no cover - bookkeeping only
        logger.warning("could not audit MCP call %s", tool, exc_info=True)


async def call_tool(app, actor: Actor, params: dict) -> dict:
    from nexus.core import metrics

    name = params.get("name")
    if not isinstance(name, str) or by_name(name) is None:
        raise protocol.ProtocolError(protocol.INVALID_PARAMS, f"unknown tool: {name!r}")
    spec = by_name(name)
    if spec.scope not in actor.scopes:
        metrics.record_mcp_tool(name, "refused")
        return _tool_result({
            "error": f"This connection is not allowed to {_SCOPE_WORDS[spec.scope]}. The user can "
                     f"change its permissions in {PRODUCT_NAME} > Settings > AI apps.",
        }, is_error=True)
    try:
        args = schema.validate(spec.input_schema, params.get("arguments"))
    except schema.SchemaError as exc:
        raise protocol.ProtocolError(protocol.INVALID_PARAMS, str(exc)) from exc
    try:
        outcome = await spec.handler(Gateway(app, actor), args)
    except ValueError as exc:  # a path the gateway refused: a bug, never the user's fault
        logger.error("tool %s built a path the gateway refused: %s", name, exc)
        outcome = ToolOutcome(False, None, "This tool could not run.", 500)
    status = "ok" if outcome.ok else "error"
    metrics.record_mcp_tool(name, status)
    if spec.scope != "read":
        await _audit(actor, name, args, status if outcome.ok else f"error:{outcome.status}")
    if outcome.ok:
        return _tool_result(outcome.data, is_error=False)
    return _tool_result({"error": outcome.message, "status": outcome.status}, is_error=True)


async def handle(app, actor: Actor, req: protocol.Request, *, stateless: bool,
                 version: str) -> dict | None:
    """The JSON-RPC response for ``req``, or None for a notification."""
    if req.is_notification:
        return None
    method = req.method

    def ok(payload: dict) -> dict:
        return protocol.result(req.id, payload, stateless=stateless, server_info=SERVER_INFO)

    if method == "initialize" and not stateless:
        return ok({"protocolVersion": version, "capabilities": CAPABILITIES,
                   "serverInfo": SERVER_INFO, "instructions": safety.INSTRUCTIONS})
    if method == "server/discover":
        return ok({"supportedVersions": list(protocol.SUPPORTED_VERSIONS),
                   "capabilities": CAPABILITIES, "serverInfo": SERVER_INFO,
                   "instructions": safety.INSTRUCTIONS})
    if method == "ping" and not stateless:
        return ok({})
    if method == "tools/list":
        tools = _allowed_tools(actor)
        prices = await load_prices({t.price_capability for t in tools if t.price_capability})
        payload: dict = {"tools": [describe(t, prices) for t in tools]}
        if stateless:
            payload["ttlMs"] = LIST_TTL_MS
            payload["cacheScope"] = "private"
        return ok(payload)
    if method == "tools/call":
        return ok(await call_tool(app, actor, req.params))
    raise protocol.ProtocolError(protocol.METHOD_NOT_FOUND, f"method not found: {method}")
