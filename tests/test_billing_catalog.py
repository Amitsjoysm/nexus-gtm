# tests/test_billing_catalog.py
from __future__ import annotations


def test_seed_is_wellformed():
    from nexus.billing.catalog import CAPABILITY_SEED
    from nexus.models.billing import CAPABILITY_MODES, CAPABILITY_UNITS, METER_KINDS

    assert len(CAPABILITY_SEED) >= 55
    ids = [c["id"] for c in CAPABILITY_SEED]
    assert len(ids) == len(set(ids)), "duplicate capability ids"
    for c in CAPABILITY_SEED:
        assert c["unit"] in CAPABILITY_UNITS, c["id"]
        assert c["meter_kind"] in METER_KINDS, c["id"]
        assert c["default_mode"] in CAPABILITY_MODES, c["id"]
        assert "." in c["id"], f"{c['id']} must be namespaced (category.name)"
        assert c["name"] and c["category"]


def test_seed_dependencies_resolve():
    """Every depends_on target must itself be a catalog entry — a dangling gate would silently
    block a feature forever."""
    from nexus.billing.catalog import CAPABILITY_SEED

    ids = {c["id"] for c in CAPABILITY_SEED}
    for c in CAPABILITY_SEED:
        for dep in c.get("depends_on", []):
            assert dep in ids, f"{c['id']} depends on unknown {dep}"


async def test_sync_catalog_is_idempotent_and_updates_metadata():
    from nexus.billing.catalog import CAPABILITY_SEED, sync_catalog
    from nexus.core.db import get_sessionmaker
    from nexus.models.billing import BillingCapability

    first = await sync_catalog()
    assert first["created"] == len(CAPABILITY_SEED)

    second = await sync_catalog()
    assert second["created"] == 0 and second["updated"] == 0  # no churn on re-run

    # An admin-edited row must not be clobbered on name, but metadata drift IS corrected.
    async with get_sessionmaker()() as s:
        cap = await s.get(BillingCapability, "ai.email_draft")
        cap.unit = "wrong"
        await s.commit()
    third = await sync_catalog()
    assert third["updated"] == 1
    async with get_sessionmaker()() as s:
        assert (await s.get(BillingCapability, "ai.email_draft")).unit == "action"
