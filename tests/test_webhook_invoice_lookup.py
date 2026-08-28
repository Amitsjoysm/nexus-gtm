# tests/test_webhook_invoice_lookup.py
"""Finding the invoice a payment event concerns must be a lookup, not a scan.

`handle_event` and `_apply_invoice_event` selected EVERY `billing_invoices` row with status
`finalized` or `paid` — platform-wide, unfiltered — hydrated them all as ORM objects, and then
compared `meta["psp_reference"]` in Python. The reference lives inside a JSON blob with no index,
so the cost of matching one webhook is O(total invoices ever finalized).

Cross-tenant reading is correct here: the event arrives with no tenant context and the provider
reference is globally unique. The full table load is not.

The reference is now a real indexed column, written alongside the meta key it came from so a row
created by the previous release is still found.
"""
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def _invoice(ts, *, period: str, meta: dict, reference: str | None = None,
                   psp_invoice_id: str | None = None):
    from nexus.models.billing import BillingInvoice

    inv = BillingInvoice(
        period_key=period, status="finalized", total_cents=1000, meta=meta,
        psp_reference=reference, psp_invoice_id=psp_invoice_id,
    )
    ts.add(inv)
    await ts.flush()
    return inv


async def test_the_lookup_is_a_query_not_a_full_scan():
    """Asserted by counting rows loaded, because a scan is correct and merely unaffordable —
    it cannot be caught by checking the answer."""
    from sqlalchemy import event

    from nexus.billing.webhooks import find_invoice_by_provider_reference
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingInvoice

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        for i in range(25):
            await _invoice(ts, period=f"2026-{i:02d}", meta={},
                           reference=f"pi_noise_{i}", psp_invoice_id=f"in_noise_{i}")
        await _invoice(ts, period="2026-99", meta={}, reference="pi_wanted",
                       psp_invoice_id="in_wanted")

    loaded = 0

    def count(*args, **kw):
        nonlocal loaded
        loaded += 1

    event.listen(BillingInvoice, "load", count)
    try:
        async with get_platform_sessionmaker()() as session:
            found = await find_invoice_by_provider_reference(
                session, payment_intent="pi_wanted", psp_invoice_id=""
            )
    finally:
        event.remove(BillingInvoice, "load", count)

    assert found is not None and found.psp_reference == "pi_wanted"
    assert loaded <= 2, f"loaded {loaded} invoices to match one reference — this is the scan"


async def test_a_row_written_before_the_column_existed_is_still_found():
    """The backfill covers what exists at deploy; this covers anything in flight during it."""
    from nexus.billing.webhooks import find_invoice_by_provider_reference
    from nexus.core.db import get_platform_sessionmaker

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _invoice(ts, period="2026-legacy", meta={"psp_reference": "pi_legacy"})

    async with get_platform_sessionmaker()() as session:
        found = await find_invoice_by_provider_reference(
            session, payment_intent="pi_legacy", psp_invoice_id=""
        )
    assert found is not None, "a pre-column invoice became unfindable"


async def test_either_reference_matches():
    from nexus.billing.webhooks import find_invoice_by_provider_reference
    from nexus.core.db import get_platform_sessionmaker

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _invoice(ts, period="2026-a", meta={}, reference="pi_a", psp_invoice_id="in_a")

    async with get_platform_sessionmaker()() as session:
        by_pi = await find_invoice_by_provider_reference(
            session, payment_intent="pi_a", psp_invoice_id=""
        )
        by_inv = await find_invoice_by_provider_reference(
            session, payment_intent="", psp_invoice_id="in_a"
        )
        missing = await find_invoice_by_provider_reference(
            session, payment_intent="pi_nope", psp_invoice_id="in_nope"
        )
    assert by_pi is not None and by_inv is not None
    assert by_pi.id == by_inv.id
    assert missing is None


async def test_no_reference_at_all_matches_nothing():
    """An event with neither reference must not match the first invoice in the table."""
    from nexus.billing.webhooks import find_invoice_by_provider_reference
    from nexus.core.db import get_platform_sessionmaker

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _invoice(ts, period="2026-b", meta={}, reference="pi_b", psp_invoice_id="in_b")

    async with get_platform_sessionmaker()() as session:
        assert await find_invoice_by_provider_reference(
            session, payment_intent="", psp_invoice_id=""
        ) is None


async def test_legacy_rows_are_not_crowded_out_by_ordinary_invoices():
    """The fallback must match in SQL, not filter a window of recent rows in Python.

    Both psp columns are NULL on every invoice between finalization and collection, and on
    every zero-total invoice. Those are ordinary rows, newer than any legacy one, and most
    numerous exactly at month end — so a "newest N" window fills with rows that can never
    match and hides the one row the fallback exists to find.
    """
    from nexus.billing.webhooks import find_invoice_by_provider_reference
    from nexus.core.db import get_platform_sessionmaker

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _invoice(ts, period="2020-legacy", meta={"psp_reference": "pi_crowded"})
        # A normal month end: finalized invoices awaiting collection, both columns NULL.
        for i in range(520):
            await _invoice(ts, period=f"2026-{i:04d}", meta={})

    async with get_platform_sessionmaker()() as session:
        found = await find_invoice_by_provider_reference(
            session, payment_intent="pi_crowded", psp_invoice_id=""
        )
    assert found is not None, "legacy invoice hidden by ordinary rows sharing its NULL columns"


async def test_a_webhook_promotes_a_legacy_row_onto_the_indexed_columns():
    """A row found through the fallback must leave it, or the slow path is permanent.

    `_apply_invoice_event` learns the provider invoice id from the event. Writing it only into
    `meta` leaves the row exactly as unmigrated as it was, for the life of the invoice.
    """
    from nexus.billing.webhooks import VerifiedEvent, _apply_invoice_event
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingInvoice

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        inv = await _invoice(ts, period="2026-promote", meta={"psp_reference": "pi_promote"})
        inv_id = inv.id

    ev = VerifiedEvent(event_id="evt_promote", event_type="invoice.finalized",
                       payload={"data": {"object": {}}}, digest="d")
    async with get_platform_sessionmaker()() as session:
        await _apply_invoice_event(
            session, ev,
            {"id": "in_promote", "payment_intent": "pi_promote", "status": "open"},
            {"applied": False},
        )
        await session.commit()

    async with tenant_session(tid) as ts:
        inv = await ts.get(BillingInvoice, inv_id)
        assert inv.psp_invoice_id == "in_promote", "provider invoice id not promoted"
        assert inv.psp_reference == "pi_promote", "meta reference not promoted"


async def test_promotion_never_overwrites_what_collection_recorded():
    """Fill-only. The event is evidence about the invoice, not authority over what we charged."""
    from nexus.billing.webhooks import VerifiedEvent, _apply_invoice_event
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingInvoice

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        inv = await _invoice(ts, period="2026-keep", meta={}, reference="in_original",
                             psp_invoice_id="in_original")
        inv_id = inv.id

    ev = VerifiedEvent(event_id="evt_keep", event_type="invoice.paid",
                       payload={"data": {"object": {}}}, digest="d")
    async with get_platform_sessionmaker()() as session:
        await _apply_invoice_event(
            session, ev,
            {"id": "in_original", "payment_intent": "pi_other", "status": "paid"},
            {"applied": False},
        )
        await session.commit()

    async with tenant_session(tid) as ts:
        inv = await ts.get(BillingInvoice, inv_id)
        assert inv.psp_reference == "in_original"
        assert inv.psp_invoice_id == "in_original"
