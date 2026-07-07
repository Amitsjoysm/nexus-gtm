"""API router registry."""
from nexus.api.routers import (
    accounts,
    agents,
    alerts,
    auth,
    cadences,
    calling,
    campaigns,
    chat,
    contacts,
    custom_fields,
    integrations,
    network,
    orchestration,
    outcomes,
    relevance,
    signals,
    workflow,
    workspace,
)

all_routers = [
    auth.router,
    relevance.router,
    accounts.router,
    contacts.router,
    agents.router,
    workflow.router,
    campaigns.router,
    cadences.router,
    calling.router,
    alerts.router,
    integrations.router,
    network.router,
    workspace.router,
    signals.router,
    orchestration.router,
    chat.router,
    custom_fields.router,
    outcomes.router,
]

__all__ = ["all_routers"]
