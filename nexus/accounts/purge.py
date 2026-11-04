# nexus/accounts/purge.py
"""Delete accounts for good, with their contacts (SDR round 2, 5.5; owner decision 2026-10-29).

Everywhere else an account is archived, never removed (`merge.py`, the single `DELETE`), because
signals, tasks, enrolments and call activity point at it. Bulk delete is the one place a person
asked for the opposite: the accounts and their contacts leave the workspace. So every row that
points at them has to be decided, and a reference left behind is either a foreign-key failure on
Postgres or a row pointing at nothing (SQLite does not check, which is why the test scans for it).

Two parts, and the split is the safety:

* ``OWNED`` is EXPLICIT: the rows that belong to an account or to one of its contacts and go with
  it — its scores, signals, alerts, Inbox tasks, list memberships, call tasks and calls, finished
  campaign enrolments, website snapshots, the legacy campaign and cadence rows.
* Everything else is DERIVED from the models: any other row that points at something being deleted
  keeps the row and loses the pointer, when the column allows it. That keeps what is somebody
  else's record — an AI run and its charge, a conversation, an agent action, a play run, an outcome,
  a do-not-contact block (it holds the address), the mailbox's own threads, messages and replies.
  A pointer that may not be cleared (NOT NULL) and is not in ``OWNED`` refuses the whole delete
  (`PurgeIncomplete`), so a table added later cannot be stranded or silently emptied: someone
  decides, and `test_bulk_delete` names it.

Only the caller's workspace: every statement filters on ``tenant_id`` (RLS is the second layer).
The shared ``companies`` row is never touched here; the caller links it first, so the master
record outlives the account (`companies.backfill.link_accounts`).
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select, update

logger = logging.getLogger("nexus.accounts.purge")

#: (table, column, parent): rows of ``table`` whose ``column`` points at a ``parent`` row being
#: deleted are deleted too. In dependency order, so a parent's set is known before its children's.
OWNED: tuple[tuple[str, str, str], ...] = (
    ("contacts", "account_id", "accounts"),
    ("account_scores", "account_id", "accounts"),
    # Its fit against each further service (W8, 0077): a score of the account, NOT NULL, so left
    # out it refused every delete in a workspace that had scored a second service.
    ("account_service_fits", "account_id", "accounts"),
    ("page_snapshots", "account_id", "accounts"),
    ("signal_events", "account_id", "accounts"),
    ("alerts", "account_id", "accounts"),
    ("inbox_tasks", "account_id", "accounts"),
    ("list_items", "account_id", "accounts"),
    ("list_items", "contact_id", "contacts"),
    ("campaign_targets", "account_id", "accounts"),
    ("cadence_enrollments", "account_id", "accounts"),
    ("cadence_touches", "enrollment_id", "cadence_enrollments"),
    ("engagement_enrollments", "account_id", "accounts"),
    ("engagement_enrollments", "contact_id", "contacts"),
    ("call_tasks", "account_id", "accounts"),
    ("call_activities", "account_id", "accounts"),
)

#: A campaign still sending (or about to) and an enrolment it is still working. Deleting a person a
#: campaign is about to email would leave it emailing nobody, or failing every tick.
RUNNING_CAMPAIGN = ("reviewing", "active", "paused")
LIVE_ENROLMENT = ("awaiting_review", "active", "paused", "snoozed")

_CHUNK = 500


class PurgeIncomplete(RuntimeError):
    """A row that must point at something being deleted, and is not deleted with it."""


def _chunks(values) -> list[list[str]]:
    values = sorted(values)
    return [values[i:i + _CHUNK] for i in range(0, len(values), _CHUNK)]


async def in_running_campaigns(ts, account_ids: list[str]) -> dict[str, list[str]]:
    """``{account_id: [campaign name, ...]}`` for the accounts someone in a running campaign is at."""
    from nexus.models.engagement import EngagementCampaign, EngagementEnrollment

    out: dict[str, list[str]] = {}
    for chunk in _chunks(set(account_ids)):
        rows = (await ts.session.execute(
            select(EngagementEnrollment.account_id, EngagementCampaign.name)
            .join(EngagementCampaign, EngagementCampaign.id == EngagementEnrollment.campaign_id)
            .where(EngagementEnrollment.tenant_id == ts.tenant_id,
                   EngagementCampaign.tenant_id == ts.tenant_id,
                   EngagementEnrollment.account_id.in_(chunk),
                   EngagementEnrollment.status.in_(LIVE_ENROLMENT),
                   EngagementCampaign.status.in_(RUNNING_CAMPAIGN))
        )).all()
        for account_id, name in rows:
            names = out.setdefault(account_id, [])
            if name not in names:
                names.append(name)
    return out


async def _ids(ts, table, column, parents) -> set[str]:
    out: set[str] = set()
    pk = next(iter(table.primary_key.columns))
    for chunk in _chunks(parents):
        rows = await ts.session.execute(
            select(pk).where(table.c.tenant_id == ts.tenant_id, column.in_(chunk)))
        out.update(r[0] for r in rows.all())
    return out


async def plan(ts, account_ids: list[str]) -> dict[str, set[str]]:
    """Every row the delete removes, by table: the accounts, then what ``OWNED`` hands down."""
    import nexus.models  # noqa: F401  (every mapper, so the derived half sees every table)
    from nexus.core.db import Base

    tables = Base.metadata.tables
    doomed: dict[str, set[str]] = {"accounts": set(account_ids)}
    for table_name, column, parent in OWNED:
        parents = doomed.get(parent)
        if not parents:
            continue
        table = tables[table_name]
        doomed.setdefault(table_name, set()).update(
            await _ids(ts, table, table.c[column], parents))
    return doomed


async def purge_accounts(ts, account_ids: list[str]) -> dict[str, int]:
    """Delete ``account_ids`` (this workspace's) and what belongs to them. Returns rows deleted per
    table. Raises `PurgeIncomplete` before anything is changed when a reference cannot be decided.
    The caller owns the transaction: a raise leaves it to roll back."""
    from nexus.core.db import Base

    ids = sorted(set(account_ids))
    if not ids:
        return {}
    doomed = await plan(ts, ids)

    # Decide every reference into the rows going, before writing anything.
    clear: list[tuple] = []
    for table in Base.metadata.sorted_tables:
        for fk in table.foreign_keys:
            target = fk.column.table.name
            if not doomed.get(target):
                continue
            column = fk.parent
            if "tenant_id" not in table.c:
                raise PurgeIncomplete(f"{table.name}.{column.name} is not workspace-scoped")
            if column.nullable:
                clear.append((table, column, doomed[target]))
                continue
            pointing = await _ids(ts, table, column, doomed[target])
            stranded = pointing - doomed.get(table.name, set())
            if stranded:
                raise PurgeIncomplete(
                    f"{len(stranded)} {table.name} row(s) must point at {target} being deleted "
                    f"({table.name}.{column.name}); add the table to OWNED or make it nullable")

    for table, column, targets in clear:
        for chunk in _chunks(targets):
            await ts.session.execute(
                update(table).where(table.c.tenant_id == ts.tenant_id, column.in_(chunk))
                .values({column.name: None}).execution_options(synchronize_session=False))

    counts: dict[str, int] = {}
    for table in reversed(Base.metadata.sorted_tables):
        rows = doomed.get(table.name)
        if not rows:
            continue
        pk = next(iter(table.primary_key.columns))
        for chunk in _chunks(rows):
            await ts.session.execute(
                delete(table).where(table.c.tenant_id == ts.tenant_id, pk.in_(chunk))
                .execution_options(synchronize_session=False))
        counts[table.name] = len(rows)
    logger.info("deleted %d accounts for good in %s: %s", len(ids), ts.tenant_id, counts)
    return counts
