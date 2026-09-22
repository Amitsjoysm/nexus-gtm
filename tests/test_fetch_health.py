"""An unreachable fetcher must be visible, because nothing else reports it.

When it cannot answer, signals silently fall back to the paid provider — the bill goes up and the
console says all clear. That is exactly what the email-verifier health row exists to prevent.
"""
from __future__ import annotations

import logging

import httpx
import pytest

from nexus.fetching.client import FetchClient, set_fetch_client
from nexus.fetching.health import check_fetch_service, warn_if_fetcher_unreachable


@pytest.fixture(autouse=True)
def _reset_client():
    yield
    set_fetch_client(None)


def _install(handler):
    set_fetch_client(FetchClient(
        base_url="http://fetch.test", token="t",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://fetch.test"),
    ))


async def test_unconfigured_is_reported_as_unconfigured():
    set_fetch_client(FetchClient(base_url="", token=""))
    check = await check_fetch_service()
    assert check.status == "unconfigured"
    assert "paid provider" in check.detail


async def test_a_healthy_service_is_ok_and_says_which_tier_it_has():
    _install(lambda request: httpx.Response(200, json={"ok": True, "browser": True}))
    check = await check_fetch_service()
    assert check.status == "ok"
    assert "browser" in check.detail


async def test_an_unreachable_service_is_an_error():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    _install(handler)
    check = await check_fetch_service()
    assert check.status == "error"
    assert "unreachable" in check.detail


async def test_a_wrong_token_is_an_error_naming_the_status():
    _install(lambda request: httpx.Response(401, json={"detail": "bad or missing token"}))
    check = await check_fetch_service()
    assert check.status == "error"
    assert "401" in check.detail


async def test_the_platform_health_probe_reports_it():
    from nexus.api.routers.admin_health import _PROBES

    assert "web fetcher" in {name for name, _ in _PROBES}


async def test_the_boot_warning_names_the_problem(caplog):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    _install(handler)
    with caplog.at_level(logging.WARNING):
        await warn_if_fetcher_unreachable(logging.getLogger("test.boot"))
    assert "WEB FETCHER UNREACHABLE" in caplog.text


async def test_the_boot_warning_is_quiet_when_the_fetcher_is_not_configured(caplog):
    set_fetch_client(FetchClient(base_url="", token=""))
    with caplog.at_level(logging.WARNING):
        await warn_if_fetcher_unreachable(logging.getLogger("test.boot"))
    assert "UNREACHABLE" not in caplog.text
