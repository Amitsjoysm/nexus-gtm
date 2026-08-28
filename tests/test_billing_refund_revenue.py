# tests/test_billing_refund_revenue.py
"""A refund has to reach the revenue figures.

`charge.refunded` wrote `refunded_at` and `amount_refunded` into `invoice.meta` and stopped there.
Nothing read either field, so `collection_health` still counted a fully refunded invoice as paid
at its full amount: revenue was overstated by the entire refund total, indefinitely and silently.

The invoice deliberately stays `paid` rather than becoming `void`. `void` means "never owed"; a
refunded invoice WAS owed and WAS collected, and the money going back out is a second event. That
is the same "corrections are compensating rows, never mutations" rule the rest of this package
follows.
"""
from __future__ import annotations

from tests.conftest import make_tenant, tenant_session


async def _invoice(ts, *, period: str, total: int, status: str, meta: dict | None = None):
    from nexus.models.billing import BillingInvoice

    inv = BillingInvoice(
        period_key=period, status=status, total_cents=total, subtotal_cents=total,
        meta=meta or {},
    )
    ts.add(inv)
    await ts.flush()
    return inv


async def test_a_full_refund_is_netted_out_of_collected_revenue():
    from nexus.billing.revenue import collection_health

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _invoice(ts, period="2026-07", total=10_000, status="paid")
        await _invoice(
            ts, period="2026-08", total=10_000, status="paid",
            meta={"amount_refunded": 10_000, "refunded_at": "2026-08-20T00:00:00Z"},
        )

    health = await collection_health()
    assert health.invoiced_cents == 20_000
    assert health.paid_cents == 20_000, "gross collected is unchanged — the money did arrive"
    assert health.refunded_cents == 10_000
    assert health.net_paid_cents == 10_000, "revenue still counts a refunded invoice in full"
    assert health.refunded_invoices == 1


async def test_a_partial_refund_is_netted_at_its_own_amount():
    from nexus.billing.revenue import collection_health

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _invoice(
            ts, period="2026-08", total=10_000, status="paid",
            meta={"amount_refunded": 2_500},
        )

    health = await collection_health()
    assert health.refunded_cents == 2_500
    assert health.net_paid_cents == 7_500


async def test_a_refund_larger_than_the_invoice_cannot_drive_revenue_negative():
    """Defensive: the refund figure comes from the provider, so it is not ours to trust."""
    from nexus.billing.revenue import collection_health

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _invoice(
            ts, period="2026-08", total=5_000, status="paid",
            meta={"amount_refunded": 999_999},
        )

    health = await collection_health()
    assert health.refunded_cents == 5_000, "a refund is capped at what was collected"
    assert health.net_paid_cents == 0


async def test_an_unrefunded_period_reports_zero_and_the_dict_carries_the_fields():
    from nexus.billing.revenue import collection_health

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        await _invoice(ts, period="2026-08", total=4_200, status="paid")

    health = await collection_health()
    assert health.refunded_cents == 0
    assert health.net_paid_cents == 4_200
    d = health.as_dict()
    for key in ("refunded_cents", "net_paid_cents", "refunded_invoices"):
        assert key in d, f"{key} missing from the reported figures"
