# nexus/api/routers/admin_engagement.py
"""What an operator pastes into Google Cloud and Azure, and whether each piece is in place (§12).

Setting up mailbox connection is two consoles the product does not control. Every value those
consoles ask for — redirect URIs, the Pub/Sub push endpoint and its OIDC audience, the Graph
notification URL, the scopes to request — is derived here from the public base URL, so the
operator copies it rather than composing it, and a typo cannot silently break the flow.

Gated on ``providers.manage``: the people who hold the client secrets are the people who set
these consoles up. No secret is in the response — ``configured`` booleans say whether one is
stored, and the push URL carries no token of ours (see ``config.gmail_push_audience``).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.permissions import PROVIDERS_MANAGE

router = APIRouter(prefix="/admin/engagement", tags=["admin-engagement"])


class MailboxAppOut(BaseModel):
    provider: str
    configured: bool
    missing: list[str]
    client_id: str
    tenant: str
    redirect_uri: str
    scopes: list[str]


class EngagementSetupOut(BaseModel):
    public_base_url: str
    campaigns_enabled: bool
    mailbox_apps: list[MailboxAppOut]
    gmail_pubsub_topic: str
    gmail_push_service_account: str
    gmail_push_audience: str
    graph_notification_url: str
    ledger_stores: dict[str, bool]
    pseudonym_secret_configured: bool


@router.get("/setup", response_model=EngagementSetupOut)
async def engagement_setup(
    _: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> EngagementSetupOut:
    from nexus.engagement import config

    apps = []
    for provider in config.MAILBOX_PROVIDERS:
        app = await config.oauth_app(provider)
        apps.append(MailboxAppOut(
            provider=provider, configured=app.configured, missing=list(app.missing),
            client_id=app.client_id, tenant=app.tenant, redirect_uri=app.redirect_uri,
            scopes=list(app.scopes),
        ))
    return EngagementSetupOut(
        public_base_url=config.public_base_url(),
        campaigns_enabled=config.campaigns_enabled(),
        mailbox_apps=apps,
        gmail_pubsub_topic=config.gmail_pubsub_topic(),
        gmail_push_service_account=config.gmail_push_service_account(),
        gmail_push_audience=config.gmail_push_audience(),
        graph_notification_url=config.graph_notification_url(),
        ledger_stores={s: bool(await config.ledger_dsn(s)) for s in config.LEDGER_STORES},
        pseudonym_secret_configured=bool(await config.pseudonym_secret()),
    )
