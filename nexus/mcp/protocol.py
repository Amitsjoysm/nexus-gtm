# nexus/mcp/protocol.py
"""JSON-RPC envelopes and protocol versions for the one MCP transport we serve: Streamable HTTP,
POST only, JSON responses, no server-pushed stream and no session.

Two dialects, because clients in the field speak both (2026-10):

* **handshake** (``2025-03-26``, ``2025-06-18``, ``2025-11-25``): the client opens with
  ``initialize``, names its version in the ``MCP-Protocol-Version`` header afterwards, and may
  ``ping``. A request with no header at all is ``2025-03-26``, as those revisions specify.
* **stateless** (``2026-07-28``): no handshake. Every request carries its version in
  ``params._meta["io.modelcontextprotocol/protocolVersion"]``, ``server/discover`` replaces
  ``initialize``, and every result says ``resultType: "complete"`` and who answered.

Nothing here knows about NEXUS; ``server.py`` decides what each method does.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

HANDSHAKE_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18", "2025-03-26")  # newest first
STATELESS_VERSIONS: tuple[str, ...] = ("2026-07-28",)
SUPPORTED_VERSIONS: tuple[str, ...] = STATELESS_VERSIONS + HANDSHAKE_VERSIONS
#: What a handshake-dialect request with no version header means, per the 2025 revisions.
DEFAULT_HEADER_VERSION = "2025-03-26"

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
#: Implementation-defined range (-32000..-32019): the workspace cannot use AI apps right now.
UNAVAILABLE = -32001
#: Reserved by the 2026-07-28 revision.
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022


class ProtocolError(Exception):
    """A fault the client caused, answered as a JSON-RPC error rather than a tool result."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True)
class Request:
    id: str | int | None
    method: str
    params: dict = field(default_factory=dict)
    is_notification: bool = False

    @property
    def meta(self) -> dict:
        meta = self.params.get("_meta")
        return meta if isinstance(meta, dict) else {}


def parse(body: bytes) -> Request:
    """One JSON-RPC message. A batch (a JSON array) is refused: the 2025-06-18 revision removed
    batching, and a stateless server has no reason to accept one."""
    try:
        msg = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError(PARSE_ERROR, f"the body is not JSON: {exc}") from exc
    if isinstance(msg, list):
        raise ProtocolError(INVALID_REQUEST, "batched requests are not supported")
    if not isinstance(msg, dict):
        raise ProtocolError(INVALID_REQUEST, "a request is a JSON object")
    method = msg.get("method")
    if not isinstance(method, str) or not method:
        raise ProtocolError(INVALID_REQUEST, "the request names no method")
    params = msg.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ProtocolError(INVALID_REQUEST, "params must be an object")
    is_notification = "id" not in msg
    req_id = msg.get("id")
    if req_id is not None and not isinstance(req_id, (str, int)):
        raise ProtocolError(INVALID_REQUEST, "id must be a string or a number")
    return Request(id=req_id, method=method, params=params, is_notification=is_notification)


def _unsupported(version: str) -> ProtocolError:
    return ProtocolError(
        UNSUPPORTED_VERSION, f"protocol version {version} is not supported",
        {"supported": list(SUPPORTED_VERSIONS), "requested": version},
    )


def dialect(req: Request, header_version: str | None) -> tuple[str, str]:
    """``("stateless", version)`` or ``("handshake", version)`` for this request.

    ``initialize`` is never refused here, whatever version it asks for: negotiating is its job.
    """
    stated = req.meta.get(META_VERSION)
    if isinstance(stated, str) and stated:
        if stated in STATELESS_VERSIONS:
            return "stateless", stated
        if stated in HANDSHAKE_VERSIONS:
            # A client naming a handshake version in _meta is still speaking the handshake dialect.
            return "handshake", stated
        raise _unsupported(stated)
    if req.method == "initialize":
        return "handshake", negotiate(req.params.get("protocolVersion"))
    version = (header_version or "").strip() or DEFAULT_HEADER_VERSION
    if version in HANDSHAKE_VERSIONS:
        return "handshake", version
    if version in STATELESS_VERSIONS:
        # The header alone, without _meta, is a stateless client that forgot _meta; answer it
        # statelessly rather than refuse.
        return "stateless", version
    raise _unsupported(version)


def negotiate(requested: object) -> str:
    """The version ``initialize`` answers with: the client's when we support it, else our newest."""
    if isinstance(requested, str) and requested in HANDSHAKE_VERSIONS:
        return requested
    return HANDSHAKE_VERSIONS[0]


def result(req_id: str | int | None, payload: dict, *, stateless: bool,
           server_info: dict) -> dict:
    body = dict(payload)
    if stateless:
        body["resultType"] = "complete"
        meta = dict(body.get("_meta") or {})
        meta[META_SERVER_INFO] = server_info
        body["_meta"] = meta
    return {"jsonrpc": "2.0", "id": req_id, "result": body}


def error(req_id: str | int | None, code: int, message: str, data: object = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}
