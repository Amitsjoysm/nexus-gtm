# nexus/alerts/signal_alerts.py
"""The subscriber that turns an ingested signal into an alert.

This is the wire that was missing. ``signal.created`` was published on every ingested signal and the
only subscriber on the bus listened for ``account.scored``, so a funding round reached the database
and stopped there. Everything the collection pipeline gathers becomes actionable here or nowhere.

Registered alongside the CRM subscriber in ``main.py`` and ``workers/worker.py``, so it is live in
both the API process and the worker.
"""
from __future__ import annotations

import logging

from nexus.core.events import Event, EventBus, get_event_bus

logger = logging.getLogger("nexus.alerts.signal_alerts")


async def on_signal_created(event: Event) -> None:
    """Create an alert for a signal worth interrupting someone for.

    Never raises. An alerting failure must not roll back the ingestion that produced the signal —
    losing the signal to save the notification would be exactly backwards.
    """
    from nexus.alerts.rules import alert_dedupe_key, decide
    from nexus.alerts.service import get_alert_service
    from nexus.core.config import get_settings
    from nexus.core.db import get_sessionmaker, utcnow
    from nexus.core.tenancy import TenantSession, apply_rls
    from nexus.models.signal import SignalEvent

    signal_id = (event.payload or {}).get("signal_id")
    tenant_id = event.tenant_id
    if not signal_id or not tenant_id:
        return

    try:
        settings = get_settings()
        if not settings.signal_alerts_enabled:
            return
        async with get_sessionmaker()() as session:
            # A hand-built TenantSession must bind RLS first or every write is rejected against
            # Postgres — see the trap documented in CLAUDE.md.
            await apply_rls(session, tenant_id)
            ts = TenantSession(session, tenant_id)

            signal = await ts.first(SignalEvent, SignalEvent.id == signal_id)
            if signal is None:
                return

            decision = decide(
                signal.kind, float(signal.strength or 0),
                floor=settings.signal_alert_floor,
            )
            if not decision.should_alert:
                return

            # Alert-level dedupe, distinct from signal dedupe: two different job postings are two
            # real signals and one notification. Keyed per day per category per account.
            key = alert_dedupe_key(
                decision.category, signal.account_id or "", f"{utcnow():%Y-%m-%d}"
            )
            # Scanning this account's recent alerts rather than indexing into the JSON column:
            # SQLite (the offline suite) and Postgres disagree on JSON path syntax, and a query
            # that raises on one of them would fail on every signal. The scan is bounded by one
            # account's recent alerts, so it is cheap.
            if await _find_by_key(ts, signal.account_id, key) is not None:
                return

            await get_alert_service().create(
                ts,
                title=signal.title or f"New {signal.kind} signal",
                body="\n\n".join(x for x in (signal.body, decision.suggested_action) if x),
                severity=decision.severity,
                account_id=signal.account_id,
                signal_id=signal.id,
                source="signal",
                meta={
                    "dedupe": key,
                    "category": decision.category,
                    "signal_kind": signal.kind,
                    "strength": float(signal.strength or 0),
                    "suggested_action": decision.suggested_action,
                    "source_url": signal.url or "",
                    "reason": decision.reason,
                },
            )
            await session.commit()
    except Exception:
        logger.warning("failed to raise an alert for signal %s", signal_id, exc_info=True)


async def _find_by_key(ts, account_id: str | None, key: str):
    from nexus.models.alerts import Alert

    rows = await ts.list(Alert, Alert.account_id == account_id, limit=50) if account_id else []
    for row in rows:
        if (row.meta or {}).get("dedupe") == key:
            return row
    return None


def register_signal_alert_subscriber(bus: EventBus | None = None) -> None:
    """Wire signals to alerts. Idempotent — registering twice would double every notification."""
    bus = bus or get_event_bus()
    if getattr(bus, "_signal_alerts_registered", False):
        return
    bus.subscribe("signal.created", on_signal_created)
    bus._signal_alerts_registered = True
