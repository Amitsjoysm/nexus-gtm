# nexus/integrations/settings.py
"""Deployment-level integration credentials (OAuth app registrations).

These are *our* app registrations with HubSpot / Salesforce / Outreach — one per deployment, not
per tenant. Per-tenant secrets live encrypted in ``integration_connections``; this file holds only
the client id/secret pair that identifies this product to the vendor, plus the public base URL the
vendor will redirect back to.

**Why this is not in ``nexus/core/config.py``:** that file has uncommitted work in it and is out of
scope for this change. This class deliberately mirrors ``Settings`` exactly — same ``NEXUS_``
prefix, same ``.env`` file, same ``extra="ignore"`` — so a value resolves identically either way,
and merging it back is a paste of the field block plus a rename of the accessor. Reading these with
``os.getenv`` instead would have missed every value set in ``.env``, which is how the project
configures local development. ``tests/test_integration_settings.py`` pins the two configs together
so they cannot drift.

Everything defaults to empty, which means **inert**: the OAuth endpoints report "not configured"
rather than half-building an authorize URL the vendor would reject with an opaque error.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class IntegrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEXUS_", env_file=".env", extra="ignore")

    # Public base URL of this deployment, e.g. https://app.example.com. OAuth callbacks are built
    # from it. A provider rejects any redirect_uri not registered on the app, so this must match
    # the vendor console exactly — including scheme and the absence of a trailing slash.
    oauth_redirect_base: str = ""

    hubspot_client_id: str = ""
    hubspot_client_secret: str = ""

    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    # login.salesforce.com for production orgs; test.salesforce.com for sandboxes. The token
    # response carries the real instance_url, which is what subsequent REST calls must use — this
    # host is only where the authorization dance happens.
    salesforce_login_base: str = "https://login.salesforce.com"

    outreach_client_id: str = ""
    outreach_client_secret: str = ""


@lru_cache
def get_integration_settings() -> IntegrationSettings:
    return IntegrationSettings()
