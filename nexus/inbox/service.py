"""Intelligent Inbox service: turn signals into prioritized, actionable tasks."""
from __future__ import annotations

from nexus.core.db import ensure_aware, utcnow
from nexus.core.tenancy import TenantSession
from nexus.inbox.prioritizer import compute_priority
from nexus.inbox.triage import TriageSummary, pick_contact, summarize
from nexus.models.account import Account, Contact
from nexus.models.signal import SignalEvent
from nexus.models.workflow import InboxTask

# Human task labels per signal kind, so the Inbox reads as account work — "Brex: New funding" —
# not a raw news headline ("Banking News and Analysis | Banking Dive").
_KIND_LABEL = {
    "funding": "New funding — reach out",
    "hiring": "Hiring / leadership change",
    "job_posting": "Relevant hiring",
    "job_switch": "Champion changed roles",
    "g2_intent": "Active buying intent",
    "tech_install": "Adopted a relevant tech",
    "web_visit": "Visited your site",
    "product_usage": "Product activity",
    "news": "Company news",
    "call": "Follow up on call",
}


class InboxService:
    async def create_from_signal(
        self,
        ts: TenantSession,
        signal: SignalEvent,
        account: Account,
        *,
        composite_score: int | None = None,
        owner_user_id: str | None = None,
        suggested_action: dict | None = None,
    ) -> InboxTask:
        age_days = max(
            (utcnow() - ensure_aware(signal.occurred_at)).total_seconds() / 86400.0, 0.0
        )
        priority = compute_priority(
            signal_strength=signal.strength,
            composite_score=composite_score,
            age_days=age_days,
        )
        title = f"{account.name}: {_KIND_LABEL.get(signal.kind, signal.kind)}"
        # The raw headline becomes the supporting context, not the task title itself.
        reason = f"{signal.title} · account score {composite_score if composite_score is not None else 'n/a'}"
        action = suggested_action or {"type": "review_account", "account_id": account.id}

        # Idempotent per signal: if this exact signal already produced a task (open OR done),
        # never create a second one. So re-processing won't duplicate, and a signal the rep
        # already actioned and marked complete is not re-alerted.
        if signal.id:
            dup = (
                await ts.session.scalars(
                    ts.select(InboxTask, InboxTask.signal_id == signal.id).limit(1)
                )
            ).first()
            if dup is not None:
                return dup

        # One OPEN task per account: the account, not each headline, is the unit of work.
        # Collapse multiple qualifying signals into a single inbox row and keep it pointed at the
        # strongest signal — this is what stops the same account repeating many times.
        open_task = (
            await ts.session.scalars(
                ts.select(
                    InboxTask,
                    InboxTask.account_id == account.id,
                    InboxTask.status == "open",
                )
                .order_by(InboxTask.priority.desc())
                .limit(1)
            )
        ).first()
        if open_task is not None:
            if priority >= open_task.priority:
                open_task.signal_id = signal.id
                open_task.title = title
                open_task.reason = reason
                open_task.priority = priority
                open_task.suggested_action = action
            await ts.flush()
            return open_task

        task = InboxTask(
            tenant_id=ts.tenant_id,
            owner_user_id=owner_user_id,
            account_id=account.id,
            signal_id=signal.id,
            title=title,
            reason=reason,
            priority=priority,
            suggested_action=action,
        )
        ts.add(task)
        await ts.flush()
        return task

    async def list_tasks(
        self,
        ts: TenantSession,
        *,
        owner_user_id: str | None = None,
        status: str = "open",
        limit: int = 50,
    ) -> list[InboxTask]:
        """List tasks by status so an SDR can recover their pending/previous work, not just
        today's open items. ``status`` is 'open', 'done', or 'all'."""
        where = []
        if status in ("open", "done", "snoozed"):
            where.append(InboxTask.status == status)
        if owner_user_id:
            where.append(InboxTask.owner_user_id == owner_user_id)
        stmt = ts.select(InboxTask, *where).order_by(InboxTask.priority.desc()).limit(limit)
        return list((await ts.session.scalars(stmt)).all())

    async def list_open(
        self, ts: TenantSession, *, owner_user_id: str | None = None, limit: int = 50
    ) -> list[InboxTask]:
        return await self.list_tasks(ts, owner_user_id=owner_user_id, status="open", limit=limit)

    async def triage(
        self, ts: TenantSession, tasks: list[InboxTask]
    ) -> dict[str, TriageSummary]:
        """Build a glanceable triage rollup per task.

        Batch-loads the linked signals, accounts, and their contacts (no N+1) and folds
        each task's records into a :class:`TriageSummary`. Pure read; no verification calls.
        """
        if not tasks:
            return {}
        signal_ids = {t.signal_id for t in tasks if t.signal_id}
        account_ids = {t.account_id for t in tasks if t.account_id}

        signals: dict[str, SignalEvent] = {}
        if signal_ids:
            rows = await ts.list(SignalEvent, SignalEvent.id.in_(signal_ids))
            signals = {s.id: s for s in rows}

        accounts: dict[str, Account] = {}
        contacts_by_account: dict[str, list[Contact]] = {}
        if account_ids:
            arows = await ts.list(Account, Account.id.in_(account_ids))
            accounts = {a.id: a for a in arows}
            crows = await ts.list(Contact, Contact.account_id.in_(account_ids))
            for c in crows:
                contacts_by_account.setdefault(c.account_id, []).append(c)

        now = utcnow()
        out: dict[str, TriageSummary] = {}
        for t in tasks:
            signal = signals.get(t.signal_id) if t.signal_id else None
            account = accounts.get(t.account_id) if t.account_id else None
            contact = (
                pick_contact(contacts_by_account.get(t.account_id, []))
                if t.account_id
                else None
            )
            out[t.id] = summarize(
                t, signal=signal, account=account, contact=contact, now=now
            )
        return out

    async def complete(self, ts: TenantSession, task_id: str) -> InboxTask | None:
        task = await ts.get(InboxTask, task_id)
        if task is None:
            return None
        task.status = "done"
        await ts.flush()
        return task

    async def reopen(self, ts: TenantSession, task_id: str) -> InboxTask | None:
        """Mark a completed task incomplete again (done -> open) so it returns to the list."""
        task = await ts.get(InboxTask, task_id)
        if task is None:
            return None
        task.status = "open"
        await ts.flush()
        return task


_service = InboxService()


def get_inbox_service() -> InboxService:
    return _service
