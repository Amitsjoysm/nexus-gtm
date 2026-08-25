# nexus/billing/reconcile.py
"""Reconciliation: detect where our subscription state disagrees with the provider's.

Webhooks are the primary mechanism for keeping the two in step, but webhooks can be missed — a
delivery can fail past its retry budget, an endpoint can be misconfigured for a window, an event
type can be added that we do not yet subscribe to. Without a periodic comparison those gaps are
invisible until a customer complains about being billed for a plan they cancelled.

**This job reports; it does not repair.** Silently "fixing" a billing disagreement is worse than
reporting it: if our row says active and Stripe says canceled, the right action depends on which
one the customer actually agreed to, and that is a judgement call with financial and legal
consequences. An automated writer would resolve it confidently and wrongly, and would overwrite
the evidence needed to work out what happened. So drift is logged at WARNING and counted, and a
human decides.
"""
from __future__ import annotations

import logging

from nexus.core.tenancy import TenantSession
from nexus.models.billing import BillingSubscription

logger = logging.getLogger("nexus.billing.reconcile")

# Fields worth comparing. Deliberately narrow: period timestamps drift by seconds for benign
# reasons (clock skew, when each side stamps the roll), and alerting on that would train
# operators to ignore this job entirely.
COMPARED = ("status", "plan_id")


async def compare_subscription(ts: TenantSession, sub: BillingSubscription) -> dict:
    """Compare one local subscription against the provider. Returns a drift report.

    ``drift`` is empty when the two agree. A subscription with no ``psp_subscription_id`` is
    skipped, not reported: enterprise deals are administered locally and never had a provider
    object, so flagging them would bury real findings in noise.
    """
    from nexus.billing.payments import resolve_payment_provider
    from nexus.billing.webhooks import STRIPE_SUBSCRIPTION_STATUS

    if not sub.psp_subscription_id:
        return {"tenant_id": sub.tenant_id, "skipped": "not_provider_managed"}

    remote = await (await resolve_payment_provider()).get_subscription(
        subscription_id=sub.psp_subscription_id
    )
    if not remote:
        # Gone at the provider but still live here. That IS the finding.
        return {
            "tenant_id": sub.tenant_id,
            "subscription_id": sub.psp_subscription_id,
            "drift": {"remote": "missing", "local_status": sub.status},
        }

    raw_status = str(remote.get("status") or "")
    mapped = STRIPE_SUBSCRIPTION_STATUS.get(raw_status)
    remote_plan = str(((remote.get("metadata") or {}).get("plan_id")) or "")

    drift: dict = {}
    # An unmapped remote status is not drift — webhooks deliberately leave those alone, so
    # reporting it would flag our own intentional behaviour as a defect.
    if mapped is not None and mapped != sub.status:
        drift["status"] = {"local": sub.status, "remote": raw_status, "remote_mapped": mapped}
    if remote_plan and remote_plan != sub.plan_id:
        drift["plan_id"] = {"local": sub.plan_id, "remote": remote_plan}

    return {
        "tenant_id": sub.tenant_id,
        "subscription_id": sub.psp_subscription_id,
        "drift": drift,
    }


async def reconcile_tenant(ts: TenantSession) -> dict:
    """Compare every provider-managed subscription for one tenant."""
    checked = skipped = drifted = 0
    findings: list[dict] = []

    for sub in await ts.list(BillingSubscription):
        try:
            report = await compare_subscription(ts, sub)
        except Exception:
            # One unreachable subscription must not stop the sweep for the rest.
            logger.warning(
                "reconciliation failed for subscription %s", sub.psp_subscription_id,
                exc_info=True,
            )
            continue
        if report.get("skipped"):
            skipped += 1
            continue
        checked += 1
        if report.get("drift"):
            drifted += 1
            findings.append(report)
            logger.warning(
                "billing drift for tenant %s subscription %s: %s",
                report["tenant_id"], report.get("subscription_id"), report["drift"],
            )

    return {"checked": checked, "skipped": skipped, "drifted": drifted, "findings": findings}
