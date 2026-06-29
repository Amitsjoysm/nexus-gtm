"""Connector lookup. A process-wide override lets tests inject a canned FixtureConnector for the
sync-job path without touching real provider code."""
from __future__ import annotations

from nexus.network.connectors.base import NetworkConnector
from nexus.network.connectors.fixture import FixtureConnector

# Two slots by design: `_REGISTRY` maps a provider key -> connector class (multi-provider, like
# integrations/search/provider.py), while `_override` is a test-only short-circuit that returns one
# connector for ANY provider so the sync-job path can be exercised offline without real adapters.
_REGISTRY: dict[str, type] = {"fixture": FixtureConnector}
_override: NetworkConnector | None = None


def set_network_connector(connector: NetworkConnector | None) -> None:
    """Test seam: force every lookup to return ``connector`` (or clear with ``None``)."""
    global _override
    _override = connector


def get_network_connector(provider: str) -> NetworkConnector:
    if _override is not None:
        return _override
    cls = _REGISTRY.get(provider)
    if cls is None:
        raise ValueError(f"unknown network provider: {provider}")
    return cls()
