"""``emit()`` — the one call that records a ledger event (spec §18.1).

    await emit(ts, "outcome.recorded", refs={"account_id": ..., "contact_id": ...},
               payload={"stage": "meeting"})

Four guarantees, each load-bearing:

* **Same transaction as the action.** The outbox row is added to the caller's session, so it
  commits exactly when the action commits and disappears when the action rolls back. An event about
  something that did not happen would poison a training set.
* **Never harmful.** Any failure is logged and counted and swallowed, and the insert runs in a
  SAVEPOINT so a failed insert cannot poison the caller's transaction either. The action always
  matters more than the evidence about it — the same rule ``metered()`` and ``record_audit`` follow.
* **Consent-gated.** Nothing is written or buffered unless the workspace's consent is ``on``.
* **Stoppable.** ``ledger_capture_enabled`` (Control plane) stops collection everywhere, at once.
"""
from __future__ import annotations

import logging
from datetime import datetime

from nexus.core.tenancy import TenantSession

logger = logging.getLogger("nexus.engagement.ledger")


async def emit(
    ts: TenantSession, event_type: str, *, refs: dict | None = None, payload: dict | None = None,
    actor_user_id: str | None = None, actor_role: str | None = None,
    correlation_id: str | None = None, causation_id: str | None = None, model: str | None = None,
    prompt_version: str | None = None, occurred_at: datetime | None = None,
) -> str | None:
    """Record one event; returns its id, or ``None`` when nothing was recorded. Never raises."""
    from nexus.core import metrics
    from nexus.core.config import get_settings
    from nexus.core.db import utcnow
    from nexus.engagement.ids import new_ulid
    from nexus.engagement.ledger import consent, envelope
    from nexus.models.ledger import LedgerOutbox

    try:
        if not get_settings().ledger_capture_enabled:
            return None
        if event_type not in envelope.EVENT_TYPES:
            logger.warning("ledger event type %r is not registered; dropped", event_type)
            metrics.record_ledger_event("unregistered", "dropped")
            return None
        if await consent.status(ts) != "on":
            return None
        event_id = new_ulid()
        when = occurred_at or utcnow()
        body = envelope.build(
            event_id=event_id, event_type=event_type, tenant_id=ts.tenant_id, occurred_at=when,
            actor_user_id=actor_user_id, actor_role=actor_role, refs=refs,
            correlation_id=correlation_id, causation_id=causation_id, model=model,
            prompt_version=prompt_version, payload=payload,
        )
        async with ts.session.begin_nested():
            ts.add(LedgerOutbox(event_id=event_id, event_type=event_type,
                                schema_version=envelope.SCHEMA_VERSION, occurred_at=when,
                                payload=body))
            await ts.flush()
        metrics.record_ledger_event(event_type, "recorded")
        return event_id
    except Exception:
        logger.warning("ledger emit failed for %s", event_type, exc_info=True)
        try:
            from nexus.core import metrics as _metrics

            _metrics.record_ledger_event(event_type if isinstance(event_type, str) else "?",
                                         "failed")
        except Exception:
            pass
        return None
