"""Test buttons for the engagement secrets in Provider keys (spec §12).

**An OAuth client secret cannot be proven by a successful call** until a person has authorised the
app, and nobody has on the day an operator pastes the secret in. What CAN be proven is that the
provider recognises the client: both token endpoints authenticate the client BEFORE they look at the
authorization code. So each check presents a deliberately invalid code:

* the client is valid → the provider rejects the CODE (``invalid_grant`` / ``invalid_request``),
  reported as ``probe_ok`` "valid client, not yet authorised by a user";
* the secret is wrong, expired or the app does not exist → the provider rejects the CLIENT
  (``invalid_client`` / ``unauthorized_client``; Azure codes 7000215, 7000222, 700016),
  reported as ``failed`` with the provider's own words.

A ledger store's connection string is tested by connecting, behind the same SSRF guard source
databases use, and refusing a superuser or Supabase's all-powerful ``postgres`` role: each store gets
a role scoped to its job (spec §18.2), and a check that passed an admin connection would bless the
setup the guide tells the operator not to use.

The pseudonymisation secret never leaves the process, so its check is local: long and varied enough
to key an HMAC.

A transport failure never condemns a credential ("could not reach"), matching ``providers/testing``.
"""
from __future__ import annotations

import asyncio

import httpx

from nexus.providers.testing import TestResult

_TIMEOUT = 20.0
_PROBE_CODE = "nexus-credential-check"

#: Azure AD error codes that mean the CLIENT is wrong rather than the code.
_AZURE_BAD_CLIENT = {7000215, 7000222, 700016, 700023, 7000218}

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _body(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _describe(body: dict, resp: httpx.Response) -> str:
    text = body.get("error_description") or body.get("error") or resp.text or ""
    return str(text).strip()[:300]


async def check_google_client(
    client_secret: str, *, client_id: str, redirect_uri: str, transport=None,
) -> TestResult:
    if not client_id:
        return TestResult(False, "failed", "set the Google client id in Runtime settings first")
    if not redirect_uri:
        return TestResult(False, "failed", "set the public base URL in Runtime settings first")
    form = {
        "grant_type": "authorization_code", "code": _PROBE_CODE, "client_id": client_id,
        "client_secret": client_secret, "redirect_uri": redirect_uri,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=transport) as http:
            resp = await http.post(_GOOGLE_TOKEN_URL, data=form)
    except Exception as exc:
        return TestResult(False, "failed", f"could not reach Google: {type(exc).__name__}", None)
    body = _body(resp)
    error = str(body.get("error") or "")
    if resp.status_code == 400 and error in ("invalid_grant", "invalid_request"):
        return TestResult(True, "probe_ok",
                          "valid client, not yet authorised by a user", resp.status_code)
    return TestResult(False, "failed", _describe(body, resp), resp.status_code)


async def check_microsoft_client(
    client_secret: str, *, client_id: str, tenant: str, redirect_uri: str, transport=None,
) -> TestResult:
    if not client_id:
        return TestResult(False, "failed", "set the Microsoft client id in Runtime settings first")
    if not redirect_uri:
        return TestResult(False, "failed", "set the public base URL in Runtime settings first")
    url = f"https://login.microsoftonline.com/{tenant or 'common'}/oauth2/v2.0/token"
    form = {
        "grant_type": "authorization_code", "code": _PROBE_CODE, "client_id": client_id,
        "client_secret": client_secret, "redirect_uri": redirect_uri,
        "scope": "https://graph.microsoft.com/Mail.Send offline_access",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=transport) as http:
            resp = await http.post(url, data=form)
    except Exception as exc:
        return TestResult(False, "failed",
                          f"could not reach Microsoft: {type(exc).__name__}", None)
    body = _body(resp)
    codes = {int(c) for c in (body.get("error_codes") or []) if str(c).isdigit()}
    error = str(body.get("error") or "")
    if (resp.status_code == 400 and error in ("invalid_grant", "invalid_request")
            and not codes & _AZURE_BAD_CLIENT):
        return TestResult(True, "probe_ok",
                          "valid client, not yet authorised by a user", resp.status_code)
    return TestResult(False, "failed", _describe(body, resp), resp.status_code)


async def check_store_dsn(dsn: str, *, store: str) -> TestResult:
    """Connect to a ledger store and refuse an over-privileged role."""
    from nexus.core.config import get_settings
    from nexus.sources.safety import SourceRejected, redact_dsn, validate_dsn

    allow_private = get_settings().env in ("local", "test")
    try:
        validate_dsn(dsn, allow_private=allow_private)
    except SourceRejected as exc:
        return TestResult(False, "failed", str(exc))
    try:
        import asyncpg
    except ImportError:
        return TestResult(False, "failed", "asyncpg is not installed (pip install -e '.[postgres]')")

    url = dsn.strip().replace("postgresql+asyncpg://", "postgresql://", 1)
    try:
        conn = await asyncio.wait_for(asyncpg.connect(url), timeout=_TIMEOUT)
    except Exception as exc:
        return TestResult(False, "failed",
                          f"could not connect to {redact_dsn(dsn)}: {type(exc).__name__}", None)
    try:
        row = await conn.fetchrow(
            "SELECT current_user AS role, r.rolsuper AS superuser, r.rolbypassrls AS bypass, "
            "current_setting('server_version') AS version "
            "FROM pg_roles r WHERE r.rolname = current_user"
        )
    finally:
        await conn.close()
    role = str(row["role"])
    if row["superuser"] or row["bypass"] or role in ("postgres", "supabase_admin"):
        return TestResult(
            False, "failed",
            f"connected as {role}, which can do everything. Create the {store} store's own role "
            "(docs/engagement/setup-supabase.md) and connect with that instead.",
        )
    return TestResult(True, "probe_ok",
                      f"connected to the {store} store as {role} (Postgres {row['version']})")


def check_pseudonym_secret(value: str) -> TestResult:
    text = (value or "").strip()
    generate = "generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
    if len(text) < 32:
        return TestResult(False, "failed", f"use at least 32 characters; {generate}")
    if len(set(text)) < 16:
        return TestResult(False, "failed", f"too few distinct characters to be random; {generate}")
    return TestResult(True, "verified", "long and varied enough to key the pseudonyms")


async def check(provider: str, key: str, *, transport=None) -> TestResult:
    """Dispatch for ``providers.testing.probe`` and ``verify``."""
    from nexus.engagement import config

    if provider == "google_oauth":
        app = await config.oauth_app(config.GOOGLE)
        return await check_google_client(key, client_id=app.client_id,
                                         redirect_uri=app.redirect_uri, transport=transport)
    if provider == "microsoft_oauth":
        app = await config.oauth_app(config.MICROSOFT)
        return await check_microsoft_client(key, client_id=app.client_id, tenant=app.tenant,
                                             redirect_uri=app.redirect_uri, transport=transport)
    if provider.startswith("ledger_") and provider[len("ledger_"):] in config.LEDGER_STORES:
        return await check_store_dsn(key, store=provider[len("ledger_"):])
    if provider == "ledger_pseudonym":
        return check_pseudonym_secret(key)
    return TestResult(False, "failed", f"no engagement check for {provider!r}")


ENGAGEMENT_KEY_IDS = frozenset({
    "google_oauth", "microsoft_oauth", "ledger_archive", "ledger_training", "ledger_insights",
    "ledger_pseudonym",
})
