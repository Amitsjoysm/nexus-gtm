# tests/test_admin_ip_allowlist.py
"""Restricting the Control plane to named origins.

The panel grants power over pricing, provider credentials and other people's workspaces, so origin
is worth checking on top of authentication: a stolen admin token is worth much less if it also has
to arrive from the right network.

Three properties keep this from becoming a lockout, and each has its own test:

* **Empty means open.** A default-closed allowlist would lock every existing deployment out of its
  own admin panel the moment it upgraded.
* **At most two entries.** A policy limit, not a technical one, so "just one more" is a decision
  someone makes rather than a list that grows until it means nothing.
* **A malformed list is ignored, not enforced.** The only way to fix a bad allowlist is through the
  panel it would have closed.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


@pytest.fixture(autouse=True)
def _reset_allowlist():
    """The allowlist is module-level state, so a test that sets it would leak into every test that
    runs after it — including ones in other files that expect an open panel."""
    from nexus.api import deps_ip

    deps_ip.set_allowlist("")
    yield
    deps_ip.set_allowlist("")


async def _admin(client, monkeypatch, *, slug: str, email: str) -> str:
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


# ---- parsing ---------------------------------------------------------------------------------

def test_more_than_two_entries_is_refused():
    """Three is where an allowlist stops being a restriction and starts being a list."""
    from nexus.api.deps_ip import parse_allowlist

    with pytest.raises(ValueError, match="at most 2"):
        parse_allowlist("1.1.1.1, 2.2.2.2, 3.3.3.3")


def test_a_malformed_entry_is_refused_rather_than_skipped():
    """Silently dropping an unparseable entry turns a typo into an open panel — the exact opposite
    of what the operator was trying to do."""
    from nexus.api.deps_ip import parse_allowlist

    with pytest.raises(ValueError, match="not an IP address"):
        parse_allowlist("192.168.1.1, not-an-ip")


def test_a_cidr_range_counts_as_one_entry():
    """An office network is one entry, not two hundred."""
    from nexus.api.deps_ip import ip_allowed, parse_allowlist

    nets = parse_allowlist("10.0.0.0/24")
    assert ip_allowed("10.0.0.7", nets) is True
    assert ip_allowed("10.0.1.7", nets) is False


def test_an_empty_list_admits_everyone():
    from nexus.api.deps_ip import ip_allowed, parse_allowlist

    assert parse_allowlist("") == []
    assert ip_allowed("8.8.8.8", []) is True


def test_an_unknown_origin_fails_a_non_empty_list():
    """No observable address, against a list that says address matters. An unknown origin must not
    pass a check whose entire purpose is knowing where the request came from."""
    from nexus.api.deps_ip import ip_allowed, parse_allowlist

    assert ip_allowed("", parse_allowlist("10.0.0.1")) is False
    assert ip_allowed("garbage", parse_allowlist("10.0.0.1")) is False


def test_a_bad_value_leaves_the_previous_list_installed():
    """Clearing on a parse failure would turn a typo into an open panel."""
    from nexus.api import deps_ip

    deps_ip.set_allowlist("10.0.0.0/24")
    with pytest.raises(ValueError):
        deps_ip.set_allowlist("nonsense")
    assert deps_ip.current_allowlist() == "10.0.0.0/24"


# ---- enforcement -----------------------------------------------------------------------------

async def test_an_empty_allowlist_lets_the_panel_through(client, monkeypatch):
    """The compatibility line: a default-closed allowlist would lock every existing deployment out
    of its own panel on upgrade."""
    token = await _admin(client, monkeypatch, slug="ip1", email="boss@ip1.com")
    assert (await client.get("/api/admin/billing/rates",
                             headers=auth(token))).status_code == 200


async def test_an_origin_outside_the_allowlist_is_refused(client, monkeypatch):
    from nexus.api import deps_ip

    token = await _admin(client, monkeypatch, slug="ip2", email="boss@ip2.com")
    deps_ip.set_allowlist("203.0.113.5")
    r = await client.get("/api/admin/billing/rates", headers=auth(token))
    assert r.status_code == 403
    # Says WHY, and names the address as we saw it. Behind a proxy that is frequently not the one
    # the operator expects, and without it they cannot fix their own lockout.
    assert "not permitted" in r.text.lower()


async def test_a_listed_origin_is_admitted(client, monkeypatch):
    from nexus.api import deps_ip

    token = await _admin(client, monkeypatch, slug="ip3", email="boss@ip3.com")
    # 0.0.0.0/0 stands in for "the address this test client presents", which differs by transport.
    deps_ip.set_allowlist("0.0.0.0/0")
    assert (await client.get("/api/admin/billing/rates",
                             headers=auth(token))).status_code == 200


async def test_the_tenant_api_is_unaffected(client, monkeypatch):
    """This gates the CONTROL PLANE only. A restriction that also locked customers out of the
    product would be an outage wearing a security label."""
    from nexus.api import deps_ip

    token = await signup(client, slug="ip7", email="o@ip7.com", company="IP7")
    deps_ip.set_allowlist("203.0.113.5")
    assert (await client.get("/api/accounts", headers=auth(token))).status_code == 200


async def test_the_forwarded_header_is_read_for_the_original_client():
    """Behind our own reverse proxy the socket address is the proxy, so the first X-Forwarded-For
    entry is the value to trust."""
    from nexus.api.deps_ip import client_ip_of

    class Req:
        headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}
        client = type("C", (), {"host": "10.0.0.1"})()

    assert client_ip_of(Req()) == "203.0.113.9"


# ---- through the panel -------------------------------------------------------------------------

async def test_the_allowlist_is_settable_from_the_runtime_panel(client, monkeypatch):
    """It is not a `Settings` field — pydantic refuses an attribute it has not declared — so it has
    its own storage and its own reader. This asserts that seam works end to end."""
    from nexus.api import deps_ip

    token = await _admin(client, monkeypatch, slug="ip8", email="boss@ip8.com")
    r = await client.put("/api/admin/runtime/settings/admin_ip_allowlist", headers=auth(token),
                         json={"value": "0.0.0.0/0", "note": "office network"})
    assert r.status_code == 200, r.text
    assert deps_ip.current_allowlist() == "0.0.0.0/0"

    rows = (await client.get("/api/admin/runtime/settings", headers=auth(token))).json()
    row = next(x for x in rows if x["key"] == "admin_ip_allowlist")
    assert row["value"] == "0.0.0.0/0"
    assert row["overridden"] is True
    assert row["in_effect"] is True


async def test_clearing_the_allowlist_actually_reopens_the_panel(client, monkeypatch):
    """A `Settings` field reverts on the next TTL sweep; an external sink has no environment value
    to fall back to, so clearing has to reset it explicitly. Leaving a stale list installed would
    keep the panel locked to an address the operator believes they just removed."""
    from nexus.api import deps_ip

    token = await _admin(client, monkeypatch, slug="ip9", email="boss@ip9.com")
    await client.put("/api/admin/runtime/settings/admin_ip_allowlist", headers=auth(token),
                     json={"value": "0.0.0.0/0"})
    await client.delete("/api/admin/runtime/settings/admin_ip_allowlist", headers=auth(token))
    assert deps_ip.current_allowlist() == ""


async def test_a_three_entry_list_is_refused_by_the_panel(client, monkeypatch):
    token = await _admin(client, monkeypatch, slug="ip10", email="boss@ip10.com")
    r = await client.put("/api/admin/runtime/settings/admin_ip_allowlist", headers=auth(token),
                         json={"value": "1.1.1.1,2.2.2.2,3.3.3.3"})
    assert r.status_code == 400
    assert "at most 2" in r.text


async def test_a_rejected_value_leaves_no_stored_override(client, monkeypatch):
    """`coerce` only checks the declared kind, which for a string is no check at all — the real
    constraints live in the sink. Validated before the write, or the row commits and the sink then
    rejects it, leaving an override the panel reports as active and nothing ever applies."""
    token = await _admin(client, monkeypatch, slug="ip11", email="boss@ip11.com")
    bad = await client.put("/api/admin/runtime/settings/admin_ip_allowlist", headers=auth(token),
                           json={"value": "1.1.1.1,2.2.2.2,3.3.3.3"})
    assert bad.status_code == 400

    rows = (await client.get("/api/admin/runtime/settings", headers=auth(token))).json()
    row = next(x for x in rows if x["key"] == "admin_ip_allowlist")
    assert row["overridden"] is False, "a refused value was stored anyway"
