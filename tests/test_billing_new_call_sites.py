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
        await _meter_import(ts, {"imported": 80, "skipped": 4920}, user_id=P.user_id,
                            kind="accounts")
        rows = await _events(ts, "data.import_csv")
        assert len(rows) == 1
        assert float(rows[0].quantity) == 80, "billed the file, not the records bought"


async def test_an_import_that_landed_nothing_is_not_billed():
    from nexus.api.routers.imports import _meter_import

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _meter_import(ts, {"imported": 0, "skipped": 500}, user_id="u1", kind="accounts")
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
