"""Where the engagement engine's OAuth apps and public endpoints come from (spec §12).

Two kinds of configuration, stored in two places on purpose:

* **Secrets** — the Google and Microsoft client secrets, the three ledger store connection strings
  and the pseudonymisation secret — live in the Control plane's provider keys (``google_oauth``,
  ``microsoft_oauth``, ``ledger_archive``, ``ledger_training``, ``ledger_insights``,
  ``ledger_pseudonym``): sealed, never returned, testable. The environment values are the floor the
  managed keys layer over, exactly as for every other provider.
* **Non-secret settings** — client ids, the Microsoft tenant, the Pub/Sub topic, the push service
  account and the public base URL — are runtime settings with validators, so an operator can see
  and change them without a redeploy.

Everything here is read per call. A value copied into a long-lived object is the "saved, applied
nothing" failure ``tests/test_runtime_control_plane.py`` exists to catch.

Nothing here invents a stand-in. An unconfigured provider reports exactly what is missing, and the
mailbox screen shows "not configured" rather than a connect button that cannot work (spec §12).
"""
from __future__ import annotations

from dataclasses import dataclass

from nexus.core.config import get_settings

GOOGLE = "google"
MICROSOFT = "microsoft"
MAILBOX_PROVIDERS = (GOOGLE, MICROSOFT)

#: Provider-key ids for the engagement secrets.
SECRET_KEY_IDS = {GOOGLE: "google_oauth", MICROSOFT: "microsoft_oauth"}
LEDGER_STORES = ("archive", "training", "insights")

#: The scopes each provider is asked for. Minimal for the product: read mail to detect replies,
#: compose drafts and send. Both Gmail scopes are "restricted" and need Google verification + CASA.
GOOGLE_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
)
MICROSOFT_SCOPES = (
    "openid",
    "email",
    "offline_access",
    "https://graph.microsoft.com/User.Read",
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/Mail.Send",
)


@dataclass(frozen=True, slots=True)
class OAuthApp:
    provider: str
    client_id: str
    client_secret: str
    tenant: str
    redirect_uri: str
    scopes: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def configured(self) -> bool:
        return not self.missing


def campaigns_enabled() -> bool:
    """The Release B switch: campaigns, the reply desk and their workers."""
    return bool(get_settings().engagement_campaigns_enabled)


def public_base_url() -> str:
    return (get_settings().engagement_public_base_url or "").strip().rstrip("/")


def redirect_uri(provider: str) -> str:
    base = public_base_url()
    return f"{base}/api/engagement/mailboxes/oauth/{provider}/callback" if base else ""


def gmail_push_audience() -> str:
    """The Pub/Sub push endpoint, which is also the audience its OIDC token must carry.

    **No secret of ours in this URL, deliberately.** A shared token on the push URL was the first
    design and ``tests/test_credential_leaks.py`` refuses it — rightly: a push endpoint is typed
    into a console, read back out of it, and logged by every proxy in front of us. The trust is the
    Google-signed OIDC token instead, verified in phase 09 against Google's certificates, this
    audience and the configured push service account. A deployment with no service account
    configured refuses the webhook rather than falling back to a weaker check, so removing the
    token removes a second factor that was never the one doing the work.
    """
    base = public_base_url()
    return f"{base}/api/engagement/webhooks/gmail" if base else ""


def gmail_push_service_account() -> str:
    return (get_settings().engagement_google_push_service_account or "").strip().lower()


def gmail_pubsub_topic() -> str:
    return (get_settings().engagement_google_pubsub_topic or "").strip()


def graph_notification_url() -> str:
    base = public_base_url()
    return f"{base}/api/engagement/webhooks/graph" if base else ""


async def secret(key_id: str) -> str:
    """The first usable key for ``key_id`` from the managed pool, else the environment. Never
    raises: an unreadable key store reads as "not configured", which the caller reports."""
    from nexus.providers.resolver import key_pool

    try:
        pool = await key_pool(key_id)
    except Exception:
        return ""
    return pool[0] if pool else ""


async def oauth_app(provider: str) -> OAuthApp:
    """Everything needed to run the OAuth flow for ``provider``, and what is missing if anything."""
    s = get_settings()
    if provider == GOOGLE:
        client_id = (s.engagement_google_client_id or "").strip()
        tenant, scopes = "", GOOGLE_SCOPES
    elif provider == MICROSOFT:
        client_id = (s.engagement_microsoft_client_id or "").strip()
        tenant = (s.engagement_microsoft_tenant or "").strip() or "common"
        scopes = MICROSOFT_SCOPES
    else:
        raise ValueError(f"unknown mailbox provider {provider!r}")
    client_secret = await secret(SECRET_KEY_IDS[provider])
    missing: list[str] = []
    if not public_base_url():
        missing.append("Public base URL (Runtime settings → Mailboxes & engagement)")
    if not client_id:
        missing.append(f"{provider.title()} client id (Runtime settings → Mailboxes & engagement)")
    if not client_secret:
        missing.append(f"{provider.title()} client secret (Provider keys → {SECRET_KEY_IDS[provider]})")
    return OAuthApp(
        provider=provider, client_id=client_id, client_secret=client_secret, tenant=tenant,
        redirect_uri=redirect_uri(provider), scopes=scopes, missing=tuple(missing),
    )


async def ledger_dsn(store: str) -> str:
    if store not in LEDGER_STORES:
        raise ValueError(f"unknown ledger store {store!r}")
    return await secret(f"ledger_{store}")


async def pseudonym_secret() -> str:
    return await secret("ledger_pseudonym")
