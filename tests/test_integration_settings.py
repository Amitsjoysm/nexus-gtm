"""Deployment-level integration credentials resolve exactly like the main Settings object.

The point of these tests is the *equivalence*: this is a second BaseSettings class only because
``nexus/core/config.py`` is out of scope for this change, so it must read the same prefix and the
same ``.env``, and default to inert rather than to a fake value.
"""
from __future__ import annotations


def test_integration_settings_read_the_nexus_prefix(monkeypatch):
    from nexus.integrations.settings import get_integration_settings

    monkeypatch.setenv("NEXUS_HUBSPOT_CLIENT_ID", "cid-123")
    get_integration_settings.cache_clear()
    try:
        assert get_integration_settings().hubspot_client_id == "cid-123"
    finally:
        get_integration_settings.cache_clear()


def test_integration_settings_default_to_inert():
    """Unset credentials read as 'not configured', never as a fake default — the OAuth endpoints
    return a clear error instead of half-building an authorize URL the vendor would reject."""
    from nexus.integrations.settings import get_integration_settings

    get_integration_settings.cache_clear()
    try:
        s = get_integration_settings()
        assert s.hubspot_client_id == ""
        assert s.hubspot_client_secret == ""
        assert s.salesforce_client_id == ""
        assert s.oauth_redirect_base == ""
        # The one field with a real default: production login host.
        assert s.salesforce_login_base == "https://login.salesforce.com"
    finally:
        get_integration_settings.cache_clear()


def test_integration_settings_mirror_the_main_settings_config():
    """If these drift apart, a value set in .env resolves for one and not the other — the exact
    failure this class was written to avoid."""
    from nexus.core.config import Settings
    from nexus.integrations.settings import IntegrationSettings

    for key in ("env_prefix", "env_file", "extra"):
        assert IntegrationSettings.model_config.get(key) == Settings.model_config.get(key), key
