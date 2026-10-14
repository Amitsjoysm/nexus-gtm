"""Deleting a workspace's contribution, and erasing one person, from all three stores (spec §18.6).

Two requests, one shape: delete everywhere, then COUNT WHAT REMAINS and report it. A deletion that
reports what it did is a claim; a deletion that reports what is left is evidence, and the expected
answer is zero. The report is written to the audit log by the caller.

**Erasure deletes the archive rows, rather than blanking fields in them.** The archive holds the
email that was sent — the person's name is in the greeting and their address in the body — so
removing "identity fields" would leave the person's data in the text. The row is the unit that is
about them, and the training and insights rows are rebuilt from the archive, so removing it there is
what makes the deletion hold after any later rebuild.

An erasure also deletes the person's rows from the OUTBOX and is remembered (``erasures.py``): the
shipper deletes, rather than ships, any event that names them, and the builder skips one, so the
erasure holds for events still waiting, events recorded later, and any rebuild.
"""
from __future__ import annotations

import logging

from nexus.engagement.ledger.stores import STORES, StoreNotConfigured, connect

logger = logging.getLogger("nexus.engagement.ledger.deletion")


async def delete_workspace(tenant_id: str) -> dict:
    """Remove everything this workspace contributed. Idempotent."""
    from nexus.engagement import config
    from nexus.engagement.ledger.pseudonym import workspace_key

    report = {"tenant_id": tenant_id, "deleted": {}, "remaining": {}, "unconfigured": []}
    secret = await config.pseudonym_secret()
    if not secret:
        report["error"] = "the pseudonymisation secret is not configured"
        return report
    wkey = workspace_key(secret, tenant_id)

    await _outbox_delete(tenant_id, report)

    for store in STORES:
        try:
            async with connect(store) as conn:
                if store == "archive":
                    deleted = await conn.execute(
                        "DELETE FROM nexus_ledger.events WHERE tenant_id = $1", tenant_id)
                    remaining = await conn.fetchval(
                        "SELECT count(*) FROM nexus_ledger.events WHERE tenant_id = $1", tenant_id)
                elif store == "training":
                    deleted = await conn.execute(
                        "DELETE FROM nexus_ledger.events WHERE workspace_key = $1", wkey)
                    deleted += " " + await conn.execute(
                        "DELETE FROM nexus_ledger.examples WHERE workspace_key = $1", wkey)
                    remaining = await conn.fetchval(
                        "SELECT (SELECT count(*) FROM nexus_ledger.events WHERE workspace_key = $1)"
                        " + (SELECT count(*) FROM nexus_ledger.examples WHERE workspace_key = $1)",
                        wkey)
                else:
                    people, domains = await _insights_scope(conn, "workspace_key = $1", wkey)
                    deleted = await conn.execute(
                        "DELETE FROM nexus_ledger.facts WHERE workspace_key = $1", wkey)
                    await _reprofile(conn, people, domains)
                    remaining = await conn.fetchval(
                        "SELECT count(*) FROM nexus_ledger.facts WHERE workspace_key = $1", wkey)
                report["deleted"][store] = _rows(deleted)
                report["remaining"][store] = int(remaining or 0)
        except StoreNotConfigured:
            report["unconfigured"].append(store)
        except Exception as exc:
            report["remaining"][store] = -1
            report.setdefault("errors", {})[store] = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("ledger deletion failed for %s", store, exc_info=True)
    return report


