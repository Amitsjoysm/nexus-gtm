"""API router registry."""
from nexus.api.routers import (
    accounts,
    agents,
    alerts,
    auth,
    integrations,
    orchestration,
    relevance,
    signals,
    workflow,
    workspace,
)

all_routers = [
    auth.router,
    relevance.router,
    accounts.router,
    agents.router,
    workflow.router,
    alerts.router,
    integrations.router,
    workspace.router,
    signals.router,
    orchestration.router,
]

__all__ = ["all_routers"]
