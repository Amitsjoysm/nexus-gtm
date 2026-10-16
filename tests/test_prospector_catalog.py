"""Prospector's place in the catalogs: the provider key, billing, rate cards and runtime settings.

The InfoJoy Search API is a pooled credential like Exa or Apify: a superadmin stores it in Provider
keys (sealed, never returned) and it falls back to `NEXUS_INFOJOY_SEARCH_API_KEYS`. The token travels
as the `X-Api-Token` header and never in a URL, because a URL is copied into every log line and
exception that carries it.
"""
from __future__ import annotations

import json

import httpx

from nexus.core.config import get_settings


# ---- the provider key ---------------------------------------------------------------------------

def test_infojoy_search_is_a_manageable_provider(monkeypatch):
    from nexus.providers.catalog import PROVIDERS, env_pool

    assert "infojoy_search" in PROVIDERS
    monkeypatch.setattr(get_settings(), "infojoy_search_api_key", "")
    monkeypatch.setattr(get_settings(), "infojoy_search_api_keys", "k-one, k-two")
    assert env_pool("infojoy_search") == ["k-one", "k-two"]


def test_the_base_url_is_https_and_not_a_request_parameter():
    url = get_settings().infojoy_search_base_url
    assert url.startswith("https://")
    assert not url.endswith("/")


async def test_probe_sends_the_token_as_a_header_never_in_the_url():
    from nexus.providers.testing import probe

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"PageNumber": 1, "PageSize": 1, "Items": []})

    result = await probe("infojoy_search", "tok-secret-123", transport=httpx.MockTransport(handler))

    assert result.ok and result.status == "probe_ok"
    request = seen[0]
    assert request.headers["X-Api-Token"] == "tok-secret-123"
    assert "tok-secret-123" not in str(request.url)
    assert request.url.path == "/api/search/contacts"
    body = json.loads(request.content)
    assert body["PageSize"] == 1 and body["IncludeTotalCount"] is False
    assert isinstance(body["Filters"], dict)


async def test_probe_reports_a_rejected_token():
    from nexus.providers.testing import probe

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid token"})

    result = await probe("infojoy_search", "bad", transport=httpx.MockTransport(handler))

    assert not result.ok and result.status == "failed" and result.http_status == 401


# ---- billing --------------------------------------------------------------------------------------

def test_the_module_gate_is_enabled_and_the_daily_limit_hangs_off_it():
    from nexus.billing.catalog import CAPABILITY_SEED
    from nexus.billing.rates import UNPRICED_BY_DESIGN

    caps = {c["id"]: c for c in CAPABILITY_SEED}
    assert caps["module.prospector"]["default_mode"] == "enabled"
    daily = caps["prospector.daily_rows"]
    assert daily["depends_on"] == ["module.prospector"] and daily["unit"] == "row"
    # A limit holder: its plan quota is the budget (D22), and a quota needs no price.
    assert "prospector.daily_rows" in UNPRICED_BY_DESIGN


def test_the_module_gate_changes_nothing_for_existing_plans():
    from nexus.billing.plans import PLAN_SEED

    for plan in PLAN_SEED:
        listed = {e[0] for e in plan.get("entitlements", [])}
        assert not any(c.startswith("prospector.") or c == "module.prospector" for c in listed), \
            plan["id"]


# ---- tables ---------------------------------------------------------------------------------------

def test_the_tables_are_split_between_platform_and_tenant():
    from nexus.models.prospector import ProspectorDailyRows, ProspectorPage, ProspectorPurchase

    assert "tenant_id" not in ProspectorPage.__table__.columns     # shared vendor cache
    assert "tenant_id" in ProspectorPurchase.__table__.columns     # RLS-enrolled
    assert "tenant_id" in ProspectorDailyRows.__table__.columns    # RLS-enrolled


async def test_verify_upgrades_a_working_search_to_verified():
    from nexus.providers.testing import verify

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"PageNumber": 1, "PageSize": 1, "Items": []})

    result = await verify("infojoy_search", "tok", transport=httpx.MockTransport(handler))

    assert result.ok and result.status == "verified"
