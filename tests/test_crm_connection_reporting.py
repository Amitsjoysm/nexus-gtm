# tests/test_crm_connection_reporting.py
"""The CRM screen must not tell an admin they are disconnected while syncing is working.

Measured live 2026-08-27: `GET /integrations/crm/connection` returned
`{"provider":"hubspot","source":"env","has_credentials":false}` on a deployment where
`POST /integrations/crm/connection/test` answered `"HubSpot portal 246520431 — Connected."`.

`has_credentials` was hard-false on the env branch, so it reported on the tenant row only while
naming the deployment's provider — two different facts in one object. This codebase works hard to
avoid "configured and doing nothing"; this is the inverse, and it is the one that makes an admin
paste a second token to fix a connection that was never broken.

The credential itself still appears nowhere: this reports only WHETHER one exists.
"""
from __future__ import annotations

import pytest

from nexus.api.routers.integrations import _connection_out


@pytest.fixture
def settings(monkeypatch):
    from nexus.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "crm_provider", "hubspot")
    monkeypatch.setattr(s, "hubspot_access_token", "")
    return s


def test_a_keyed_deployment_reports_that_it_has_credentials(settings, monkeypatch):
    monkeypatch.setattr(settings, "hubspot_access_token", "pat-na1-secret")
    out = _connection_out(None, env_provider="hubspot")
    assert out.source == "env"
    assert out.provider == "hubspot"
    assert out.has_credentials is True


def test_an_unkeyed_deployment_still_reports_no_credentials(settings):
    out = _connection_out(None, env_provider="hubspot")
    assert out.source == "env"
    assert out.has_credentials is False, "claiming a credential that does not exist is worse"


def test_the_token_is_never_in_the_response(settings, monkeypatch):
    monkeypatch.setattr(settings, "hubspot_access_token", "pat-na1-secret")
    body = _connection_out(None, env_provider="hubspot").model_dump_json()
    assert "pat-na1-secret" not in body
    assert "secret" not in body.lower()


def test_no_provider_configured_is_unchanged(settings, monkeypatch):
    monkeypatch.setattr(settings, "crm_provider", "stub")
    out = _connection_out(None, env_provider="stub")
    assert out.source == "none"
    assert out.has_credentials is False


def test_a_tenant_row_still_wins_over_the_deployment_default(settings, monkeypatch):
    """A workspace that connected its own CRM must report on THAT, not on the env fallback."""
    monkeypatch.setattr(settings, "hubspot_access_token", "pat-na1-secret")

    class _Row:
        provider = "hubspot"
        status = "verified"
        api_base = ""
        verified_at = None
        last_error = None
        updated_at = None

    import nexus.api.routers.integrations as mod

    monkeypatch.setattr(mod, "has_credentials", lambda row: True)
    out = _connection_out(_Row(), env_provider="hubspot")
    assert out.source == "tenant"
    assert out.has_credentials is True
