# nexus/network/connectors/registry.py
"""Connector lookup. Real OAuth providers (google/microsoft) are built from settings; the offline
``fixture`` stays available for the test suite. A process-wide override lets a test inject a canned
connector for the sync-job path. ``provider_configured`` reports whether a real provider has
credentials (so the API can return a clear 'not configured' instead of inventing data)."""
from __future__ import annotations

from nexus.core.config import get_settings
from nexus.network.connectors.base import NetworkConnector
from nexus.network.connectors.fixture import FixtureConnector

_override: NetworkConnector | None = None


def set_network_connector(connector: NetworkConnector | None) -> None:
    """Test seam: force every lookup to return ``connector`` (or clear with ``None``)."""
    global _override
    _override = connector


def _redirect_uri(provider: str) -> str:
    base = get_settings().network_oauth_redirect_base.rstrip("/")
    return f"{base}/api/network/oauth/{provider}/callback"


def provider_configured(provider: str) -> bool:
    s = get_settings()
    if provider == "google":
        return bool(s.network_google_client_id and s.network_google_client_secret
                    and s.network_oauth_redirect_base)
    if provider == "microsoft":
        return bool(s.network_microsoft_client_id and s.network_microsoft_client_secret
                    and s.network_oauth_redirect_base)
    if provider in ("linkedin", "fixture"):
        return True
    return False


def get_network_connector(provider: str) -> NetworkConnector:
    if _override is not None:
        return _override
    s = get_settings()
    if provider == "google":
        from nexus.network.connectors.google import GoogleConnector

        return GoogleConnector(
            client_id=s.network_google_client_id,
            client_secret=s.network_google_client_secret,
            redirect_uri=_redirect_uri("google"),
        )
    if provider == "microsoft":
        from nexus.network.connectors.microsoft import MicrosoftConnector

        return MicrosoftConnector(
            tenant=s.network_microsoft_tenant,
            client_id=s.network_microsoft_client_id,
            client_secret=s.network_microsoft_client_secret,
            redirect_uri=_redirect_uri("microsoft"),
        )
    if provider == "fixture":
        return FixtureConnector()
    raise ValueError(f"unknown network provider: {provider}")
