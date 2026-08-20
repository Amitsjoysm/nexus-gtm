# nexus/core/audit.py
"""Audit trail for privileged, security-relevant actions.

Deliberately a *log*, not a table: the events worth auditing today (a workspace admin changing an
integration credential) are low-volume and belong in the same stream as the rest of the platform's
operational logging, where a deployment's log shipper already retains them. One stable
``key=value`` line per event keeps it greppable and parseable.

The contract that matters: **this function never records a secret.** Callers pass booleans like
``token_set=True``, never the token, a prefix of it, its length, or a hash.
"""
from __future__ import annotations

import logging

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
