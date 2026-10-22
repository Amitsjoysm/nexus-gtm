"""Billing failure points an operator could not see (2026-10-22).

Traced while asking why "Stripe is not able to charge" and why "superadmin cannot create, edit or
delete plans":

* Platform health passed the payments row whenever Stripe held ANY webhook endpoint, so one pointing
  at an old domain, one disabled at Stripe, or one without the subscription events read healthy
  while every payment stayed at Stripe and no workspace moved.
* With no webhook signing secret in force every delivery is refused, and the only trace was a log
  line.
* A console opened from an address the IP allowlist refuses showed a working page whose every tab
  failed on its own with a 403.
"""
from __future__ import annotations

from tests.conftest import auth, signup

HANDLED = ("checkout.session.completed", "customer.subscription.updated", "invoice.paid")
URL = "https://gtm.example.com/api/billing/webhooks/stripe"


def _endpoint(url=URL, status="enabled", events=HANDLED):
    return {"url": url, "status": status, "enabled_events": list(events)}


def test_an_enabled_endpoint_at_this_app_with_every_event_is_healthy():
    from nexus.api.routers.admin_health import webhook_endpoint_problem

    assert webhook_endpoint_problem([_endpoint()], base_url="https://gtm.example.com",
                                    handled=HANDLED) == ""
    assert webhook_endpoint_problem([_endpoint(events=["*"])], base_url="https://gtm.example.com/",
                                    handled=HANDLED) == ""


def test_an_endpoint_elsewhere_disabled_or_short_of_events_is_named():
    from nexus.api.routers.admin_health import webhook_endpoint_problem

    elsewhere = webhook_endpoint_problem(
        [_endpoint(url="https://old.example.com/api/billing/webhooks/stripe")],
        base_url="https://gtm.example.com", handled=HANDLED)
    assert "NO WEBHOOK ENDPOINT POINTS AT THIS APP" in elsewhere and "old.example.com" in elsewhere
    assert "DISABLED" in webhook_endpoint_problem([_endpoint(status="disabled")],
                                                  base_url="https://gtm.example.com", handled=HANDLED)
    short = webhook_endpoint_problem([_endpoint(events=["checkout.session.completed"])],
                                     base_url="https://gtm.example.com", handled=HANDLED)
    assert "invoice.paid" in short and "customer.subscription.updated" in short
    assert "NO WEBHOOK" in webhook_endpoint_problem([], base_url="", handled=HANDLED)


def test_without_a_public_address_the_path_is_what_is_matched():
    from nexus.api.routers.admin_health import webhook_endpoint_problem

    assert webhook_endpoint_problem([_endpoint(url="https://any-host.example/api/billing/webhooks/stripe")],
                                    base_url="", handled=HANDLED) == ""


async def test_a_missing_signing_secret_is_reported(monkeypatch):
    from nexus.api.routers import admin_health
    from nexus.billing import credentials

    async def none_set():
        return ("sk_test_x", "", "")

    async def no_row(provider="stripe"):
        return None

    monkeypatch.setattr(credentials, "resolve_stripe_secrets", none_set)
    monkeypatch.setattr(credentials, "active_credential", no_row)
    assert "NEXUS_STRIPE_WEBHOOK_SECRET" in await admin_health.signing_secret_problem()

    async def a_row(provider="stripe"):
        return object()

    monkeypatch.setattr(credentials, "active_credential", a_row)
    assert "Create webhook in Stripe" in await admin_health.signing_secret_problem()

    async def set_one():
        return ("sk_test_x", "whsec_x", "")

    monkeypatch.setattr(credentials, "resolve_stripe_secrets", set_one)
    assert await admin_health.signing_secret_problem() == ""


async def test_whoami_says_when_the_allowlist_refuses_this_address(client, monkeypatch):
    from nexus.api import deps_ip
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_admin_emails", "boss@origin.io")
    token = await signup(client, slug="origin", email="boss@origin.io", company="ORIGIN")

    open_ = (await client.get("/api/admin/billing/whoami", headers=auth(token))).json()
    assert open_["origin_allowed"] is True

    monkeypatch.setattr(deps_ip, "_nets", deps_ip.parse_allowlist("10.9.8.7/32"))
    monkeypatch.setattr(deps_ip, "_raw", "10.9.8.7/32")
    refused = await client.get("/api/admin/billing/whoami",
                               headers={**auth(token), "X-Forwarded-For": "203.0.113.9"})
    assert refused.status_code == 200
    assert refused.json()["origin_allowed"] is False and refused.json()["origin_seen"] == "203.0.113.9"
    # ... and the console's own requests are exactly what it says.
    plans = await client.get("/api/admin/billing/plans",
                             headers={**auth(token), "X-Forwarded-For": "203.0.113.9"})
    assert plans.status_code == 403


async def test_a_non_admin_learns_nothing_about_the_allowlist(client):
    token = await signup(client, slug="plain", email="rep@plain.io", company="PLAIN")
    body = (await client.get("/api/admin/billing/whoami", headers=auth(token))).json()
    assert body["is_platform_admin"] is False and body["origin_seen"] == ""


def test_the_console_names_the_allowlist_and_the_missing_permission():
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1] / "frontend/src/pages/AdminBillingPage.tsx"
            ).read_text(encoding="utf-8")
    assert "origin_allowed === false" in page and "IP allowlist" in page
    assert "needs the Stripe permission" in page
