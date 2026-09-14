"""Analytics & performance dashboards (tenant-scoped aggregates)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import case, func, or_, select

from nexus.core.db import ensure_aware
from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact
from nexus.models.alerts import Alert
from nexus.models.billing import BillingUsageEvent
from nexus.models.intelligence import AccountScore, AgentRun
from nexus.models.signal import SignalEvent
from nexus.models.workflow import InboxTask, PlayRun

# Floor used only as a defensive sort key when a timestamp is somehow null; real rows always
# carry one. Keeps the merge-sort total even on malformed data instead of raising.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Activity feed is intentionally bounded: each source contributes at most `limit` recent rows,
# they merge in Python, and the newest `limit` survive. Caps the per-request work regardless of
# table size, so the endpoint stays cheap to poll even with millions of rows per tenant.
_ACTIVITY_MAX = 50

#: A usage capability that meters tokens alongside an action rather than being one. Counting it
#: would report a single research brief as hundreds of "actions".
_TOKEN_METER = "ai.tokens"


def _count(tid: str, model, *where):
    return (
        select(func.count())
        .select_from(model)
        .where(model.tenant_id == tid, *where)
        .scalar_subquery()
    )


def _avg_composite(tid: str):
    return (
        select(func.avg(AccountScore.composite))
        .where(AccountScore.tenant_id == tid)
        .scalar_subquery()
    )


class AnalyticsService:
    async def overview(self, ts: TenantSession) -> dict:
        """All dashboard KPIs in ONE database round trip.

        This is the hottest read in the app (every open dashboard polls it), so the eight
        aggregates ride a single SELECT of scalar subqueries instead of eight sequential
        queries — one network round trip and one transaction snapshot instead of eight.
        Portable: bare scalar-subquery SELECTs run identically on SQLite and Postgres."""
        tid = ts.tenant_id
        stmt = select(
            _count(tid, Account).label("accounts"),
            _count(tid, Contact).label("contacts"),
            _count(tid, SignalEvent).label("signals"),
            _count(tid, InboxTask, InboxTask.status == "open").label("open_tasks"),
            _count(tid, AgentRun).label("agent_runs"),
            _count(tid, AgentRun, AgentRun.status == "failed").label("agent_failures"),
            _count(tid, PlayRun).label("plays_executed"),
            _avg_composite(tid).label("avg_composite"),
        )
        row = (await ts.session.execute(stmt)).one()

        return {
            "accounts": int(row.accounts or 0),
            "contacts": int(row.contacts or 0),
            "signals": int(row.signals or 0),
            "open_tasks": int(row.open_tasks or 0),
            # Named "agent_runs" until it was reported as a bug: the dashboard renders each key
            # with `humanize()`, so this tile read "Agent Runs" directly beside a nav item called
            # "AI Runs" — and the two count different tables. This is `agent_runs`, one row per
            # individual agent execution (a research brief, a draft, a scoring pass). "AI Runs" is
            # `orchestration_runs`, one row per multi-agent orchestrator session.
            #
            # Neither number was ever wrong. A workspace that has scored accounts but never opened
            # the orchestrator genuinely has many agent actions and zero runs, which is exactly
            # what was seen (6 vs 0). Only the label lied, so only the label changed.
            "agent_actions": int(row.agent_runs or 0),
            "agent_failures": int(row.agent_failures or 0),
            "plays_executed": int(row.plays_executed or 0),
            "avg_composite_score": round(float(row.avg_composite), 1) if row.avg_composite else 0.0,
        }

    async def overview_for_user(self, ts: TenantSession, user_id: str) -> dict:
        """A rep's dashboard: their own queue and AI work, beside the book they share.

        The overview was manager-only while the dashboard every rep lands on called it anyway, so a
        rep's first screen was "Role 'rep' lacks view_analytics". This is what a rep can honestly be
        shown as *theirs* with the attribution that exists today:

        * ``open_tasks`` — tasks assigned to them plus unassigned ones, which is the queue they are
          expected to work. Assigned-only would read 0 for nearly everyone (plays rarely set an
          owner) beside an Inbox full of work; the whole workspace would count other reps' queues.
        * ``my_agent_actions`` — AI actions they ran, from the usage stream: ``AgentRun`` carries no
          user, and every metered AI call does. Refunded actions net out, since a refund means the
          action failed; the token meter is excluded because it measures an action rather than
          being one.

        Accounts, contacts, signals and the average fit are the shared book, and the same numbers
        the rep can already reach from the Accounts and Signals pages. Team health (agent failures,
        plays executed) is a manager's question and is left out rather than zeroed.
        """
        tid = ts.tenant_id
        net_actions = (
            select(func.coalesce(func.sum(case(
                (BillingUsageEvent.quantity > 0, 1),
                (BillingUsageEvent.quantity < 0, -1),
                else_=0,
            )), 0))
            .where(
                BillingUsageEvent.tenant_id == tid,
                BillingUsageEvent.user_id == user_id,
                BillingUsageEvent.capability_id.like("ai.%"),
                BillingUsageEvent.capability_id != _TOKEN_METER,
            )
            .scalar_subquery()
        )
        stmt = select(
            _count(tid, Account).label("accounts"),
            _count(tid, Contact).label("contacts"),
            _count(tid, SignalEvent).label("signals"),
            _count(
                tid, InboxTask,
                InboxTask.status == "open",
                or_(InboxTask.owner_user_id == user_id, InboxTask.owner_user_id.is_(None)),
            ).label("open_tasks"),
            net_actions.label("my_agent_actions"),
            _avg_composite(tid).label("avg_composite"),
        )
        row = (await ts.session.execute(stmt)).one()
        return {
            "accounts": int(row.accounts or 0),
            "contacts": int(row.contacts or 0),
            "signals": int(row.signals or 0),
            "open_tasks": int(row.open_tasks or 0),
            "my_agent_actions": max(0, int(row.my_agent_actions or 0)),
            "avg_composite_score": round(float(row.avg_composite), 1) if row.avg_composite else 0.0,
        }

    async def activity(self, ts: TenantSession, *, limit: int = 20) -> list[dict]:
        """Unified, newest-first feed of what just happened in this workspace.

        Merges the most recent rows from four sources (signals, alerts, account scores, agent
        runs) into one time-ordered stream — the "live" pulse the dashboard polls. Tenant-scoped
        by explicit filter (matching :meth:`overview`); never a global scan."""
        limit = max(1, min(limit, _ACTIVITY_MAX))
        items: list[dict] = []

        # Column-pruned reads: each source selects ONLY the fields the feed renders. The full
        # ORM rows would also drag Text bodies (signals, alerts) and JSON blobs (agent run
        # input/output) across the wire on every poll, plus per-row ORM construction.
        async def _recent(stmt):
            return (await ts.session.execute(stmt)).all()

        # Buying signals detected on the tenant's accounts.
        for s in await _recent(
            select(
                SignalEvent.id,
                SignalEvent.title,
                SignalEvent.kind,
                SignalEvent.account_id,
                SignalEvent.occurred_at,
                SignalEvent.strength,
            )
            .where(SignalEvent.tenant_id == ts.tenant_id)
            .order_by(SignalEvent.occurred_at.desc())
            .limit(limit)
        ):
            items.append({
                "id": f"signal:{s.id}",
                "kind": "signal",
                "title": s.title,
                "detail": s.kind.replace("_", " "),
                "account_id": s.account_id,
                "_at": ensure_aware(s.occurred_at),
                "tone": "success" if s.strength >= 0.66 else "info",
            })

        # Notifications raised by plays / agents / thresholds; tone tracks severity.
        _alert_tone = {"critical": "critical", "warning": "warning", "info": "info"}
        for a in await _recent(
            select(Alert.id, Alert.title, Alert.severity, Alert.account_id, Alert.created_at)
            .where(Alert.tenant_id == ts.tenant_id)
            .order_by(Alert.created_at.desc())
            .limit(limit)
        ):
            items.append({
                "id": f"alert:{a.id}",
                "kind": "alert",
                "title": a.title,
                "detail": a.severity,
                "account_id": a.account_id,
                "_at": ensure_aware(a.created_at),
                "tone": _alert_tone.get(a.severity, "info"),
            })

        # Account relevance (re)scored — the heartbeat of the scoring loop.
        for sc in await _recent(
            select(
                AccountScore.id,
                AccountScore.composite,
                AccountScore.icp_fit,
                AccountScore.intent,
                AccountScore.health,
                AccountScore.account_id,
                AccountScore.computed_at,
            )
            .where(AccountScore.tenant_id == ts.tenant_id)
            .order_by(AccountScore.computed_at.desc())
            .limit(limit)
        ):
            items.append({
                "id": f"score:{sc.id}",
                "kind": "account_scored",
                "title": f"Account scored {sc.composite}",
                "detail": f"ICP {sc.icp_fit} · Intent {sc.intent} · Health {sc.health}",
                "account_id": sc.account_id,
                "_at": ensure_aware(sc.computed_at),
                "tone": "success" if sc.composite >= 70 else "neutral",
            })

        # AI agent invocations; a failure is the one that wants attention.
        for r in await _recent(
            select(
                AgentRun.id,
                AgentRun.agent,
                AgentRun.status,
                AgentRun.error,
                AgentRun.account_id,
                AgentRun.created_at,
            )
            .where(AgentRun.tenant_id == ts.tenant_id)
            .order_by(AgentRun.created_at.desc())
            .limit(limit)
        ):
            failed = r.status == "failed"
            items.append({
                "id": f"run:{r.id}",
                "kind": "agent_run",
                "title": f"{r.agent.replace('_', ' ').title()} agent {'failed' if failed else 'ran'}",
                "detail": (r.error or r.status) if failed else r.status,
                "account_id": r.account_id,
                "_at": ensure_aware(r.created_at),
                "tone": "warning" if failed else "neutral",
            })

        items.sort(key=lambda x: x["_at"] or _EPOCH, reverse=True)
        items = items[:limit]

        # One batched lookup resolves account names for the surviving rows (no N+1).
        account_ids = {i["account_id"] for i in items if i["account_id"]}
        names: dict[str, str] = {}
        if account_ids:
            rows = (await ts.session.execute(
                select(Account.id, Account.name).where(
                    Account.tenant_id == ts.tenant_id, Account.id.in_(account_ids)
                )
            )).all()
            names = {aid: name for aid, name in rows}

        out: list[dict] = []
        for i in items:
            at = i.pop("_at")
            out.append({
                **i,
                "account_name": names.get(i["account_id"]) if i["account_id"] else None,
                "at": at.isoformat() if at else "",
            })
        return out


_service = AnalyticsService()


def get_analytics_service() -> AnalyticsService:
    return _service
