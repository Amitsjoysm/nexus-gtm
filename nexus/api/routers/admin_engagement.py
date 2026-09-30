# nexus/api/routers/admin_engagement.py
"""What an operator pastes into Google Cloud and Azure, and whether each piece is in place (§12).

Setting up mailbox connection is two consoles the product does not control. Every value those
consoles ask for — redirect URIs, the Pub/Sub push endpoint and its OIDC audience, the Graph
notification URL, the scopes to request — is derived here from the public base URL, so the
operator copies it rather than composing it, and a typo cannot silently break the flow.

Gated on ``providers.manage``: the people who hold the client secrets are the people who set
these consoles up. No secret is in the response — ``configured`` booleans say whether one is
stored, and the push URL carries no token of ours (see ``config.gmail_push_audience``).

**Editable here, not only readable** (2026-09-30). The screen used to say "set it under
Configuration" for the ids and "Provider keys" for the secrets, three tabs for one job, and the
operator who reported it could not find the first (it sat past the edge of the tab strip). ``PUT``
writes the six non-secret settings through the runtime-settings service, so the same validators
and the same 30s reach apply, and the two client secrets through the provider-key service, sealed
and never returned. ``campaigns_enabled`` is deliberately not writable here: it is the engine's
emergency stop and stays on Configuration beside its warning.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.audit import record_admin_action
from nexus.billing.permissions import PROVIDERS_MANAGE
from nexus.core.db import get_platform_sessionmaker

router = APIRouter(prefix="/admin/engagement", tags=["admin-engagement"])


class MailboxAppOut(BaseModel):
    provider: str
    configured: bool
    missing: list[str]
    client_id: str
    tenant: str
    redirect_uri: str
    scopes: list[str]
    # The last four characters of the secret in use, or "". Enough to say WHICH secret is live
    # (the one pasted yesterday, or last year's) without the secret ever leaving the server.
    secret_hint: str = ""


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


#: Form field -> runtime setting. The only settings this endpoint can write.
_SETTINGS = {
    "public_base_url": "engagement_public_base_url",
    "google_client_id": "engagement_google_client_id",
    "google_pubsub_topic": "engagement_google_pubsub_topic",
    "google_push_service_account": "engagement_google_push_service_account",
    "microsoft_client_id": "engagement_microsoft_client_id",
    "microsoft_tenant": "engagement_microsoft_tenant",
}


class EngagementSetupIn(BaseModel):
    """Every field optional: omitted (null) leaves it alone. For a setting, an empty string clears
    the override so the environment value applies again. For a secret, empty keeps what is stored,
    because a form that re-posts its blank password box must not erase the live secret."""

    model_config = {"extra": "forbid"}

    public_base_url: str | None = Field(default=None, max_length=500)
    google_client_id: str | None = Field(default=None, max_length=200)
    google_pubsub_topic: str | None = Field(default=None, max_length=300)
    google_push_service_account: str | None = Field(default=None, max_length=300)
    microsoft_client_id: str | None = Field(default=None, max_length=100)
    microsoft_tenant: str | None = Field(default=None, max_length=200)
    google_client_secret: str | None = Field(default=None, max_length=500)
    microsoft_client_secret: str | None = Field(default=None, max_length=500)
    note: str = Field(default="", max_length=500)


async def _secret_hint(key_id: str) -> str:
    """The hint of the key the resolver uses: pinned first, then oldest, enabled only."""
    from nexus.providers.service import list_keys

    live = [k for k in await list_keys(key_id) if k.enabled]
    return live[0].key_hint if live else ""


@router.get("/setup", response_model=EngagementSetupOut)
async def engagement_setup(
    _: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> EngagementSetupOut:
    return await _setup_out()


@router.put("/setup", response_model=EngagementSetupOut)
async def save_engagement_setup(
    body: EngagementSetupIn,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> EngagementSetupOut:
    """Save the mailbox apps. Every value is validated before any is written: a form that saved
    the Google half and then refused the Microsoft half would leave a state nobody asked for."""
    from nexus.core.config import get_settings
    from nexus.engagement import config
    from nexus.providers.crypto import key_digest
    from nexus.providers.service import DuplicateKey, add_key, list_keys, prefer_key
    from nexus.runtime_config.service import clear_override, set_override, validate

    fields = body.model_dump(exclude={"note"})
    to_set: dict[str, str] = {}
    to_clear: list[str] = []
    for field, key in _SETTINGS.items():
        value = fields.get(field)
        if value is None:
            continue
        value = value.strip()
        if not value:
            to_clear.append(key)
            continue
        try:
            validate(key, value)
        except ValueError as exc:
            label = field.replace("_", " ").capitalize()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{label}: {exc}") from exc
        to_set[key] = value

    live = get_settings()
    before: dict[str, object] = {k: getattr(live, k, "") for k in [*to_set, *to_clear]}
    after: dict[str, object] = {}
    for key, value in to_set.items():
        after[key] = await set_override(key, value, note=body.note, user_id=principal.user_id)
    for key in to_clear:
        await clear_override(key)
        after[key] = getattr(live, key, "")

    for provider in config.MAILBOX_PROVIDERS:
        secret = (fields.get(f"{provider}_client_secret") or "").strip()
        if not secret:
            continue
        key_id = config.SECRET_KEY_IDS[provider]
        before[key_id] = {"secret_hint": await _secret_hint(key_id)}
        try:
            row = await add_key(key_id, "Set from Mailbox apps", secret, user_id=principal.user_id)
        except DuplicateKey:
            digest = key_digest(secret)
            row = next(k for k in await list_keys(key_id) if k.key_digest == digest)
        # Pinned, so it is the one in use: `config.secret` takes the first of the pool, which
        # would otherwise stay the OLDEST key and ignore the one just pasted.
        await prefer_key(row.id)
        after[key_id] = {"secret_hint": row.key_hint}

    if after:
        async with get_platform_sessionmaker()() as session:
            await record_admin_action(
                session, actor=principal.user_id, action="engagement.setup",
                target="mailbox_apps", before=before, after=after, note=body.note,
            )
            await session.commit()
    return await _setup_out()


async def _setup_out() -> EngagementSetupOut:
    from nexus.engagement import config

    apps = []
    for provider in config.MAILBOX_PROVIDERS:
        app = await config.oauth_app(provider)
        apps.append(MailboxAppOut(
            provider=provider, configured=app.configured, missing=list(app.missing),
            client_id=app.client_id, tenant=app.tenant, redirect_uri=app.redirect_uri,
            scopes=list(app.scopes),
            secret_hint=await _secret_hint(config.SECRET_KEY_IDS[provider]),
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
