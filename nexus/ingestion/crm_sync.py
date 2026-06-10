"""CRM auto-sync: the shared "push one account" unit + the event fast-path subscriber.

`sync_account_to_crm` is the single source of truth for what an automatic CRM sync does; both
the heartbeat sweep (handle_sync_crm_due_accounts) and the event single-account job
(handle_sync_crm_account) call it. It is change-aware via Account.crm_synced_at and never raises
across the connector boundary (push_* return CRMPushResult).
"""
from __future__ import annotations

from datetime import datetime

from nexus.core.config import get_settings
from nexus.core.db import ensure_aware
from nexus.core.events import Event, EventBus, get_event_bus
from nexus.core.tenancy import TenantSession
from nexus.ingestion.crm import CRMConnector, CRMPushResult
from nexus.models.account import Account, Contact
from nexus.models.signal import SignalEvent


async def sync_account_to_crm(
    ts: TenantSession, account: Account, *, connector: CRMConnector, now: datetime
) -> CRMPushResult:
    """Push one account's state to the CRM and stamp crm_synced_at on success.

    Always pushes the account record + contact roster. For an already-synced account, also
    pushes one activity per signal ingested since the last sync (created_at > crm_synced_at).
    A never-synced account gets the record + contacts but NO historical activity backfill
    (anti-flood). Stamps crm_synced_at = now only when push_account succeeds, so a failed push
    leaves the account due for retry on the next sweep (self-healing).
    """
    contacts = await ts.list(Contact, Contact.account_id == account.id)
    res = await connector.push_account(account, contacts=contacts)
    if not res.ok:
        return res

    prior = account.crm_synced_at
    if prior is not None:  # no first-sync activity backfill
        prior = ensure_aware(prior)
        signals = await ts.list(SignalEvent, SignalEvent.account_id == account.id)
        for sig in signals:
            if ensure_aware(sig.created_at) > prior:
                await connector.push_activity(
                    account_id=account.crm_id or account.id,
                    kind="signal",
                    detail={"signal": sig.title, "kind": sig.kind, "source": sig.source},
                )
    account.crm_synced_at = now
    return res


async def on_account_scored(event: Event) -> None:
    """Event fast-path: enqueue a single-account CRM sync when auto-sync is enabled.

    The authoritative gating (global switch + per-tenant opt-in) lives in the handler; this is
    a cheap pre-filter so we do not enqueue no-op jobs when auto-sync is entirely off.
    """
    if not get_settings().crm_sync_enabled:
        return
    # Lazy import avoids a tasks <-> crm_sync import cycle at module load.
    from nexus.workers.tasks import enqueue_sync_crm_account

    await enqueue_sync_crm_account(event.tenant_id, event.payload["account_id"])


def register_crm_sync_subscribers(bus: EventBus | None = None) -> None:
    """Subscribe the CRM-sync fast-path to ``account.scored``. Called from app + worker startup."""
    bus = bus or get_event_bus()
    bus.subscribe("account.scored", on_account_scored)
