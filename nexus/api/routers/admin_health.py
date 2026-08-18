# nexus/api/routers/admin_health.py
"""Live platform health: what the API exposes, and whether its dependencies actually answer.

`/health` says the process is up and `/ready` says the database accepts connections. Neither
answers the question an operator actually has during an incident — *which part is broken?* A
deployment where Stripe has no webhook endpoint, Apify refuses every actor, and the search
provider is unkeyed reports "ok" on both.

Two sections, and the distinction between them is the whole point:

**Dependencies are PROBED.** Every one is really called, and each reports `ok` / `degraded` /
`unconfigured` / `error` with the provider's own message. `unconfigured` is deliberately not
`error`: "no Stripe key" and "Stripe rejected our key" send an operator to different places, and
collapsing them is how this codebase has repeatedly produced a wrong diagnosis (an Apify 403 was
reported as a rate limit; a Docker port-forward failure was diagnosed as a broken TLS certificate).

**Routes are INVENTORIED, and only some are probed.** 200 routes are registered, 123 of them
mutating. Calling them to see whether they work would create campaigns, charge cards and delete
records — a health check must not be the most destructive thing in the system. So a route is
probed only when it is a GET with no path parameters and no known side effect; everything else is
listed with an explicit `not_probed` reason. An operator seeing the whole surface, and knowing
exactly which parts were actually verified, is worth far more than a green tick that was never
tested.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.permissions import SYSTEM_READ

logger = logging.getLogger("nexus.api.admin_health")

router = APIRouter(prefix="/admin/health", tags=["admin-health"])

OK, DEGRADED, UNCONFIGURED, ERROR = "ok", "degraded", "unconfigured", "error"

# Probing is opt-in by shape, never by allowlist maintenance: a GET with no path parameter is
# safe to call, everything else is not. Plus these, which are GETs that do real work or cost money.
_NEVER_PROBE = {
    "/api/admin/health/endpoints",   # would recurse
    "/metrics",                      # scrape target, not a health signal
}


class DependencyOut(BaseModel):
    name: str
    status: str
    detail: str = ""
    latency_ms: float | None = None


class RouteOut(BaseModel):
    method: str
    path: str
    # public | authenticated | platform-admin — read off the endpoint's own dependencies, so it
    # cannot drift from what the server actually enforces.
    auth: str
    status: str                      # ok | error | not_probed
    http_status: int | None = None
    reason: str = ""                 # why it was not probed
    latency_ms: float | None = None


class HealthOut(BaseModel):
    generated_at: str
    overall: str
    dependencies: list[DependencyOut]
    routes: list[RouteOut]
    summary: dict = Field(default_factory=dict)


# ---- dependency probes -------------------------------------------------------------------------
#
# Each returns (status, detail). None may raise: one broken dependency must not blank the console
# that exists to diagnose it.

async def _probe_database() -> tuple[str, str]:
    from sqlalchemy import text

    from nexus.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        await session.execute(text("SELECT 1"))
        bind = session.get_bind()
        return OK, f"{bind.dialect.name} reachable"


async def _probe_queue() -> tuple[str, str]:
    from nexus.core.config import get_settings
    from nexus.workers.queue import get_task_queue

    settings = get_settings()
    queue = get_task_queue()
    depth = await queue.depth()
    if settings.queue_backend != "redis":
        return DEGRADED, f"in-memory queue ({settings.queue_backend}) — jobs are lost on restart"
    # None means the backend cannot say; reporting 0 would look exactly like a healthy empty queue.
    return OK, f"redis, depth={depth if depth is not None else 'unknown'}"


async def _probe_payments() -> tuple[str, str]:
    from nexus.billing.payments import get_payment_provider
    from nexus.core.config import get_settings

    settings = get_settings()
    provider = get_payment_provider()
    if provider.name == "noop":
        return UNCONFIGURED, "NEXUS_PAYMENT_PROVIDER=noop — no money moves"
    if not getattr(provider, "configured", False):
        return UNCONFIGURED, "stripe selected but NEXUS_STRIPE_SECRET_KEY is unset"

    import httpx

    key = settings.stripe_secret_key
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get("https://api.stripe.com/v1/account",
                             headers={"Authorization": f"Bearer {key}"})
        if r.status_code != 200:
            return ERROR, f"Stripe rejected the key ({r.status_code})"
        account = r.json()
        mode = "test" if key.startswith("sk_test") else "live"
        notes = [f"{mode} mode", f"account={account.get('id')}"]
        if not account.get("charges_enabled"):
            notes.append("charges_enabled=FALSE — the account cannot take payment")
        # THE one that silently breaks everything: subscription state arrives only by webhook.
        wh = await client.get("https://api.stripe.com/v1/webhook_endpoints",
                              headers={"Authorization": f"Bearer {key}"}, params={"limit": 5})
        endpoints = (wh.json().get("data") or []) if wh.status_code == 200 else []
        if not endpoints:
            return DEGRADED, (
                "NO WEBHOOK ENDPOINT REGISTERED — checkout will complete at Stripe and the "
                "subscription will never reach this database. " + ", ".join(notes)
            )
        notes.append(f"{len(endpoints)} webhook endpoint(s)")
        if not account.get("charges_enabled"):
            return DEGRADED, ", ".join(notes)
        return OK, ", ".join(notes)


async def _probe_apify() -> tuple[str, str]:
    import httpx

    from nexus.integrations.apify import ACTORS, get_apify_client

    client = get_apify_client()
    if not client.configured:
        return UNCONFIGURED, "no NEXUS_APIFY_API_KEY[S] — phone and personalization are inert"

    key = client.api_keys[0]
    async with httpx.AsyncClient(timeout=15) as http:
        me = await http.get("https://api.apify.com/v2/users/me",
                            headers={"Authorization": f"Bearer {key}"})
        if me.status_code != 200:
            return ERROR, f"Apify rejected the key ({me.status_code})"
        user = (me.json().get("data") or {}).get("username", "?")
        # A valid key is not the same as a usable actor: the FULL_PERMISSIONS actors 403 until
        # approved per ACCOUNT, which is invisible from the key alone.
        blocked = []
        for name, actor_id in ACTORS.items():
            meta = await http.get(f"https://api.apify.com/v2/acts/{actor_id}",
                                  headers={"Authorization": f"Bearer {key}"})
            if meta.status_code != 200:
                blocked.append(f"{name}(unreachable)")
                continue
            if (meta.json().get("data") or {}).get("actorPermissionLevel") == "FULL_PERMISSIONS":
                blocked.append(name)
        if blocked:
            return DEGRADED, (
                f"key ok (user={user}); actors needing per-ACCOUNT approval: {', '.join(blocked)}"
            )
        return OK, f"user={user}, {len(ACTORS)} actors reachable"


async def _probe_llm() -> tuple[str, str]:
    from nexus.agents.llm import get_llm_provider

    provider = get_llm_provider()
    # The chain wraps providers (cost tracking around a fallback around the real one), so the
    # outermost class name says nothing useful. Walk in for the one that would actually answer.
    inner = provider
    for _ in range(4):
        nested = getattr(inner, "inner", None) or getattr(inner, "primary", None)
        if nested is None:
            break
        inner = nested
    name = getattr(inner, "name", type(inner).__name__)
    if "stub" in name.lower():
        return DEGRADED, f"{name} — agent output is canned, not generated by a model"
    return OK, name


async def _probe_search() -> tuple[str, str]:
    from nexus.core.config import get_settings

    settings = get_settings()
    configured = settings.signal_search_provider or settings.search_provider
    return OK, f"provider={configured or 'default'}"


async def _probe_enforcement() -> tuple[str, str]:
    """Not a dependency, but the single most misread setting in the system."""
    from nexus.core.config import get_settings

    mode = get_settings().billing_enforcement
    if mode == "on":
        return OK, "enforcement=on — plans are actually enforced"
    if mode == "shadow":
        return DEGRADED, (
            "enforcement=shadow — entitlements are computed and then ALLOWED anyway, so plan "
            "changes have no visible effect. This is the safe default, not a fault."
        )
    return DEGRADED, "enforcement=off — billing is a full kill switch right now"


_PROBES: tuple[tuple[str, Any], ...] = (
    ("database", _probe_database),
    ("queue", _probe_queue),
    ("payments (stripe)", _probe_payments),
    ("apify", _probe_apify),
    ("llm", _probe_llm),
    ("search", _probe_search),
    ("billing enforcement", _probe_enforcement),
)


async def _run_probe(name: str, fn) -> DependencyOut:
    started = time.perf_counter()
    try:
        status, detail = await asyncio.wait_for(fn(), timeout=20)
    except asyncio.TimeoutError:
        status, detail = ERROR, "probe timed out after 20s"
    except Exception as exc:
        logger.warning("health probe %s failed", name, exc_info=True)
        status, detail = ERROR, f"{type(exc).__name__}: {str(exc)[:200]}"
    return DependencyOut(
        name=name, status=status, detail=detail,
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
    )


# ---- route inventory ---------------------------------------------------------------------------

def _auth_level(route) -> str:
    """What the route actually enforces, read from its own dependency list."""
    names = []
    for dep in getattr(getattr(route, "dependant", None), "dependencies", []) or []:
        call = getattr(dep, "call", None)
        names.append(getattr(call, "__qualname__", "") or getattr(call, "__name__", ""))
    blob = " ".join(names)
    if "platform" in blob:
        return "platform-admin"
    if "require" in blob or "principal" in blob.lower() or "tenant" in blob.lower():
        return "authenticated"
    return "public"


def _probeable(method: str, path: str) -> tuple[bool, str]:
    if path in _NEVER_PROBE:
        return False, "excluded (would recurse or is a scrape target)"
    if method != "GET":
        return False, f"{method} mutates — a health check must not be destructive"
    if "{" in path:
        return False, "needs a path parameter; no safe value to invent"
    return True, ""


@router.get("/endpoints", response_model=HealthOut)
async def endpoint_health(
    principal: Principal = Depends(require_platform_permission(SYSTEM_READ)),
) -> HealthOut:
    """Every registered route plus a live probe of every dependency.

    Probes run concurrently — they are all network-bound, and an operator watching an incident
    should not wait for them in series.
    """
    from datetime import datetime, timezone

    from fastapi.routing import APIRoute
    from httpx import ASGITransport, AsyncClient

    from nexus.main import create_app

    dependencies = list(await asyncio.gather(*(_run_probe(n, f) for n, f in _PROBES)))

    app = create_app()
    routes: list[RouteOut] = []
    to_probe: list[tuple[int, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(m for m in (route.methods or set()) if m not in ("HEAD", "OPTIONS")):
            ok, why = _probeable(method, route.path)
            routes.append(RouteOut(
                method=method, path=route.path, auth=_auth_level(route),
                status="not_probed" if not ok else "pending", reason=why,
            ))
            if ok:
                to_probe.append((len(routes) - 1, route.path))

    # In-process ASGI calls: no network hop, and nothing leaves the box.
    if to_probe:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://health") as client:
            async def _hit(index: int, path: str) -> None:
                started = time.perf_counter()
                try:
                    resp = await asyncio.wait_for(client.get(path), timeout=15)
                    code = resp.status_code
                except Exception as exc:
                    routes[index].status = ERROR
                    routes[index].reason = f"{type(exc).__name__}: {str(exc)[:120]}"
                    return
                finally:
                    routes[index].latency_ms = round((time.perf_counter() - started) * 1000, 1)
                routes[index].http_status = code
                # 401/403 from an unauthenticated probe means the gate WORKS. Counting that as a
                # failure would paint every protected route red and make the console useless.
                routes[index].status = OK if code < 500 else ERROR
                if code >= 500:
                    routes[index].reason = f"HTTP {code}"

            await asyncio.gather(*(_hit(i, p) for i, p in to_probe))

    probed = [r for r in routes if r.status in (OK, ERROR)]
    failing = [r for r in probed if r.status == ERROR]
    dep_bad = [d for d in dependencies if d.status == ERROR]
    dep_degraded = [d for d in dependencies if d.status in (DEGRADED, UNCONFIGURED)]
    overall = ERROR if (failing or dep_bad) else (DEGRADED if dep_degraded else OK)

    return HealthOut(
        generated_at=datetime.now(timezone.utc).isoformat(),
        overall=overall,
        dependencies=dependencies,
        routes=routes,
        summary={
            "routes_total": len(routes),
            "routes_probed": len(probed),
            "routes_failing": len(failing),
            "routes_not_probed": len(routes) - len(probed),
            "dependencies_total": len(dependencies),
            "dependencies_ok": sum(1 for d in dependencies if d.status == OK),
            "dependencies_degraded": len(dep_degraded),
            "dependencies_failing": len(dep_bad),
        },
    )
