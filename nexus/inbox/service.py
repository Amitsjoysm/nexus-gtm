"""Intelligent Inbox service: turn signals into prioritized, actionable tasks."""
from __future__ import annotations

from nexus.core.db import ensure_aware, utcnow
from nexus.core.tenancy import TenantSession
from nexus.inbox.prioritizer import compute_priority
from nexus.inbox.triage import TriageSummary, pick_contact, summarize
from nexus.models.account import Account, Contact
from nexus.models.signal import SignalEvent
from nexus.models.workflow import InboxTask


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
        task = InboxTask(
            tenant_id=ts.tenant_id,
            owner_user_id=owner_user_id,
            account_id=account.id,
            signal_id=signal.id,
            title=f"{signal.title}",
            reason=f"{signal.kind} signal · account score {composite_score if composite_score is not None else 'n/a'}",
            priority=priority,
            suggested_action=suggested_action or {"type": "review_account", "account_id": account.id},
        )
        ts.add(task)
        await ts.flush()
        return task

    async def list_open(
        self, ts: TenantSession, *, owner_user_id: str | None = None, limit: int = 50
    ) -> list[InboxTask]:
        where = [InboxTask.status == "open"]
        if owner_user_id:
            where.append(InboxTask.owner_user_id == owner_user_id)
        stmt = ts.select(InboxTask, *where).order_by(InboxTask.priority.desc()).limit(limit)
        return list((await ts.session.scalars(stmt)).all())

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


_service = InboxService()


def get_inbox_service() -> InboxService:
    return _service
