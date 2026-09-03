# tests/test_billing_new_call_sites.py
"""The call sites added to close the outreach and data gaps actually record usage."""
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def _events(ts, capability_id: str) -> list:
    from sqlalchemy import select

    from nexus.models.billing import BillingUsageEvent

    return list(
        (
            await ts.session.scalars(
                select(BillingUsageEvent).where(
                    BillingUsageEvent.tenant_id == ts.tenant_id,
                    BillingUsageEvent.capability_id == capability_id,
                )
            )
        ).all()
    )


async def test_a_csv_import_bills_the_rows_that_landed_not_the_file_size():
    from nexus.api.routers.imports import _meter_import

    tid = await make_tenant()

    class P:
        user_id = "u1"

    async with tenant_session(tid) as ts:
        await _meter_import(ts, {"created": 78, "updated": 2, "skipped": 4920}, user_id=P.user_id,
                            kind="accounts")
        rows = await _events(ts, "data.import_csv")
        assert len(rows) == 1
        assert float(rows[0].quantity) == 80, "billed the file, not the records bought"
        # 78 created + 2 updated; the 4,920 duplicates the customer already had are not sold twice.


async def test_an_import_that_landed_nothing_is_not_billed():
    from nexus.api.routers.imports import _meter_import

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _meter_import(ts, {"created": 0, "updated": 0, "skipped": 500}, user_id="u1",
                            kind="accounts")
        assert await _events(ts, "data.import_csv") == []


async def test_an_export_is_one_unit_however_large():
    from nexus.api.routers.accounts import _meter

    tid = await make_tenant()

    class P:
        user_id = "u1"

    async with tenant_session(tid) as ts:
        await _meter(ts, "data.export", 1, P)
        rows = await _events(ts, "data.export")
        assert len(rows) == 1 and float(rows[0].quantity) == 1


async def test_an_empty_result_is_never_billed():
    from nexus.api.routers.accounts import _meter

    tid = await make_tenant()

    class P:
        user_id = "u1"

    async with tenant_session(tid) as ts:
        await _meter(ts, "discovery.lookalike_company", 0, P)
        assert await _events(ts, "discovery.lookalike_company") == []


async def test_a_send_records_one_outreach_email_send():
    from nexus.orchestration.tools import _meter_send

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _meter_send(ts)
        rows = await _events(ts, "outreach.email_send")
        assert len(rows) == 1 and float(rows[0].quantity) == 1


async def test_metering_never_raises_into_the_caller():
    """A billing failure must not be the reason an outbound message or an import fails."""
    from nexus.api.routers.accounts import _meter
    from nexus.orchestration.tools import _meter_send

    tid = await make_tenant()

    class Broken:
        user_id = "u1"

    async with tenant_session(tid) as ts:
        # A capability that does not exist still must not raise.
        await _meter(ts, "does.not.exist", 3, Broken)
        await _meter_send(ts)


async def test_the_meter_reads_the_keys_the_importer_actually_returns():
    """The contract between csv_ingest and the meter, pinned.

    `_meter_import` originally read `result["imported"]`, a key `import_accounts_csv` has never
    returned — so the meter was wired and billed zero on every upload. Verified live: a 3-row
    import created 3 accounts and produced no usage event at all. A meter that is present and
    inert is worse than an absent one, because the code reads as though the action is billed.
    """
    import inspect

    from nexus.api.routers.imports import _meter_import
    from nexus.imports import csv_ingest

    source = inspect.getsource(csv_ingest)
    # The literal dict csv_ingest builds for its callers.
    assert '"created": created, "updated": updated' in source, (
        "csv_ingest's return shape changed; _meter_import reads it by key"
    )
    meter_src = inspect.getsource(_meter_import)
    for key in ("created", "updated"):
        assert f'"{key}"' in meter_src, f"_meter_import no longer reads {key!r}"
    assert '"imported"' not in meter_src, "reading a key the importer does not return"
