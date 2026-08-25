# nexus/core/audit.py
"""Audit trail for privileged, security-relevant actions.

**Two sinks, for two different readers, and both always fire.** ``record_audit`` writes a row to
``audit_log`` for the workspace admin who needs to review what changed in their workspace, and
emits the same event as one ``key=value`` line on the ``nexus.audit`` logger for the operator
grepping a deployment's log stream. Neither substitutes for the other: the table is
tenant-scoped and disappears with the tenant, the log stream is retained by infrastructure and
survives a database restore.

Two contracts that matter:

* **Never record a secret.** Callers pass booleans like ``token_set=True`` — never the token, a
  prefix of it, its length, or a hash.
* **Never break the action being audited.** ``record_audit`` swallows its own failures. Losing a
  credential change because its audit row would not serialise is exactly backwards; the payload is
  logged at ERROR so the evidence survives even when the row does not.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from nexus.core.tenancy import TenantSession

logger = logging.getLogger("nexus.audit")


def _render(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = "" if value is None else str(value)
    return f'"{text}"' if (" " in text or not text) else text


def _format(action: str, tenant_id: str, actor: str | None, fields: dict) -> str:
    parts = [f"action={action}", f"tenant={tenant_id}"]
    if actor:
        parts.append(f"actor={actor}")
    parts.extend(f"{k}={_render(v)}" for k, v in fields.items())
    return " ".join(parts)


def audit(action: str, *, tenant_id: str, actor: str | None = None, **fields) -> None:
    """Record one audited action, e.g.

    ``action=crm.connection.set tenant=abc actor=u1 provider=hubspot token_set=true``

    Never pass a secret in ``fields`` — pass a boolean saying whether one was supplied.
    """
    logger.info(_format(action, tenant_id, actor, fields))


def _jsonable(meta: dict | None) -> dict:
    """Coerce ``meta`` into something a JSON column will accept.

    ``default=str`` is what makes a datetime or a UUID survive instead of poisoning the whole
    write. A value that even that cannot handle raises here, and the caller turns it into a
    logged failure rather than a lost action.
    """
    return json.loads(json.dumps(meta or {}, default=str))


async def record_audit(
    ts: "TenantSession",
    action: str,
    *,
    actor_user_id: str | None = None,
    target_type: str = "",
    target_id: str = "",
    meta: dict | None = None,
) -> None:
    """Record one audited action to both sinks: the ``audit_log`` table and the log line.

    Never raises. An audit failure must not roll back the action it records, so a bad ``meta`` or
    a write error is logged at ERROR — with the payload, so the evidence survives — and swallowed.
    The row is added to the caller's session and lands on their flush/commit, which keeps the
    audit entry in the same transaction as the change it describes.
    """
    from nexus.models.audit import AuditLog

    payload = meta or {}
    try:
        ts.add(
            AuditLog(
                tenant_id=ts.tenant_id,
                action=action,
                actor_user_id=actor_user_id,
                target_type=target_type,
                target_id=target_id,
                meta=_jsonable(payload),
            )
        )
    except Exception:
        logger.error(
            "audit row could not be written: action=%s tenant=%s actor=%s meta=%r",
            action, ts.tenant_id, actor_user_id, payload, exc_info=True,
        )

    audit(action, tenant_id=ts.tenant_id, actor=actor_user_id, **payload)
