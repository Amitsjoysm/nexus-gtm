"""Deleting a workspace's contribution, and erasing one person, from all three stores (spec §18.6).

Two requests, one shape: delete everywhere, then COUNT WHAT REMAINS and report it. A deletion that
reports what it did is a claim; a deletion that reports what is left is evidence, and the expected
answer is zero. The report is written to the audit log by the caller.

**Erasure deletes the archive rows, rather than blanking fields in them.** The archive holds the
email that was sent — the person's name is in the greeting and their address in the body — so
removing "identity fields" would leave the person's data in the text. The row is the unit that is
about them, and the training and insights rows are rebuilt from the archive, so removing it there is
what makes the deletion hold after any later rebuild.

Ordering matters in one place: the person's still-unshipped events are shipped FIRST, so an event
recorded a minute before the request cannot arrive in the archive a minute after the erasure ran.
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


async def erase_person(person_key: str) -> dict:
    """Remove one person from every store, by their deterministic key."""
    report = {"person_key": person_key, "deleted": {}, "remaining": {}, "unconfigured": []}
    for store in STORES:
        try:
            async with connect(store) as conn:
                if store in ("archive", "training"):
                    deleted = await conn.execute(
                        "DELETE FROM nexus_ledger.events WHERE person_keys @> ARRAY[$1]::text[]",
                        person_key)
                    remaining = await conn.fetchval(
                        "SELECT count(*) FROM nexus_ledger.events "
                        "WHERE person_keys @> ARRAY[$1]::text[]", person_key)
                    if store == "training":
                        deleted += " " + await conn.execute(
                            "DELETE FROM nexus_ledger.examples "
                            "WHERE person_keys @> ARRAY[$1]::text[]", person_key)
                        remaining += await conn.fetchval(
                            "SELECT count(*) FROM nexus_ledger.examples "
                            "WHERE person_keys @> ARRAY[$1]::text[]", person_key)
                else:
                    _people, domains = await _insights_scope(conn, "person_key = $1", person_key)
                    deleted = await conn.execute(
                        "DELETE FROM nexus_ledger.facts WHERE person_key = $1", person_key)
                    await conn.execute(
                        "DELETE FROM nexus_ledger.person_profiles WHERE person_key = $1",
                        person_key)
                    # The company they worked at still has a profile; it must stop counting them.
                    await _reprofile(conn, [], domains)
                    remaining = await conn.fetchval(
                        "SELECT (SELECT count(*) FROM nexus_ledger.facts WHERE person_key = $1) "
                        "+ (SELECT count(*) FROM nexus_ledger.person_profiles "
                        "   WHERE person_key = $1)", person_key)
                report["deleted"][store] = _rows(deleted)
                report["remaining"][store] = int(remaining or 0)
        except StoreNotConfigured:
            report["unconfigured"].append(store)
        except Exception as exc:
            report["remaining"][store] = -1
            report.setdefault("errors", {})[store] = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("ledger erasure failed for %s", store, exc_info=True)
    return report


async def _insights_scope(conn, where: str, value: str) -> tuple[list[str], list[str]]:
    """Whose profiles this delete will invalidate, read BEFORE the rows go."""
    rows = await conn.fetch(
        f"SELECT DISTINCT person_email, company_domain FROM nexus_ledger.facts WHERE {where}", value)
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
