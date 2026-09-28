"""Superadmin configuration for mailbox OAuth apps and the ledger stores (spec §12).

Offline by design: nothing here calls Google, Microsoft or a store. The calls themselves are exercised
by `tests_live/engagement/test_credentials_live.py` against the real apps (D21).
"""
from __future__ import annotations

import pytest

from nexus.core.config import get_settings
from tests.conftest import assert_staff_surface_hidden, auth, signup


@pytest.fixture(autouse=True)
def _clean_settings_and_keys():
    from nexus.providers import resolver

    settings = get_settings()
    keys = ("engagement_public_base_url", "engagement_google_client_id",
            "engagement_microsoft_client_id", "engagement_microsoft_tenant",
            "engagement_campaigns_enabled", "engagement_google_pubsub_topic",
            "engagement_google_push_service_account")
    before = {k: getattr(settings, k) for k in keys}
    resolver.invalidate()
    yield
    for key, value in before.items():
        setattr(settings, key, value)
    resolver.invalidate()


def test_the_six_engagement_secrets_are_provider_keys_with_env_floors():
    from nexus.providers.catalog import PROVIDERS

    settings = get_settings()
    for key_id in ("google_oauth", "microsoft_oauth", "ledger_archive", "ledger_training",
                   "ledger_insights", "ledger_pseudonym"):
        assert key_id in PROVIDERS
        assert hasattr(settings, PROVIDERS[key_id].env_attr)


async def test_an_unconfigured_provider_names_every_missing_piece():
    from nexus.engagement import config

    app = await config.oauth_app(config.GOOGLE)
    assert not app.configured
    assert any("Public base URL" in m for m in app.missing)
    assert any("client id" in m for m in app.missing)
    assert any("google_oauth" in m for m in app.missing)


async def test_a_managed_secret_and_two_settings_configure_a_provider(monkeypatch):
    from nexus.engagement import config
    from nexus.providers import resolver
    from nexus.providers.service import add_key

    monkeypatch.setattr(get_settings(), "engagement_public_base_url", "https://app.example.com")
    monkeypatch.setattr(get_settings(), "engagement_microsoft_client_id",
                        "11111111-2222-3333-4444-555555555555")
    await add_key("microsoft_oauth", "prod app", "a-real-looking-client-secret-value")
    resolver.invalidate()

    app = await config.oauth_app(config.MICROSOFT)
    assert app.configured, app.missing
    assert app.client_secret == "a-real-looking-client-secret-value"
    assert app.tenant == "common"
    assert app.redirect_uri == (
        "https://app.example.com/api/engagement/mailboxes/oauth/microsoft/callback"
    )
    assert "https://graph.microsoft.com/Mail.Send" in app.scopes


def test_endpoints_are_derived_from_the_base_url(monkeypatch):
    from nexus.engagement import config

    monkeypatch.setattr(get_settings(), "engagement_public_base_url", "https://app.example.com/")
    assert config.gmail_push_audience() == "https://app.example.com/api/engagement/webhooks/gmail"
    assert config.graph_notification_url() == (
        "https://app.example.com/api/engagement/webhooks/graph"
    )
    # The push URL carries no credential of ours: the OIDC signature is what is trusted, and
    # a URL an operator pastes into a console must survive being logged.
    assert "?" not in config.gmail_push_audience()


def test_campaigns_are_on_since_the_cutover_and_off_is_the_emergency_stop(monkeypatch):
    """The engagement engine replaced the old Campaigns and Cadences (spec §13), so it is on by
    default; switching it off stops every campaign and every mailbox read."""
    from nexus.core.config import Settings, get_settings
    from nexus.engagement import config

    assert Settings.model_fields["engagement_campaigns_enabled"].default is True
    assert config.campaigns_enabled() is True
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", False)
    assert config.campaigns_enabled() is False


@pytest.mark.parametrize("key,good,bad", [
    ("engagement_public_base_url", "https://app.example.com", "https://app.example.com/path"),
    ("engagement_google_client_id", "123456789012-abc123.apps.googleusercontent.com",
     "abc123.googleusercontent.com"),
    ("engagement_google_pubsub_topic", "projects/my-project/topics/gmail-replies",
     "gmail-replies"),
    ("engagement_google_push_service_account", "gmail-push@my-project.iam.gserviceaccount.com",
     "someone@gmail.com"),
    ("engagement_microsoft_client_id", "11111111-2222-3333-4444-555555555555", "my-app"),
    ("engagement_microsoft_tenant", "contoso.onmicrosoft.com", "not a tenant"),
])
def test_each_engagement_setting_is_validated_before_it_is_stored(key, good, bad):
    from nexus.runtime_config.catalog import CATALOG
    from nexus.runtime_config.service import _VALIDATORS

    assert CATALOG[key].group == "Mailboxes & engagement"
    _VALIDATORS[key](good)
    with pytest.raises(ValueError):
        _VALIDATORS[key](bad)


def test_the_public_base_url_must_be_https_and_public_outside_a_local_stack(monkeypatch):
    from nexus.runtime_config.service import _VALIDATORS

    monkeypatch.setattr(get_settings(), "env", "prod")
    for url in ("http://app.example.com", "https://127.0.0.1", "https://169.254.169.254"):
        with pytest.raises(ValueError):
            _VALIDATORS["engagement_public_base_url"](url)


def test_the_pseudonymisation_secret_must_be_long_and_varied():
    from nexus.engagement.credential_checks import check_pseudonym_secret

    assert not check_pseudonym_secret("short").ok
    assert not check_pseudonym_secret("a" * 64).ok
    good = check_pseudonym_secret("q3Jv9wXkP2mN7rT5yH8bL4cF6dS1gZ0aEuIoWnMk")
    assert good.ok and good.status == "verified"


async def test_a_store_dsn_pointing_inside_the_network_is_refused_before_connecting(monkeypatch):
    from nexus.engagement.credential_checks import check_store_dsn

    monkeypatch.setattr(get_settings(), "env", "prod")
    result = await check_store_dsn("postgresql://ledger:pw@169.254.169.254:5432/postgres",
                                   store="archive")
    assert not result.ok and "refusing" in result.detail


async def test_the_setup_screen_shows_what_to_paste_and_never_a_secret(client, monkeypatch):
    from nexus.providers.service import add_key

    email = "owner@platformco.com"
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    monkeypatch.setattr(get_settings(), "engagement_public_base_url", "https://app.example.com")
    token = await signup(client, slug="plat", email=email, company="Platform")
    await add_key("google_oauth", "prod", "google-client-secret-never-shown")

    r = await client.get("/api/admin/engagement/setup", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "google-client-secret-never-shown" not in r.text
    google = next(a for a in body["mailbox_apps"] if a["provider"] == "google")
    assert google["redirect_uri"].endswith("/api/engagement/mailboxes/oauth/google/callback")
    assert body["gmail_push_audience"] == (
        "https://app.example.com/api/engagement/webhooks/gmail"
    )
    assert body["ledger_stores"] == {"archive": False, "training": False, "insights": False}


async def test_the_setup_screen_is_hidden_from_workspace_members(client):
    token = await signup(client, slug="member", email="rep@memberco.com", company="Member")
    assert_staff_surface_hidden(
        await client.get("/api/admin/engagement/setup", headers=auth(token))
    )
