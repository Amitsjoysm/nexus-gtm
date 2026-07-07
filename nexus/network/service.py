"""Graph ingest + visibility helpers.

``ingest_batch`` idempotently folds a connector's NetworkSyncBatch into the graph: upsert raw
identities, resolve canonical persons, then materialize exactly one edge per
(owner_member, person, provider) with deterministic strength + aggregated touchpoint stats.
``visible_edges_where`` is the single privacy predicate used by every cross-member read.

String fields from providers are clamped to their column widths at this boundary (never rejected)
so one oversized title/name can't fail a whole sync batch — mirrors nexus/ingestion/service.py.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import or_, update

from nexus.core.db import ensure_aware, utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.network import (
    NetworkEdge,
    NetworkIdentity,
    NetworkSourceAccount,
)
from nexus.network.connectors.base import NetworkSyncBatch
from nexus.network.resolution import resolution_key, resolve_person
from nexus.network.strength import EdgeStats, score_edge


def _clamp(value: str | None, limit: int) -> str | None:
    """Truncate a provider string to its column width; None stays None."""
    if value is None:
        return None
    return value[:limit]


def visible_edges_where(member_id: str):
    """An edge is visible to a member if they own it OR it is pooled."""
    return or_(
        NetworkEdge.owner_member_id == member_id,
        NetworkEdge.pooling_enabled.is_(True),
    )


def _aggregate_touchpoints(batch: NetworkSyncBatch) -> dict[str, dict]:
    agg: dict[str, dict] = defaultdict(
        lambda: {"sent": 0, "received": 0, "meetings": 0, "first": None, "last": None}
    )
    for tp in batch.touchpoints:
        a = agg[tp.person_external_id]
        if tp.kind == "email_sent":
            a["sent"] += 1
        elif tp.kind == "email_received":
            a["received"] += 1
        elif tp.kind == "meeting":
            a["meetings"] += 1
        at = ensure_aware(tp.at)
        if at is not None:
            a["first"] = at if a["first"] is None else min(a["first"], at)
            a["last"] = at if a["last"] is None else max(a["last"], at)
    return agg


async def ingest_batch(
    ts: TenantSession,
    account: NetworkSourceAccount,
    batch: NetworkSyncBatch,
    *,
    now: datetime | None = None,
) -> dict:
    now = now or utcnow()
    agg = _aggregate_touchpoints(batch)
    new_persons = 0
    new_edges = 0

    for raw in batch.identities:
        email_c = _clamp(raw.email, 255)
        name_c = _clamp(raw.name, 200)
        title_c = _clamp(raw.title, 200)
        company_c = _clamp(raw.company, 200)
        handle_c = _clamp(raw.handle, 100)

        person = await resolve_person(
            ts, email=email_c, name=name_c, title=title_c, company=company_c
        )

        ident = await ts.first(
            NetworkIdentity,
            NetworkIdentity.source_account_id == account.id,
            NetworkIdentity.external_id == raw.external_id,
        )
        if ident is None:
            ts.add(
                NetworkIdentity(
                    source_account_id=account.id, person_id=person.id, provider=account.provider,
                    external_id=raw.external_id, email=email_c, name=name_c, title=title_c,
                    company=company_c, handle=handle_c, raw=raw.raw,
                    resolution_key=resolution_key(
                        email=email_c, name=name_c, company=company_c
                    ),
                )
            )
            person.identity_count += 1
            new_persons += 1
        else:
            ident.person_id = person.id

        a = agg.get(raw.external_id, {})
        sent, received, meetings = a.get("sent", 0), a.get("received", 0), a.get("meetings", 0)
        email_count = sent + received
        if email_count:
            relation = "email"
        elif meetings:
            relation = "calendar"
        else:
            relation = raw.relation
        stats = EdgeStats(
            relation=relation, email_count=email_count, sent_count=sent,
            received_count=received, meeting_count=meetings, last_touch_at=a.get("last"),
        )
        strength = score_edge(stats, now=now)

        edge = await ts.first(
            NetworkEdge,
            NetworkEdge.owner_member_id == account.member_id,
            NetworkEdge.person_id == person.id,
            NetworkEdge.provider == account.provider,
        )
        if edge is None:
            ts.add(
                NetworkEdge(
                    owner_member_id=account.member_id, owner_user_id=account.user_id,
                    person_id=person.id, source_account_id=account.id, provider=account.provider,
                    relation=relation, strength=strength, email_count=email_count, sent_count=sent,
                    received_count=received, meeting_count=meetings, first_touch_at=a.get("first"),
                    last_touch_at=a.get("last"), pooling_enabled=account.pooling_enabled,
                )
            )
            person.edge_count += 1
            new_edges += 1
        else:
            edge.relation = relation
            edge.strength = strength
            edge.email_count = email_count
            edge.sent_count = sent
            edge.received_count = received
            edge.meeting_count = meetings
            edge.first_touch_at = a.get("first")
            edge.last_touch_at = a.get("last")
            edge.pooling_enabled = account.pooling_enabled

    account.sync_cursor = batch.next_cursor
    account.last_synced_at = now
    account.status = "connected"
    await ts.flush()
    return {"identities": len(batch.identities), "new_persons": new_persons, "new_edges": new_edges}


async def set_pooling(ts: TenantSession, account: NetworkSourceAccount, enabled: bool) -> None:
    """Toggle pooling on a source account and mirror it onto its edges (drives visibility)."""
    account.pooling_enabled = enabled
    await ts.session.execute(
        update(NetworkEdge)
        .where(
            NetworkEdge.tenant_id == ts.tenant_id,
            NetworkEdge.source_account_id == account.id,
        )
        .values(pooling_enabled=enabled)
    )
    await ts.flush()
