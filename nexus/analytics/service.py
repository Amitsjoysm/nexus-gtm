"""Analytics & performance dashboards (tenant-scoped aggregates)."""
from __future__ import annotations

from sqlalchemy import func, select

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact
from nexus.models.intelligence import AccountScore, AgentRun
from nexus.models.signal import SignalEvent
from nexus.models.workflow import InboxTask, PlayRun


class AnalyticsService:
    async def overview(self, ts: TenantSession) -> dict:
        async def _count(model, *where) -> int:
            stmt = select(func.count()).select_from(model).where(
                model.tenant_id == ts.tenant_id, *where
            )
            return int((await ts.session.scalar(stmt)) or 0)

        avg_stmt = select(func.avg(AccountScore.composite)).where(
            AccountScore.tenant_id == ts.tenant_id
        )
        avg_composite = await ts.session.scalar(avg_stmt)

        return {
            "accounts": await _count(Account),
            "contacts": await _count(Contact),
            "signals": await _count(SignalEvent),
            "open_tasks": await _count(InboxTask, InboxTask.status == "open"),
            "agent_runs": await _count(AgentRun),
            "agent_failures": await _count(AgentRun, AgentRun.status == "failed"),
            "plays_executed": await _count(PlayRun),
            "avg_composite_score": round(float(avg_composite), 1) if avg_composite else 0.0,
        }


_service = AnalyticsService()


def get_analytics_service() -> AnalyticsService:
    return _service
