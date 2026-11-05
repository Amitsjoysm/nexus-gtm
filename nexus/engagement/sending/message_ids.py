"""Put right the Message-IDs stored for emails sent before 2026-11-05 (`service._mark_sent`).

Until then the stored Message-ID of a sent email was the one we wrote, not the one the provider
sent (Gmail replaces it; Outlook assigns its own). A follow-up answers the newest message in its
thread by that stored id, so every follow-up's In-Reply-To and References named a message the
recipient never received, and their client opened a new thread for it. New sends store the right
id; this corrects the rows already sent, so the next follow-up of a sequence already running lands
in the recipient's thread too. It also rewrites the reply headers stored on later rows that copied
an old id, so a draft approved after the repair is sent with the corrected chain.

Reports, and writes only when asked. Never raises for one message: an email the provider no longer
has (deleted from Sent) keeps its id and is counted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("nexus.engagement.sending.message_ids")


@dataclass(slots=True)
class Repair:
    checked: int = 0
    #: ``{stored id: the id the provider sent}`` for every one that differed.
    changed: dict[str, str] = field(default_factory=dict)
    unreadable: int = 0
    #: Rows whose In-Reply-To or References named an old id.
    headers_rewritten: int = 0


def _rewrite(chain: str, mapping: dict[str, str]) -> str:
    return " ".join(mapping.get(part, part) for part in (chain or "").split())


async def repair_mailbox(ts, mailbox, provider, *, apply: bool, limit: int = 2000) -> Repair:
    """Check every sent email of one mailbox against its provider, and with ``apply`` correct the
    stored Message-ID and the reply headers that copied it."""
    from nexus.models.engagement import EngagementMessage

    out = Repair()
    sent = await ts.list(EngagementMessage, EngagementMessage.mailbox_connection_id == mailbox.id,
                         EngagementMessage.direction == "out",
                         EngagementMessage.status == "sent",
                         EngagementMessage.provider_message_id.is_not(None))
    for row in sorted(sent, key=lambda r: r.created_at)[:limit]:
        out.checked += 1
        try:
            real = (await provider.rfc_message_id_of(row.provider_message_id) or "").strip()
        except Exception:  # noqa: BLE001 - one unreadable message must not stop the rest
            logger.warning("could not read the Message-ID of %s", row.provider_message_id,
                           exc_info=True)
            real = ""
        if not real:
            out.unreadable += 1
            continue
        if real != (row.rfc_message_id or ""):
            if row.rfc_message_id:
                out.changed[row.rfc_message_id] = real
            if apply:
                row.rfc_message_id = real
    if out.changed:
        everything = await ts.list(EngagementMessage,
                                   EngagementMessage.mailbox_connection_id == mailbox.id)
        for row in everything:
            reply_to = out.changed.get(row.in_reply_to or "", row.in_reply_to)
            refs = _rewrite(row.references_header or "", out.changed)
            if reply_to != row.in_reply_to or refs != (row.references_header or ""):
                out.headers_rewritten += 1
                if apply:
                    row.in_reply_to, row.references_header = reply_to, refs
    if apply:
        await ts.flush()
    return out