async def erase_person(person_key: str, *, erasure_key: str = "",
                       known_event_ids: list[str] | None = None) -> dict:
    """Remove one person from the outbox and every store.

    * **Outbox** (when ``erasure_key`` is given): every row naming them, shipped or not. A shipped
      row is kept seven days with the email text in it; an unshipped one would carry them back into
      the archive.
    * **Archive**: every row whose ``person_keys`` holds theirs. Its event ids are collected, and
      kept in ``event_ids`` across retries, because the derived stores are cleared by them.
    * **Training and insights**: rows keyed to them, AND every row built from an archive row just
      deleted. A training example keyed to the contact still carries the body that named a Cc'd
      colleague, and a rebuild from the archive would no longer produce it, so it goes too.

    ``remaining`` counts by the same conditions afterwards; the expected answer is zero."""
    event_ids = sorted(set(known_event_ids or []))
    report = {"person_key": person_key, "deleted": {}, "remaining": {}, "unconfigured": [],
              "event_ids": event_ids}
    if erasure_key:
        await _outbox_erase(erasure_key, report)
    if not person_key:
        # No pseudonymisation secret when it was asked, so nothing was ever shipped under a key we
        # could search by: shipping needs that secret, and from now on this person is never shipped.
        report["stores_skipped"] = "no pseudonymisation secret, so nothing was shipped under a key"
        return report
    for store in STORES:
        try:
            async with connect(store) as conn:
                if store == "archive":
                    rows = await conn.fetch(
                        "DELETE FROM nexus_ledger.events WHERE person_keys @> ARRAY[$1]::text[] "
                        "RETURNING event_id", person_key)
                    event_ids = sorted(set(event_ids) | {r["event_id"] for r in rows})
                    report["event_ids"] = event_ids
                    deleted = f"DELETE {len(rows)}"
                    remaining = await conn.fetchval(
                        "SELECT count(*) FROM nexus_ledger.events "
                        "WHERE person_keys @> ARRAY[$1]::text[]", person_key)
                elif store == "training":
                    deleted = await conn.execute(
                        "DELETE FROM nexus_ledger.events WHERE person_keys @> ARRAY[$1]::text[] "
                        "OR event_id = ANY($2::text[])", person_key, event_ids)
                    deleted += " " + await conn.execute(
                        "DELETE FROM nexus_ledger.examples WHERE person_keys @> ARRAY[$1]::text[] "
                        "OR source_event_ids && $2::text[]", person_key, event_ids)
                    remaining = await conn.fetchval(
                        "SELECT (SELECT count(*) FROM nexus_ledger.events "
                        "        WHERE person_keys @> ARRAY[$1]::text[] "
                        "        OR event_id = ANY($2::text[])) "
                        "+ (SELECT count(*) FROM nexus_ledger.examples "
                        "   WHERE person_keys @> ARRAY[$1]::text[] "
                        "   OR source_event_ids && $2::text[])", person_key, event_ids)
                else:
                    where = "person_key = $1 OR event_id = ANY($2::text[])"
                    people, domains = await _insights_scope(conn, where, person_key, event_ids)
                    deleted = await conn.execute(
                        f"DELETE FROM nexus_ledger.facts WHERE {where}", person_key, event_ids)
                    deleted += " " + await conn.execute(
                        "DELETE FROM nexus_ledger.person_profiles WHERE person_key = $1",
                        person_key)
                    # Everyone else whose facts went, and the companies they worked at, are
                    # recomputed from what is left, so no profile keeps counting what is gone.
                    await _reprofile(conn, people, domains)
                    remaining = await conn.fetchval(
                        f"SELECT (SELECT count(*) FROM nexus_ledger.facts WHERE {where}) "
                        "+ (SELECT count(*) FROM nexus_ledger.person_profiles "
                        "   WHERE person_key = $1)", person_key, event_ids)
                report["deleted"][store] = _rows(deleted)
                report["remaining"][store] = int(remaining or 0)
        except StoreNotConfigured:
            report["unconfigured"].append(store)
        except Exception as exc:
            report["remaining"][store] = -1
            report.setdefault("errors", {})[store] = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("ledger erasure failed for %s", store, exc_info=True)
    return report


async def _outbox_erase(erasure_key: str, report: dict) -> None:
    """Delete every outbox row naming this person, then look again and count what is left.

    Rows are resolved exactly as the shipper resolves them, so "names this person" means the same
    thing here as at shipping. The second pass is the evidence; a row recorded between the two is
    counted rather than missed (and the shipper drops it anyway)."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.engagement.ledger import erasures
    from nexus.engagement.ledger import resolve as resolver
    from nexus.models.ledger import LedgerOutbox

    blocked = {erasure_key}

    async def sweep(delete: bool) -> int:
        found, last_id = 0, ""
        async with get_platform_sessionmaker()() as session:
            while True:
                rows = (await session.execute(
                    select(LedgerOutbox).where(LedgerOutbox.id > last_id)
                    .order_by(LedgerOutbox.id).limit(500)
                )).scalars().all()
                if not rows:
                    break
                for row in rows:
                    envelope = row.payload or {}
                    resolved = (await resolver.resolve(session, envelope)).as_dict()
                    if erasures.names_anyone(blocked, envelope, resolved):
                        found += 1
                        if delete:
                            await session.delete(row)
                last_id = rows[-1].id
                if delete:
                    await session.commit()
        return found

    report["deleted"]["outbox"] = await sweep(delete=True)
    report["remaining"]["outbox"] = await sweep(delete=False)


async def _insights_scope(conn, where: str, *values) -> tuple[list[str], list[str]]:
    """Whose profiles this delete will invalidate, read BEFORE the rows go."""
    rows = await conn.fetch(
        f"SELECT DISTINCT person_email, company_domain FROM nexus_ledger.facts WHERE {where}",
        *values)
    return ([r["person_email"] for r in rows if r["person_email"]],
            [r["company_domain"] for r in rows if r["company_domain"]])


async def _reprofile(conn, people: list[str], domains: list[str]) -> None:
    from nexus.engagement.ledger.builder import refresh_company, refresh_person

    for email in sorted(set(people)):
        await refresh_person(conn, email)
    for domain in sorted(set(domains)):
        await refresh_company(conn, domain)


async def _outbox_delete(tenant_id: str, report: dict) -> None:
    """Anything not yet shipped goes too — otherwise the next tick re-creates what we just deleted."""
    from sqlalchemy import delete

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.ledger import LedgerOutbox

    async with get_platform_sessionmaker()() as session:
        result = await session.execute(
            delete(LedgerOutbox).where(LedgerOutbox.tenant_id == tenant_id))
        await session.commit()
    report["deleted"]["outbox"] = int(result.rowcount or 0)
    report["remaining"]["outbox"] = 0


def _rows(status) -> int:
    """asyncpg returns ``"DELETE 12"``; several statements are joined with a space."""
    total = 0
    for part in str(status).split():
        if part.isdigit():
            total += int(part)
    return total
