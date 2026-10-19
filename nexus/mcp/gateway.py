# nexus/mcp/gateway.py
"""Runs a tool's work through NEXUS's own ``/api``, as the connected user, inside this process.

Why a request rather than a function call: an ``/api`` endpoint already does everything a tool
must do — the role check, the tenant binding, the balance preflight and the charge, validation, and
the message a person reads when something is refused. Calling the service functions directly would
mean repeating those in every tool, and the first thing to drift would be a credit check. Here the
tool's request IS an app request, so they cannot drift.

The token minted for it carries the actor's LIVE role (read by ``credentials.resolve``) and current
token version, lives five minutes, and never leaves the process.

Only the (method, path) pairs in ``_ALLOWED`` can be reached, checked before anything is sent, so a
tool cannot be talked into ``DELETE`` or into an admin path whatever its arguments say.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta

import httpx
from jose import jwt

from nexus.mcp.credentials import Actor
from nexus.mcp.brand import PRODUCT_NAME

logger = logging.getLogger("nexus.mcp.gateway")

_ID = r"[A-Za-z0-9_-]{1,64}"
_ALLOWED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(f"^{pattern}$")) for method, pattern in (
        ("GET", "/api/accounts"),
        ("POST", "/api/accounts"),
        ("GET", f"/api/accounts/{_ID}"),
        ("GET", f"/api/accounts/{_ID}/contacts"),
        ("POST", f"/api/accounts/{_ID}/source-contacts"),
        ("POST", f"/api/accounts/{_ID}/lookalikes"),
        ("POST", f"/api/accounts/contacts/{_ID}/enrich"),
        ("GET", "/api/contacts"),
        ("GET", f"/api/contacts/{_ID}"),
        ("POST", f"/api/contacts/{_ID}/save-draft"),
        ("GET", "/api/signals"),
        ("GET", "/api/inbox"),
        ("POST", f"/api/inbox/{_ID}/complete"),
        ("GET", "/api/lists"),
        ("POST", "/api/lists"),
        ("GET", f"/api/lists/{_ID}"),
        ("GET", f"/api/lists/{_ID}/members"),
        ("POST", f"/api/lists/{_ID}/members"),
        ("GET", "/api/engagement/campaigns"),
        ("POST", f"/api/engagement/campaigns/{_ID}/contacts"),
        ("GET", "/api/billing/credits"),
        ("POST", "/api/agents/research/run"),
        ("POST", "/api/agents/messaging/run"),
        # Prospector: search masked (read), save charged per field (write).
        ("POST", "/api/prospector/search"),
        ("POST", "/api/prospector/deliver"),
    )
)

_TIMEOUT_S = 150.0
_TOKEN_TTL = timedelta(minutes=5)


def path_allowed(method: str, path: str) -> bool:
    return any(m == method.upper() and p.match(path) for m, p in _ALLOWED)


@dataclass
class ToolOutcome:
    ok: bool
    data: object = None
    message: str = ""
    status: int = 200


def _internal_token(actor: Actor) -> str:
    """An access token for the actor, five minutes long. Built like
    ``core.security.create_access_token`` (same claims, same key) with a shorter life."""
    from nexus.core.config import get_settings
    from nexus.core.db import utcnow

    settings = get_settings()
    now = utcnow()
    payload = {
        "sub": actor.user_id, "tid": actor.tenant_id, "role": actor.role,
        "tv": int(actor.token_version),
        "iat": int(now.timestamp()), "exp": int((now + _TOKEN_TTL).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def _detail(body: object) -> str:
    if isinstance(body, dict):
        detail = body.get("detail", body.get("message", ""))
        if isinstance(detail, list):  # FastAPI validation errors
            parts = []
            for err in detail[:5]:
                if isinstance(err, dict):
                    loc = ".".join(str(x) for x in err.get("loc", []) if x not in ("body", "query"))
                    parts.append(f"{loc}: {err.get('msg', '')}".strip(": "))
            return "; ".join(p for p in parts if p) or "The request was not valid."
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("detail") or detail)
        return str(detail or "")
    return str(body or "")


def _payment_message(body: object) -> str:
    payload = body.get("detail") if isinstance(body, dict) and isinstance(body.get("detail"),
                                                                          dict) else body
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("switch_state"):
        return (f"This is switched off for every workspace right now: "
                f"{payload.get('switch_message') or 'try again later.'}")
    capability = payload.get("capability") or "this action"
    reason = payload.get("reason") or ""
    if reason == "credits_exhausted" or "credit" in _detail(body).lower():
        return (f"Not enough credits for {capability}. Add credits in {PRODUCT_NAME} > Billing, or ask an "
                "admin to change the plan. Nothing was charged.")
    return (f"The workspace's plan does not include {capability}. An admin can change the plan in "
            f"{PRODUCT_NAME} > Billing. Nothing was charged.")


class Gateway:
    def __init__(self, app, actor: Actor) -> None:
        self._app = app
        self._actor = actor

    async def get(self, path: str, params: dict | None = None) -> ToolOutcome:
        return await self._send("GET", path, params=params)

    async def post(self, path: str, json: dict | None = None, params: dict | None = None,
                   idempotency_key: str | None = None) -> ToolOutcome:
        return await self._send("POST", path, json=json, params=params,
                                idempotency_key=idempotency_key)

    async def _send(self, method: str, path: str, *, json: dict | None = None,
                    params: dict | None = None, idempotency_key: str | None = None) -> ToolOutcome:
        if not path_allowed(method, path):
            raise ValueError(f"{method} {path} is not reachable from a tool")
        request_id = uuid.uuid4().hex
        headers = {
            "Authorization": f"Bearer {_internal_token(self._actor)}",
            "X-Request-ID": request_id,
            "User-Agent": "nexus-mcp",
        }
        if idempotency_key:
            # Scoped to the connection, so two apps choosing the same id cannot replay each other.
            headers["Idempotency-Key"] = f"mcp:{self._actor.connection_id}:{idempotency_key}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        transport = httpx.ASGITransport(app=self._app)
        async with httpx.AsyncClient(transport=transport, base_url="http://nexus.internal",
                                     timeout=_TIMEOUT_S) as client:
            try:
                resp = await client.request(method, path, json=json, params=clean_params,
                                            headers=headers)
            except Exception:
                logger.exception("tool request %s %s failed", method, path)
                return ToolOutcome(False, None, f"{PRODUCT_NAME} could not finish this. Reference: "
                                   f"{request_id}.", 500)
        try:
            body = resp.json()
        except ValueError:
            body = {"detail": resp.text[:500]}
        status = resp.status_code
        if 200 <= status < 300:
            return ToolOutcome(True, body, "", status)
        if status == 402:
            return ToolOutcome(False, body, _payment_message(body), status)
        if status == 429:
            retry = resp.headers.get("Retry-After", "")
            wait = f" in {retry} seconds" if retry else " shortly"
            return ToolOutcome(False, body, f"Too many requests; try again{wait}.", status)
        if status >= 500:
            logger.warning("tool request %s %s answered %s (request %s)", method, path, status,
                           request_id)
            # A 502/503/504 is the app saying a provider it depends on failed, in a sentence it
            # wrote ("The data provider failed... Try again shortly"), which tells the assistant
            # what to say. A 500 is a crash whose body nothing here wrote: never pass it on.
            said = _detail(body).strip() if status in (502, 503, 504) else ""
            lead = said if said else f"{PRODUCT_NAME} could not finish this."
            return ToolOutcome(False, body, f"{lead} Reference: {request_id}.", status)
        message = _detail(body) or {
            400: "The request was not valid.", 403: "You do not have permission to do this.",
            404: "Not found.", 409: "This conflicts with the current state.",
        }.get(status, f"The request was refused ({status}).")
        if status == 404 and "not found" not in message.lower():
            message = f"Not found: {message}"
        return ToolOutcome(False, body, message, status)
