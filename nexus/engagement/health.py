"""Whether campaigns are moving, read from what the worker leaves behind (Platform health).

Everything after a person approves an opening email runs in the worker: the first send, every
follow-up, the re-engagement a "later" reply earned, and reading replies. When any of that stops,
the campaign screen looks exactly like a quiet week: nothing happens, and nothing says why. A worker
that is not running, the switch left off, and a mailbox the provider will not let us read are three
different fixes, so this names which one it is.

Nothing here asks the worker anything. It reads the evidence the worker's work leaves in the
database, which the API can see whichever process it runs in:

* a step due more than ``OVERDUE_AFTER`` ago in a running campaign was not sent. The advance job
  runs every tick, and everything that legitimately waits (a daily limit, a mailbox paused by the
  provider, a quality hold) moves the due time forward or changes the status, so only a job that is
  not running, or one that raises on that enrollment every time, leaves it in the past.
* a connected mailbox not read for ``UNREAD_AFTER`` is not having its replies picked up. The poll
  reads every connected mailbox every five minutes.

Cross-tenant, so it reads on the platform sessionmaker: under the RLS-bound app role it would count
zero of everything and report a healthy, idle platform.
"""
from __future__ import annotations

from datetime import datetime, timedelta

OK, DEGRADED, UNCONFIGURED, ERROR = "ok", "degraded", "unconfigured", "error"

#: Past this, a due step is overdue rather than queued behind a busy tick.
OVERDUE_AFTER = timedelta(minutes=30)
#: Past this, a connected mailbox has missed several five-minute polls.
UNREAD_AFTER = timedelta(minutes=30)
#: How far back a failed send is still worth mentioning.
FAILED_WINDOW = timedelta(days=7)

_BROKEN_MAILBOX = ("needs_reauth", "error")


def _age(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 120:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h" if hours < 48 else f"{hours // 24}d"


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"{n} {word}"
    return f"{n} {word}es" if word.endswith("x") else f"{n} {word}s"


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    from datetime import timezone

    return value.replace(tzinfo=timezone.utc)


async def engine_health(now: datetime | None = None) -> tuple[str, str]:
    """``(status, detail)`` for the campaigns engine. The worst finding sets the status, and every
    finding is in the detail."""
    from sqlalchemy import and_, func, or_, select

    from nexus.core.db import get_platform_sessionmaker, utcnow
    from nexus.engagement import config
    from nexus.models.engagement import (
        EngagementCampaign,
        EngagementEnrollment,
        EngagementMessage,
        MailboxConnection,
    )

    now = now or utcnow()
    late = now - OVERDUE_AFTER
    async with get_platform_sessionmaker()() as session:
        running = int(await session.scalar(
            select(func.count()).select_from(EngagementCampaign)
            .where(EngagementCampaign.status == "active")) or 0)
        mailboxes = (await session.execute(select(
            MailboxConnection.status, MailboxConnection.last_synced_at,
            MailboxConnection.created_at, MailboxConnection.last_error,
            MailboxConnection.updated_at))).all()
        overdue = (await session.execute(
            select(func.count(), func.min(func.coalesce(EngagementEnrollment.next_action_at,
                                                        EngagementEnrollment.snoozed_until)))
            .select_from(EngagementEnrollment)
            .join(EngagementCampaign, EngagementCampaign.id == EngagementEnrollment.campaign_id)
            .where(EngagementCampaign.status == "active")
            .where(or_(
                and_(EngagementEnrollment.status == "active",
                     EngagementEnrollment.next_action_at < late),
                # A "later" reply's date, or an out-of-office return, that has come and gone: the
                # same two cases `advance.due_enrollments` wakes.
                and_(EngagementEnrollment.status == "snoozed",
                     EngagementEnrollment.snoozed_until < late),
                and_(EngagementEnrollment.status == "paused",
                     EngagementEnrollment.status_reason == "out_of_office",
                     EngagementEnrollment.snoozed_until < late),
            )))).one()
        failed = (await session.execute(
            select(EngagementMessage.error)
            .where(EngagementMessage.direction == "out")
            .where(EngagementMessage.status == "failed")
            .where(EngagementMessage.updated_at >= now - FAILED_WINDOW)
            .order_by(EngagementMessage.updated_at.desc()))).scalars().all()

    findings: list[tuple[str, str]] = []
    if not config.campaigns_enabled():
        if running:
            findings.append((ERROR, (
                "switched off (Runtime settings: engagement_campaigns_enabled) while "
                f"{_plural(running, 'running campaign')} wait: nothing is sent, followed up or "
                "read")))
        else:
            return UNCONFIGURED, "switched off (Runtime settings: engagement_campaigns_enabled)"
    if not mailboxes:
        return UNCONFIGURED, "no mailbox connected yet: nobody can create a campaign"

    overdue_count, oldest = int(overdue[0] or 0), _aware(overdue[1])
    if overdue_count:
        age = f", the oldest by {_age(now - oldest)}" if oldest else ""
        findings.append((ERROR, (
            f"{_plural(overdue_count, 'email')} overdue{age}: the worker is not sending them "
            "(is it running, and is it failing on them? see its log)")))

    connected = [m for m in mailboxes if m.status == "connected"]
    unread = []
    for m in connected:
        seen = _aware(m.last_synced_at) or _aware(m.created_at)
        if seen is not None and seen < now - UNREAD_AFTER:
            unread.append(seen)
    if unread:
        findings.append((ERROR, (
            f"{_plural(len(unread), 'connected mailbox')} not read for {_age(now - min(unread))}"
            ": replies are not being picked up")))

    broken = [m for m in mailboxes if m.status in _BROKEN_MAILBOX]
    if broken:
        newest = max(broken, key=lambda m: _aware(m.updated_at) or now)
        findings.append((DEGRADED, (
            f"{_plural(len(broken), 'mailbox')} need reconnecting or fixing"
            + (f" ({newest.last_error})" if newest.last_error else ""))))
    faulty = [m for m in connected if m.last_error]
    if faulty:
        newest = max(faulty, key=lambda m: _aware(m.updated_at) or now)
        findings.append((DEGRADED, f"{_plural(len(faulty), 'mailbox')} report: {newest.last_error}"))
    if failed:
        findings.append((DEGRADED, (
            f"{_plural(len(failed), 'send')} failed in the last 7 days"
            + (f", latest: {failed[0][:200]}" if failed[0] else ""))))

    if not findings:
        reads = [_aware(m.last_synced_at) for m in connected if m.last_synced_at]
        read = f", read {_age(now - max(reads))} ago" if reads else ""
        return OK, (f"{_plural(running, 'running campaign')}, "
                    f"{_plural(len(connected), 'mailbox')} connected{read}, nothing overdue")
    status = next((s for s in (ERROR, DEGRADED) if any(f[0] == s for f in findings)), OK)
    return status, "; ".join(text for _s, text in findings)
