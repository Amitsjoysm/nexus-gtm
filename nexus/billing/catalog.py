# nexus/billing/catalog.py
"""The capability catalog: the declarative registry of everything billable.

This module is the single source of truth for WHAT can be metered. Pricing lives on rate cards
and plans (docs/billing/04-Pricing-Engine.md); this file only declares the capability, its unit,
and its safe default mode.

Every entry ships as ``default_mode="shadow"`` or ``"enabled"`` — nothing blocks on first deploy
(docs/billing/15-Migration-Strategy.md §1). Turning a capability into a real gate is an Admin
action, never a code change.

Adding a new billable feature = add one row here (or via the Admin API) and call the metering
seam. That is the entire engineering cost of monetizing something new.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from nexus.core.db import get_sessionmaker
from nexus.models.billing import BillingCapability

logger = logging.getLogger("nexus.billing.catalog")


def _cap(
    id: str, category: str, name: str, *, sub_category: str = "", unit: str = "action",
    meter_kind: str = "counter", default_mode: str = "shadow", description: str = "",
    depends_on: list[str] | None = None,
) -> dict:
    return {
        "id": id, "category": category, "sub_category": sub_category, "name": name,
        "description": description, "unit": unit, "meter_kind": meter_kind,
        "default_mode": default_mode, "depends_on": depends_on or [],
    }


# ---- module gates: coarse on/off switches other capabilities depend on ----------------------
_MODULES = [
    _cap("module.outreach", "module", "Outreach module", default_mode="enabled",
         description="Campaigns, cadences, sending."),
    _cap("module.calling", "module", "Calling module", default_mode="enabled",
         description="Call queue, AI scripts, dispositions."),
    _cap("module.network", "module", "Relationship graph module", default_mode="enabled",
         description="Personal network search and warm intros."),
    _cap("module.discovery", "module", "Discovery module", default_mode="enabled",
         description="ICP auto-discovery and look-alikes."),
    _cap("module.integrations", "module", "Integrations module", default_mode="enabled",
         description="CRM and sales-engagement connectors."),
    _cap("module.api", "module", "Public API access", default_mode="enterprise",
         description="Programmatic API access."),
]

# ---- platform & seats ----------------------------------------------------------------------
_PLATFORM = [
    _cap("seat.member", "platform", "User seat", unit="seat", meter_kind="gauge",
         default_mode="metered", description="Billed as seat-days."),
    _cap("platform.workspace", "platform", "Workspace", default_mode="enabled"),
    _cap("platform.storage", "platform", "Stored data", unit="gb", meter_kind="gauge",
         default_mode="metered", description="Measured nightly."),
    _cap("platform.custom_fields", "platform", "Custom field definitions", default_mode="enabled"),
    _cap("api.request", "platform", "API request", sub_category="api", unit="request",
         description="Blanket middleware meter over every HTTP request."),
    _cap("job.queue_execution", "platform", "Background job execution", unit="job",
         description="Blanket meter over every queue handler."),
]

# ---- AI -------------------------------------------------------------------------------------
_AI = [
    _cap("ai.tokens", "ai", "LLM tokens", unit="token",
         description="Raw token meter from the LLM chokepoint; COGS truth."),
    _cap("ai.research_brief", "ai", "AI research brief", sub_category="research",
         default_mode="metered"),
    _cap("ai.email_draft", "ai", "AI email draft", sub_category="outreach",
         default_mode="metered", depends_on=["module.outreach"]),
    _cap("ai.account_qa", "ai", "Ask about this account", sub_category="research",
         default_mode="metered"),
    _cap("ai.scoring", "ai", "ICP fit scoring", sub_category="scoring"),
    _cap("ai.contact_rank", "ai", "Contact recommendation", sub_category="research",
         default_mode="metered"),
    _cap("ai.call_script", "ai", "AI call script", sub_category="calling",
         default_mode="metered", depends_on=["module.calling"]),
    _cap("ai.icp_from_website", "ai", "AI ICP from website", sub_category="relevance",
         default_mode="metered"),
    _cap("ai.chat_turn", "ai", "Orchestrator chat turn", sub_category="orchestration",
         default_mode="metered"),
    _cap("ai.personalization_fetch", "ai", "Person social insights", sub_category="personalization",
         default_mode="metered"),
    _cap("ai.premium_model", "ai", "Premium model routing", sub_category="routing",
         default_mode="enterprise",
         description="Route to a frontier model; multiplies credit cost."),
    _cap("workflow.orchestration_run", "workflow", "Orchestration run", unit="run",
         default_mode="metered"),
    _cap("workflow.orchestration_step", "workflow", "Orchestration step", unit="job"),
]

# ---- search / discovery / enrichment --------------------------------------------------------
_DISCOVERY = [
    _cap("search.web", "search", "Web search", unit="search",
         description="Exa/Brave/Serper/DuckDuckGo call."),
    _cap("discovery.icp_daily", "discovery", "Daily ICP discovery run", unit="job",
         default_mode="metered", depends_on=["module.discovery"]),
    _cap("discovery.account_added", "discovery", "Net-new ICP account", default_mode="metered",
         depends_on=["module.discovery"]),
    _cap("discovery.lookalike_company", "discovery", "Company look-alikes", unit="run",
         default_mode="metered", depends_on=["module.discovery"]),
    _cap("discovery.lookalike_contact", "discovery", "Contact look-alikes", unit="run",
         default_mode="metered", depends_on=["module.discovery"]),
    _cap("enrich.account", "enrich", "Account enrichment", default_mode="metered"),
    _cap("enrich.contact", "enrich", "Contact enrichment", default_mode="metered"),
    _cap("enrich.source_committee", "enrich", "Source buying committee", default_mode="metered"),
    _cap("enrich.linkedin_finder", "enrich", "LinkedIn URL finder", default_mode="metered"),
    _cap("verify.email", "enrich", "Email verification", unit="check", default_mode="metered"),
    _cap("signal.news_scan", "signal", "News signal scan", unit="job"),
    _cap("signal.rss_scan", "signal", "RSS signal scan", unit="job"),
    _cap("signal.stored", "signal", "Signal stored"),
]

# ---- outreach & workflow --------------------------------------------------------------------
_OUTREACH = [
    _cap("outreach.email_send", "outreach", "Email sent", unit="message",
         default_mode="metered", depends_on=["module.outreach"]),
    _cap("outreach.email_draft_save", "outreach", "Draft saved to mailbox", unit="message",
         default_mode="metered", depends_on=["module.outreach"]),
    _cap("outreach.campaign", "outreach", "Campaign launched", unit="run",
         default_mode="metered", depends_on=["module.outreach"]),
    _cap("outreach.cadence_touch", "outreach", "Cadence touch", default_mode="metered",
         depends_on=["module.outreach"]),
    _cap("outreach.sep_push", "outreach", "Sales-engagement push", default_mode="metered",
         depends_on=["module.integrations"]),
    _cap("calling.task", "calling", "Call task", default_mode="enabled",
         depends_on=["module.calling"]),
    _cap("calling.brief", "calling", "Pre-call brief", default_mode="metered",
         depends_on=["module.calling"]),
    _cap("calling.minutes", "calling", "Telephony minutes", unit="minute",
         default_mode="enterprise", depends_on=["module.calling"]),
    _cap("automation.play_run", "automation", "Play executed", unit="job",
         default_mode="metered"),
    _cap("automation.account_refresh", "automation", "Account refresh cycle", unit="job"),
    _cap("inbox.task", "workflow", "Inbox task created"),
]

# ---- network (relationship graph) -----------------------------------------------------------
_NETWORK = [
    _cap("network.source_sync", "network", "Network source sync", unit="job",
         default_mode="metered", depends_on=["module.network"]),
    _cap("network.linkedin_import", "network", "LinkedIn export import", unit="job",
         default_mode="metered", depends_on=["module.network"]),
    _cap("network.search", "network", "Network search", unit="search",
         default_mode="metered", depends_on=["module.network"]),
    _cap("network.intro_paths", "network", "Warm intro paths", default_mode="enabled",
         depends_on=["module.network"]),
    _cap("network.persons", "network", "Graph persons stored", meter_kind="gauge"),
]

# ---- integrations, notifications, data ------------------------------------------------------
_INTEGRATIONS = [
    _cap("integration.crm_sync", "integration", "CRM record sync", default_mode="metered",
         depends_on=["module.integrations"]),
    _cap("integration.crm_connection", "integration", "CRM connection", default_mode="enabled",
         depends_on=["module.integrations"]),
    _cap("notify.in_app", "notify", "In-app notification", unit="message"),
    _cap("notify.webhook", "notify", "Webhook delivery", unit="message", default_mode="metered"),
    _cap("notify.slack", "notify", "Slack notification", unit="message", default_mode="metered"),
    _cap("notify.email_digest", "notify", "Email digest", unit="message", default_mode="metered"),
    _cap("data.import_csv", "data", "CSV import", unit="job", default_mode="metered"),
    _cap("data.export", "data", "Data export", unit="job", default_mode="metered"),
    _cap("report.cadence", "report", "Cadence report", default_mode="enabled"),
    _cap("report.analytics", "report", "Analytics dashboard", default_mode="enabled"),
]

CAPABILITY_SEED: list[dict] = (
    _MODULES + _PLATFORM + _AI + _DISCOVERY + _OUTREACH + _NETWORK + _INTEGRATIONS
)

# Fields sync_catalog() keeps authoritative from code. `name`/`description` are intentionally
# NOT in this list: admins may reword customer-facing copy without a deploy, and a redeploy must
# not silently revert their edits.
_MANAGED_FIELDS = ("category", "sub_category", "unit", "meter_kind", "depends_on")


async def sync_catalog() -> dict:
    """Upsert the declarative seed into ``billing_capabilities``. Idempotent.

    ``default_mode`` is applied on INSERT only: once a capability exists, its mode is owned by
    the Admin portal (flipping shadow -> enforced is an operator decision, and a redeploy must
    never silently re-arm or disarm a gate).
    """
    created = updated = 0
    async with get_sessionmaker()() as session:
        existing = {
            c.id: c for c in (await session.scalars(select(BillingCapability))).all()
        }
        for spec in CAPABILITY_SEED:
            row = existing.get(spec["id"])
            if row is None:
                session.add(BillingCapability(**spec))
                created += 1
                continue
            changed = False
            for field in _MANAGED_FIELDS:
                if getattr(row, field) != spec[field]:
                    setattr(row, field, spec[field])
                    changed = True
            if changed:
                updated += 1
        await session.commit()
    if created or updated:
        logger.info("catalog sync: %d created, %d updated", created, updated)
    return {"created": created, "updated": updated, "total": len(CAPABILITY_SEED)}
