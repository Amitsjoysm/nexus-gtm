# nexus/alerts/signal_alerts.py
"""Turning an ingested signal into an alert.

The gap this closes: ``signal.created`` was published on every ingested signal and *nothing*
subscribed to it — the only listener on the bus handled ``account.scored``. Alerts were created in
three places, none of them from an incoming signal, so the entire collection pipeline landed in a
table nobody was notified about.

**Why this is called inline rather than from the event bus.** It was a bus subscriber first, and
that could never have worked, for two compounding reasons found by running it against the deployed
stack:

* At publish time the signal has been ``flush()``ed, not committed. A subscriber opening its own
  session cannot see it, so the lookup returned None and the handler silently did nothing — which
  is exactly what the deployed system did: five signals, zero alerts.
* ``alerts.signal_id`` is a foreign key. Even with the row passed in, writing the alert from a
  second transaction would violate the constraint against a signal that has not committed.

So the alert must be created in the **same transaction as the signal**. That is not a compromise:
an alert about a signal that was rolled back is worse than no alert, and this way the two either
both exist or neither does.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.alerts.signal_alerts")


async def raise_alerts_for(ts, account, signals) -> list:
    """Create alerts for freshly-ingested signals, in the caller's transaction.

    Never raises. An alerting failure must not roll back the ingestion that produced the signals —
    losing the signal to save the notification would be exactly backwards.
    """
    from nexus.alerts.rules import alert_dedupe_key, decide
    from nexus.alerts.service import get_alert_service
    from nexus.core.config import get_settings
    from nexus.core.db import utcnow

    settings = get_settings()
    if not settings.signal_alerts_enabled or not signals:
        return []

    created = []
    seen: set[str] = set()
    try:
        service = get_alert_service()
        for signal in signals:
            decision = decide(
                signal.kind, float(signal.strength or 0), floor=settings.signal_alert_floor
            )
            if not decision.should_alert:
                continue

            # Alert-level dedupe, distinct from signal dedupe: signals dedupe on the EVENT, alerts
            # on ATTENTION. Two different job postings are two real signals and one notification.
            key = alert_dedupe_key(
                decision.category, signal.account_id or "", f"{utcnow():%Y-%m-%d}"
            )
            if key in seen or await _already_alerted(ts, signal.account_id, key):
                continue
            seen.add(key)

            created.append(await service.create(
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
            ))
    except Exception:
        logger.warning("failed to raise alerts for %s", getattr(account, "id", "?"), exc_info=True)
    return created


async def _already_alerted(ts, account_id: str | None, key: str) -> bool:
    """Whether this account already has today's alert for this category.

    Scans the account's recent alerts rather than indexing into the JSON column: SQLite and
    Postgres disagree on JSON path syntax, and a query that raises on one of them would fail on
    every signal. Bounded by one account's recent alerts, so it is cheap.
    """
    from nexus.models.alerts import Alert

    if not account_id:
        return False
    rows = await ts.list(Alert, Alert.account_id == account_id, limit=50)
    return any((row.meta or {}).get("dedupe") == key for row in rows)
