"""Which Twilio account a workspace's calls go out on — its own, or the platform's.

Decided with the product owner 2026-09-23: "both, like CRM". Before this, telephony was the
deployment's NEXUS_TWILIO_* env vars: every customer's calls went out on our account under one caller
ID, no screen could change it, and nothing charged for the minutes.

``resolve_call_provider(ts)`` is the one thing the dial, disposition and status paths call.
Precedence, mirroring ``resolve_crm_connector``:

1. an installed override (``set_call_provider`` — the test seam), treated as the platform's;
2. the workspace's own connection (``integration_connections``, kind ``telephony``);
3. the platform account: a Provider key ``twilio`` (``ACsid:authtoken``) when a superadmin has set
   ``telephony_provider`` to ``twilio``, else the environment exactly as before;
4. click-to-dial.

**Only the platform account is charged in credits** (``source == "platform"``). A workspace on its
own Twilio pays Twilio directly; charging it again would bill one call twice.

**A workspace connection that cannot be read never falls back to the platform.** Falling back
would ring on our caller ID and bill the customer's credits after they chose their own account —
the CRM rule that a person's action must not silently land somewhere else. It raises instead,
naming the fix.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from nexus.calling.provider import CallProvider, TelephonyNotConfigured, get_call_provider, get_override
from nexus.core.tenancy import TenantSession
from nexus.integrations import connections
from nexus.models.integration import IntegrationConnection

logger = logging.getLogger("nexus.calling.connection")

KIND = "telephony"
_SECRET_FIELDS = ("account_sid", "auth_token")


@dataclass(slots=True)
class ResolvedTelephony:
    provider: CallProvider
    from_number: str
    # "workspace": the workspace's own Twilio (no credits). "platform": ours (charged in credits).
    # "none": click-to-dial, nothing is placed and nothing is charged.
    source: str


async def get_connection(ts: TenantSession) -> IntegrationConnection | None:
    return await connections.get_connection(ts, KIND)


def caller_id_of(row: IntegrationConnection | None) -> str:
    return str(((row.config if row is not None else None) or {}).get("from_number") or "")


def _build_twilio(account_sid: str, auth_token: str):
    from nexus.calling.twilio import TwilioCallProvider, TwilioSettings

    s = TwilioSettings()
    return TwilioCallProvider(
        account_sid=account_sid, auth_token=auth_token, api_base=s.twilio_api_base,
        timeout=s.twilio_timeout_s, record_calls=s.twilio_record_calls,
    )


def build_workspace_provider(row: IntegrationConnection):
    """The workspace's own Twilio. Raises ``TelephonyNotConfigured`` when it cannot be read."""
    bundle = connections.secret_bundle(row)
    if not all(bundle.get(f) for f in _SECRET_FIELDS):
        raise TelephonyNotConfigured(
            "Your workspace's Twilio connection could not be read. An admin needs to reconnect it "
            "under Integrations."
        )
    return _build_twilio(bundle["account_sid"], bundle["auth_token"])


async def platform_provider() -> CallProvider | None:
    """The platform's Twilio from a Provider key, or ``None`` to use the environment as before.

    Only when a superadmin has switched calling to Twilio. The key alone is not a switch: an
    operator adding a credential to test it must not make every workspace start placing live,
    billed calls.
    """
    from nexus.core.config import get_settings

    if (get_settings().telephony_provider or "").strip().lower() != "twilio":
        return None
    try:
        from nexus.providers import resolver

        pool = await resolver.managed_pool("twilio")
    except Exception:  # key management must never break the call it exists to serve
        logger.warning("could not read the platform Twilio key; using the environment")
        return None
    for key in pool:
        try:
            from nexus.calling.twilio import parse_credential

            return _build_twilio(*parse_credential(key))
        except (ValueError, TelephonyNotConfigured):
            continue
    return None


async def platform_caller_id(ts: TenantSession) -> str:
    """This workspace's number on the platform account: its assignment, else the platform default."""
    from nexus.core.config import get_settings
    from nexus.models.identity import Tenant

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    assigned = (getattr(tenant, "platform_caller_id", None) or "").strip()
    return assigned or (get_settings().telephony_from_number or "").strip()


async def platform_call_provider() -> CallProvider:
    """The platform's own provider, whatever any workspace has connected.

    For the work that is about the PLATFORM account in particular: the sweep charging calls placed
    on it, and checking a caller ID a superadmin assigns from it.
    """
    override = get_override()
    if override is not None:
        return override
    return await platform_provider() or get_call_provider()


async def resolve_call_provider(ts: TenantSession) -> ResolvedTelephony:
    platform_number = await platform_caller_id(ts)

    override = get_override()
    if override is not None:
        return ResolvedTelephony(override, platform_number, "platform")

    row = await get_connection(ts)
    if row is not None:
        return ResolvedTelephony(build_workspace_provider(row), caller_id_of(row), "workspace")

    managed = await platform_provider()
    if managed is not None:
        return ResolvedTelephony(managed, platform_number, "platform")

    provider = get_call_provider()
    live = provider.name != "stub"
    return ResolvedTelephony(provider, platform_number if live else "", "platform" if live else "none")


async def store(
    ts: TenantSession, *, account_sid: str, auth_token: str, from_number: str,
    actor_user_id: str | None,
) -> IntegrationConnection:
    """Save the workspace's own Twilio. A blank SID or token keeps the stored one (write-only)."""
    existing = await get_connection(ts)
    bundle = dict(connections.secret_bundle(existing))
    if account_sid:
        bundle["account_sid"] = account_sid
    if auth_token:
        bundle["auth_token"] = auth_token
    row = await connections.store_credentials(
        ts, kind=KIND, provider="twilio", secret=bundle, actor_user_id=actor_user_id,
    )
    row.config = {**(row.config or {}), "from_number": from_number}
    await ts.flush()
    return row


def account_hint(row: IntegrationConnection | None) -> str:
    """The SID's last four, so an admin can tell WHICH account is connected. Never the whole SID."""
    sid = str(connections.secret_bundle(row).get("account_sid") or "") if row is not None else ""
    return f"AC...{sid[-4:]}" if sid else ""
