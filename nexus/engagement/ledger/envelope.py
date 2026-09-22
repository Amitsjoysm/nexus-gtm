"""The ledger event envelope (spec §18.1): one JSON shape for every event, schema-versioned.

The event types are a closed list. A typo in an ``emit`` call is dropped and counted rather than
creating a new family nobody's dataset builder reads, which is how a training set quietly loses
the events it was built for.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

SCHEMA_VERSION = 1

EVENT_TYPES: frozenset[str] = frozenset({
    # AI
    "ai.call", "research.completed",
    # drafting and review (phase 08)
    "draft.created", "draft.edited", "draft.approved", "draft.rejected",
    # sending (phase 07)
    "message.sent", "message.bounced",
    # replies (phases 09-10)
    "reply.received", "reply.classified", "reply.corrected", "reply.decided",
    "response.drafted", "response.sent",
    # enrollments (phase 08)
    "enrollment.started", "enrollment.paused", "enrollment.snoozed", "enrollment.resumed",
    "enrollment.stopped", "enrollment.completed",
    # the rest of the product (this phase)
    "call.disposition", "signal.ingested", "account.scored", "enrichment.account",
    "enrichment.contact", "outcome.recorded", "credits.charged", "workflow.run_finished",
    "audit.action", "contact.suppressed", "contact.unsuppressed", "consent.changed",
    "error.job_failed",
})

#: Per-string cap. A research brief or a transcript is kept; a scraped page is not.
MAX_TEXT = 20_000
#: Whole-payload cap after the per-string cap. Beyond it the payload is replaced by a marker, so one
#: pathological event cannot bloat the outbox or a store batch.
MAX_PAYLOAD_BYTES = 256_000


def _bounded(value, depth: int = 0):
    if depth > 8:
        return "[nested too deep]"
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…[truncated]"
    if isinstance(value, dict):
        return {str(k): _bounded(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bounded(v, depth + 1) for v in list(value)[:500]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def app_version() -> str:
    return os.environ.get("NEXUS_APP_VERSION", "")


def build(
    *, event_id: str, event_type: str, tenant_id: str, occurred_at: datetime,
    actor_user_id: str | None = None, actor_role: str | None = None, refs: dict | None = None,
    correlation_id: str | None = None, causation_id: str | None = None, model: str | None = None,
    prompt_version: str | None = None, flags: dict | None = None, payload: dict | None = None,
) -> dict:
    """A JSON-safe envelope. Pure: the same inputs build the same dict."""
    body = _bounded(payload or {})
    if len(json.dumps(body, default=str)) > MAX_PAYLOAD_BYTES:
        body = {"truncated": True, "reason": "payload over the ledger size cap"}
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": SCHEMA_VERSION,
        "occurred_at": occurred_at.isoformat(),
        "tenant_id": tenant_id,
        "actor": {"user_id": actor_user_id, "role": actor_role},
        "refs": {k: v for k, v in (refs or {}).items() if v},
        "chain": {"correlation_id": correlation_id or event_id, "causation_id": causation_id},
        "context": {"app_version": app_version(), "model": model or "",
                    "prompt_version": prompt_version or "", "flags": flags or {}},
        "payload": body,
    }
