# tests/test_admin_health.py
"""The platform health console.

`/health` says the process is up; `/ready` says the database answers. Neither tells an operator
WHICH part is broken, and a deployment with no Stripe webhook, an unapproved Apify actor and a stub
LLM reports "ok" on both.

The two properties worth protecting here are honesty properties, not uptime ones:

* a route that was never probed must never look like a passing one, and
* the console must never be the most destructive thing in the system.
"""
from __future__ import annotations

from tests.conftest import auth, signup

ENDPOINT = "/api/admin/health/endpoints"


async def _admin(client, monkeypatch, slug: str):
    from nexus.billing.catalog import sync_catalog
    from nexus.core.config import get_settings

    await sync_catalog()
    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@nexus.com")
    return await signup(client, slug=slug, email="boss@nexus.com", company=slug.upper())


# ---- the gate ----------------------------------------------------------------------------------

async def test_a_workspace_owner_cannot_see_it(client):
    """Platform admin is a separate authorization system; no tenant role grants it."""
    token = await signup(client, slug="ah1", email="o@ah1.com", company="AH1")
    r = await client.get(ENDPOINT, headers=auth(token))
    assert r.status_code in (401, 404)


async def test_it_requires_authentication(client):
    assert (await client.get(ENDPOINT)).status_code in (401, 404)


def test_support_can_read_health_but_not_reprice():
    """'Is the platform up?' is the first question on every ticket. Making support escalate to
    find out turns a 10-second answer into a queue — but it must not smuggle in billing power."""
    from nexus.billing.permissions import PRICING_WRITE, ROLE_PRESETS, SYSTEM_READ

    assert SYSTEM_READ in ROLE_PRESETS["support"]
    assert PRICING_WRITE not in ROLE_PRESETS["support"]


# ---- what it reports ---------------------------------------------------------------------------

async def test_it_inventories_every_route_and_probes_dependencies(client, monkeypatch):
    token = await _admin(client, monkeypatch, "ah2")
    r = await client.get(ENDPOINT, headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["summary"]["routes_total"] > 100
    assert {d["name"] for d in body["dependencies"]} >= {
        "database", "queue", "payments (stripe)", "apify", "llm", "billing enforcement",
    }
    # The database is genuinely reachable in the suite, so this is a real probe result.
    db = next(d for d in body["dependencies"] if d["name"] == "database")
    assert db["status"] == "ok"
    assert db["latency_ms"] is not None


async def test_a_mutating_route_is_never_called(client, monkeypatch):
    """The console must not create campaigns, charge cards or delete records to prove they work."""
    token = await _admin(client, monkeypatch, "ah3")
    body = (await client.get(ENDPOINT, headers=auth(token))).json()

    mutating = [r for r in body["routes"] if r["method"] in ("POST", "PUT", "PATCH", "DELETE")]
    assert mutating, "expected mutating routes in the inventory"
    for route in mutating:
        assert route["status"] == "not_probed", f"{route['method']} {route['path']} was called!"
        assert route["reason"], "a skipped route must say why"
        assert route["http_status"] is None


async def test_a_route_needing_a_path_parameter_is_not_probed(client, monkeypatch):
    """There is no safe id to invent, and guessing one either 404s (meaningless) or hits a real
    record (worse)."""
    token = await _admin(client, monkeypatch, "ah4")
    body = (await client.get(ENDPOINT, headers=auth(token))).json()

    parametrised = [r for r in body["routes"] if "{" in r["path"] and r["method"] == "GET"]
    assert parametrised
    assert all(r["status"] == "not_probed" for r in parametrised)
    assert all("path parameter" in r["reason"] for r in parametrised)


async def test_unprobed_routes_are_never_reported_as_passing(client, monkeypatch):
    """THE honesty property. A green tick that was never tested is worse than no tick."""
    token = await _admin(client, monkeypatch, "ah5")
    body = (await client.get(ENDPOINT, headers=auth(token))).json()

    assert body["summary"]["routes_not_probed"] > 0
    for route in body["routes"]:
        assert route["status"] in ("ok", "error", "not_probed")
        if route["status"] == "ok":
            assert route["http_status"] is not None, f"{route['path']} 'ok' without being called"


async def test_an_auth_gate_firing_is_not_a_failure(client, monkeypatch):
    """Probes are unauthenticated, so protected routes answer 401/403. Counting that as broken
    would paint the whole console red and make it useless."""
    token = await _admin(client, monkeypatch, "ah6")
    body = (await client.get(ENDPOINT, headers=auth(token))).json()

    gated = [r for r in body["routes"]
             if r["http_status"] in (401, 403) and r["status"] != "not_probed"]
    assert gated, "expected some probed routes to be auth-gated"
    assert all(r["status"] == "ok" for r in gated)


async def test_health_and_ready_are_actually_probed(client, monkeypatch):
    """If nothing at all were probed the console would be an inventory pretending to be a check."""
    token = await _admin(client, monkeypatch, "ah7")
    body = (await client.get(ENDPOINT, headers=auth(token))).json()

    by_path = {(r["method"], r["path"]): r for r in body["routes"]}
    for path in ("/health", "/ready"):
        route = by_path.get(("GET", path))
        if route is not None:                       # not an APIRoute in every build
            assert route["status"] == "ok"
            assert route["http_status"] == 200


async def test_auth_level_is_read_from_the_route_not_guessed(client, monkeypatch):
    token = await _admin(client, monkeypatch, "ah8")
    body = (await client.get(ENDPOINT, headers=auth(token))).json()
    levels = {r["auth"] for r in body["routes"]}
    assert levels <= {"public", "authenticated", "platform-admin"}
    assert "platform-admin" in levels


# ---- the settings operators most often misread --------------------------------------------------

async def test_shadow_enforcement_is_surfaced_as_degraded(client, monkeypatch):
    """"Plan changes do nothing" is the most reported billing symptom, and shadow mode is the
    cause. The console has to say so rather than reporting billing as healthy."""
    token = await _admin(client, monkeypatch, "ah9")
    body = (await client.get(ENDPOINT, headers=auth(token))).json()

    enforcement = next(d for d in body["dependencies"] if d["name"] == "billing enforcement")
    assert enforcement["status"] == "degraded"
    assert "shadow" in enforcement["detail"]


async def test_an_unconfigured_dependency_is_not_an_error(client, monkeypatch):
    """"No Stripe key" and "Stripe rejected our key" send an operator to different places."""
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "payment_provider", "noop")
    token = await _admin(client, monkeypatch, "ah10")
    body = (await client.get(ENDPOINT, headers=auth(token))).json()

    payments = next(d for d in body["dependencies"] if d["name"] == "payments (stripe)")
    assert payments["status"] in ("unconfigured", "degraded")
    assert payments["status"] != "error"


async def test_a_failing_probe_does_not_blank_the_console(client, monkeypatch):
    """One broken dependency must not take down the page that exists to diagnose it."""
    import nexus.api.routers.admin_health as mod

    async def _boom():
        raise RuntimeError("dependency exploded")

    monkeypatch.setattr(mod, "_PROBES", (("database", _boom), ("llm", mod._probe_llm)))
    token = await _admin(client, monkeypatch, "ah11")
    r = await client.get(ENDPOINT, headers=auth(token))

    assert r.status_code == 200
    body = r.json()
    db = next(d for d in body["dependencies"] if d["name"] == "database")
    assert db["status"] == "error"
    assert "dependency exploded" in db["detail"]
    assert body["overall"] == "error"
    assert body["routes"], "route inventory must survive a dependency failure"
