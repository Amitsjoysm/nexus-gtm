from __future__ import annotations

from datetime import datetime, timezone

import pytest


def test_sync_batch_dtos_construct():
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, Touchpoint

    batch = NetworkSyncBatch(
        identities=[RawIdentity(external_id="g1", email="A@Acme.com", name="Ann", title="CTO")],
        touchpoints=[
            Touchpoint(person_external_id="g1", kind="email_sent",
                       at=datetime(2026, 6, 1, tzinfo=timezone.utc))
        ],
        next_cursor="cursor-2",
    )
    assert batch.identities[0].relation == "contact"  # default
    assert batch.next_cursor == "cursor-2"
    assert batch.touchpoints[0].kind == "email_sent"


async def test_fixture_connector_returns_its_batch():
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, SourceAccountRef
    from nexus.network.connectors.fixture import FixtureConnector

    canned = NetworkSyncBatch(identities=[RawIdentity(external_id="g1", name="Ann")])
    conn = FixtureConnector(canned)
    ref = SourceAccountRef(id="acc1", provider="fixture", external_account_id="rep@acme.com")

    out = await conn.fetch(ref, None)
    assert out.identities[0].external_id == "g1"
    tokens = await conn.complete_auth("ignored")
    assert tokens.access_token == "fixture-token"


def test_registry_returns_fixture_and_override():
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity
    from nexus.network.connectors.fixture import FixtureConnector
    from nexus.network.connectors.registry import (
        get_network_connector,
        set_network_connector,
    )

    assert get_network_connector("fixture").provider == "fixture"
    with pytest.raises(ValueError):
        get_network_connector("nope")

    override = FixtureConnector(NetworkSyncBatch(identities=[RawIdentity(external_id="x")]))
    set_network_connector(override)
    try:
        # the override short-circuits the registry for any provider name
        assert get_network_connector("google") is override
        assert get_network_connector("microsoft") is override
    finally:
        set_network_connector(None)
    # cleared → registry behaviour restored
    assert get_network_connector("fixture").provider == "fixture"
