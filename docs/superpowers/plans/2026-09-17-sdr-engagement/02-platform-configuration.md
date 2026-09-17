# Phase 02: Platform Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a superadmin configure the Google and Microsoft OAuth apps and the three ledger stores from the Control plane, test each secret, and copy every value Google Cloud and Azure ask for — with no redeploy and no secret ever displayed.

**Architecture:** Secrets become six new provider-key ids (`google_oauth`, `microsoft_oauth`, `ledger_archive`, `ledger_training`, `ledger_insights`, `ledger_pseudonym`) in the existing sealed store, each with a Test button backed by `nexus/engagement/credential_checks.py`. Non-secret values become runtime settings in a new "Mailboxes & engagement" group, each with a validator. `nexus/engagement/config.py` is the one reader, per call. A new admin endpoint and Control-plane tab show the derived redirect URIs, notification endpoints and scopes. Platform health gains two rows.

**Tech Stack:** FastAPI, httpx, asyncpg, React + TypeScript, pytest. Live tests against real Google and Microsoft apps.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §12, §18.2, D20, D21. **Depends on:** phase 01.

**Verified:** backend code in this plan was run in the CI image on top of phase 01 (`tests/test_engagement_platform_config.py`, `test_engagement_setup_ui.py`, `test_provider_keys.py`, `test_runtime_control_plane.py`, `test_runtime_config.py`, `test_admin_health.py`, `test_admin_routes_are_not_discoverable.py`, `ruff`), and the frontend with `npm run typecheck`: 158 tests passed, typecheck clean.

**Change to the roadmap:** the live-suite scaffold (`tests_live/engagement/conftest.py`) and the `live-engagement` CI job move from phase 03 to this phase, because the Test buttons are the first thing that needs a live check.

---

## Owner prerequisites (the owner does these; Claude never handles the credentials)

Phase code can be built and merged without them; the live tests skip until they exist.

1. Google Cloud: follow `docs/engagement/setup-google.md` steps 1–4 (created in Task 9).
2. Azure: follow `docs/engagement/setup-microsoft.md` steps 1–5 (created in Task 9).
3. Add these GitHub Actions secrets (Settings → Secrets and variables → Actions):
   `NEXUS_LIVE_REDIRECT_BASE` (the base URL registered in both apps, e.g. `https://localhost`),
   `NEXUS_LIVE_GOOGLE_CLIENT_ID`, `NEXUS_LIVE_GOOGLE_CLIENT_SECRET`,
   `NEXUS_LIVE_MICROSOFT_CLIENT_ID`, `NEXUS_LIVE_MICROSOFT_CLIENT_SECRET`, `NEXUS_LIVE_MICROSOFT_TENANT`.

Values already agreed with the owner (2026-09-17): redirect paths
`/api/engagement/mailboxes/oauth/google/callback` and `/api/engagement/mailboxes/oauth/microsoft/callback`
(local deploy base `https://localhost`); Google scopes `openid email gmail.readonly gmail.compose`;
Graph delegated scopes `openid email offline_access User.Read Mail.ReadWrite Mail.Send`.

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Modify | `nexus/core/config.py` | env floors for the engagement settings and secrets |
| Create | `nexus/engagement/config.py` | the one reader: OAuth apps, endpoints, ledger secrets |
| Modify | `nexus/providers/catalog.py` | six provider-key ids |
| Create | `nexus/engagement/credential_checks.py` | Test buttons for the six secrets |
| Modify | `nexus/providers/testing.py` | dispatch probe/verify to the checks |
| Modify | `nexus/runtime_config/catalog.py` | "Mailboxes & engagement" group, seven settings |
| Modify | `nexus/runtime_config/service.py` | six validators |
| Modify | `nexus/api/routers/admin_health.py` | `mailbox apps` and `ledger stores` rows |
| Create | `nexus/api/routers/admin_engagement.py` | `GET /admin/engagement/setup` |
| Modify | `nexus/api/routers/__init__.py` | register it |
| Create | `frontend/src/pages/admin/EngagementSetupTab.tsx` (+ `.module.css`) | the Mailbox apps tab |
| Modify | `frontend/src/pages/AdminBillingPage.tsx`, `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts` | wire the tab |
| Create | `tests/test_engagement_platform_config.py` | offline behaviour |
| Create | `tests/test_engagement_setup_ui.py` | source-reading UI test |
| Modify | `tests/test_provider_keys.py` | the catalog grew from 9 to 15 |
| Create | `tests_live/engagement/conftest.py`, `tests_live/engagement/test_credentials_live.py` | live suite |
| Modify | `.github/workflows/ci.yml` | `workflow_dispatch` + `live-engagement` job |
| Create | `docs/engagement/setup-google.md`, `docs/engagement/setup-microsoft.md` | owner guides |

---

### Task 1: Environment floors in Settings

**Files:**
- Modify: `nexus/core/config.py` (after `network_token_enc_key: str = ""`)

- [ ] **Step 1: Add the fields**

After the line `    network_token_enc_key: str = ""` add:

```python

    # SDR engagement engine (docs/superpowers/specs/2026-09-17-sdr-engagement-design.md, §12).
    # Client SECRETS are managed in Provider keys (google_oauth, microsoft_oauth); these env values
    # are the floor the managed keys layer over. The non-secret values are runtime settings.
    engagement_google_client_id: str = ""
    engagement_google_client_secret: str = ""
    engagement_google_pubsub_topic: str = ""          # projects/<project>/topics/<topic>
    engagement_google_push_service_account: str = ""  # signs the Pub/Sub push OIDC token
    engagement_microsoft_client_id: str = ""
    engagement_microsoft_client_secret: str = ""
    engagement_microsoft_tenant: str = "common"
    # Public https origin Google and Microsoft call back to: OAuth redirects, Gmail push, Graph
    # notifications. Never client-supplied.
    engagement_public_base_url: str = ""
    # Release B switch: campaigns, the reply desk and their workers. Mailbox connection and the
    # ledger do not depend on it.
    engagement_campaigns_enabled: bool = False
    # Training & insights ledger stores and the pseudonymisation secret (§18). Managed in
    # Provider keys (ledger_archive, ledger_training, ledger_insights, ledger_pseudonym).
    ledger_archive_dsn: str = ""
    ledger_training_dsn: str = ""
    ledger_insights_dsn: str = ""
    ledger_pseudonym_secret: str = ""
```

- [ ] **Step 2: Check it imports**

Run: `python -c "from nexus.core.config import Settings; print(Settings.model_fields['engagement_campaigns_enabled'].default)"`
Expected: `False`

- [ ] **Step 3: Commit**

```bash
git add nexus/core/config.py
git commit -m "feat(engagement): settings floors for mailbox OAuth apps and ledger stores"
```

---

### Task 2: Six provider-key ids

**Files:**
- Modify: `nexus/providers/catalog.py`, `tests/test_provider_keys.py`
- Test: `tests/test_engagement_platform_config.py` (created here, extended in later tasks)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_engagement_platform_config.py` containing, for now, only the imports, the fixture and the first test from the final file shown in Task 5 Step 1 (`test_the_six_engagement_secrets_are_provider_keys_with_env_floors`).

In `tests/test_provider_keys.py`, change `assert len(PROVIDERS) == 9` to `assert len(PROVIDERS) == 15` and extend the set in `test_the_catalog_covers_exactly_the_pooled_providers`:

```python
    assert set(PROVIDERS) == {
        "groq", "anthropic", "openai_compat", "exa",
        "firecrawl", "brave", "serper", "apify", "github",
        # Engagement engine (spec §12, §18): two OAuth client secrets, three ledger store
        # connection strings and the pseudonymisation secret.
        "google_oauth", "microsoft_oauth", "ledger_archive", "ledger_training",
        "ledger_insights", "ledger_pseudonym",
    }
```

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_platform_config.py tests/test_provider_keys.py -n0 -q`
Expected: FAIL — `assert 'google_oauth' in PROVIDERS` and `assert 9 == 15`.

- [ ] **Step 3: Add the ids**

In `nexus/providers/catalog.py`, after the `"github"` entry in `PROVIDERS`:

```python
    # Engagement engine (spec §12, §18). One secret each, not rotation pools: the resolver's
    # first key is the one in use. Tested by nexus/engagement/credential_checks.py.
    "google_oauth": ProviderSpec(
        "google_oauth", "Google OAuth client secret (mailboxes)",
        "engagement_google_client_secret",
    ),
    "microsoft_oauth": ProviderSpec(
        "microsoft_oauth", "Microsoft app client secret (mailboxes)",
        "engagement_microsoft_client_secret",
    ),
    "ledger_archive": ProviderSpec(
        "ledger_archive", "Ledger archive store (Postgres connection string)",
        "ledger_archive_dsn",
    ),
    "ledger_training": ProviderSpec(
        "ledger_training", "Ledger training store (Postgres connection string)",
        "ledger_training_dsn",
    ),
    "ledger_insights": ProviderSpec(
        "ledger_insights", "Ledger insights store (Postgres connection string)",
        "ledger_insights_dsn",
    ),
    "ledger_pseudonym": ProviderSpec(
        "ledger_pseudonym", "Ledger pseudonymisation secret", "ledger_pseudonym_secret",
    ),
```

None of these is a cryptographic root of THIS application (`test_crypto_roots_are_never_manageable` still passes): losing the pseudonym secret breaks ledger linkage, not the app, and rotation re-keys from the archive (spec §16).

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_platform_config.py tests/test_provider_keys.py -n0 -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add nexus/providers/catalog.py tests/test_provider_keys.py tests/test_engagement_platform_config.py
git commit -m "feat(engagement): provider-key ids for OAuth client secrets and ledger stores"
```

---

### Task 3: The one reader, `nexus/engagement/config.py`

**Files:**
- Create: `nexus/engagement/config.py`
- Test: `tests/test_engagement_platform_config.py`

- [ ] **Step 1: Write the failing tests**

Add these tests from the final file (Task 5 Step 1): `test_an_unconfigured_provider_names_every_missing_piece`, `test_a_managed_secret_and_two_settings_configure_a_provider`, `test_endpoints_are_derived_from_the_base_url_and_the_push_token_from_the_secret_key`, `test_campaigns_are_off_until_switched_on`.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_platform_config.py -n0 -q`
Expected: FAIL with `ImportError: cannot import name 'config' from 'nexus.engagement'`

- [ ] **Step 3: Implement**

Create `nexus/engagement/config.py`:

```python
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

import hashlib
import hmac
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
    """The audience the Pub/Sub push subscription must put in its OIDC token."""
    base = public_base_url()
    return f"{base}/api/engagement/webhooks/gmail" if base else ""


def gmail_push_token() -> str:
    """A per-deployment path token, derived from ``secret_key``, required on every Gmail push.

    A second factor beside the OIDC signature: a leaked push URL alone does not make a forged
    notification acceptable, and a stolen Google-signed token for another audience fails the
    audience check.
    """
    key = get_settings().secret_key.encode()
    return hmac.new(key, b"engagement:gmail-push", hashlib.sha256).hexdigest()[:40]


def gmail_push_endpoint() -> str:
    audience = gmail_push_audience()
    return f"{audience}?token={gmail_push_token()}" if audience else ""


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
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_platform_config.py -n0 -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/config.py tests/test_engagement_platform_config.py
git commit -m "feat(engagement): one per-call reader for OAuth apps, endpoints and ledger secrets"
```

---

### Task 4: Test buttons for the six secrets

**Files:**
- Create: `nexus/engagement/credential_checks.py`
- Modify: `nexus/providers/testing.py`
- Test: `tests/test_engagement_platform_config.py`, `tests_live/engagement/test_credentials_live.py`

- [ ] **Step 1: Write the failing offline tests**

Add `test_the_pseudonymisation_secret_must_be_long_and_varied` and `test_a_store_dsn_pointing_inside_the_network_is_refused_before_connecting` from the final file (Task 5 Step 1).

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_platform_config.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.credential_checks'`

- [ ] **Step 3: Implement the checks**

Create `nexus/engagement/credential_checks.py`:

```python
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
```

- [ ] **Step 4: Dispatch from the provider-key Test buttons**

In `nexus/providers/testing.py`, in BOTH `probe` and `verify`, immediately after

```python
    if provider not in PROVIDERS:
        return TestResult(False, "failed", f"unknown provider {provider!r}")
```

insert (in `verify`, keep the comment):

```python
    from nexus.engagement.credential_checks import ENGAGEMENT_KEY_IDS, check

    if provider in ENGAGEMENT_KEY_IDS:
        # No deeper call exists: an OAuth client is proven only when a user authorises it, and a
        # store's grants are checked by the ledger's own schema check (phase 06).
        return await check(provider, key, transport=transport)
```

- [ ] **Step 5: Run the offline tests**

Run: `pytest tests/test_engagement_platform_config.py tests/test_provider_keys.py -n0 -q`
Expected: all pass.

- [ ] **Step 6: Create the live-suite scaffold**

Create `tests_live/engagement/conftest.py`:

```python
"""The live engagement suite: real Google, Microsoft 365, Supabase and LLM calls (D21).

Nothing here is faked. Each test declares the secrets it needs through ``require_env``; when one is
missing the test SKIPS with the variable's name, so a partially configured CI shows exactly what is
left to set up rather than failing.

Run: ``pytest tests_live/engagement -n0 -q -rs``. Serial on purpose: the tests share real mailboxes
and stores, and two runs interleaving sends would make reply matching assertions meaningless.

The database is a throwaway SQLite file, as in ``tests/``; only the provider side is live.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import pytest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_TMPDIR = tempfile.mkdtemp(prefix="nexus_live_")
os.environ.setdefault("NEXUS_DATABASE_URL", f"sqlite+aiosqlite:///{_TMPDIR}/live.db")
os.environ.setdefault("NEXUS_ENV", "test")
os.environ.setdefault("NEXUS_LLM_PROVIDER", "auto")


def require_env(*names: str) -> dict[str, str]:
    """The named environment values, or skip the test naming whichever are missing."""
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        pytest.skip(f"live secret not configured: {', '.join(missing)}")
    return values


def redirect_uri(provider: str) -> str:
    base = require_env("NEXUS_LIVE_REDIRECT_BASE")["NEXUS_LIVE_REDIRECT_BASE"].rstrip("/")
    return f"{base}/api/engagement/mailboxes/oauth/{provider}/callback"
```

Create `tests_live/engagement/test_credentials_live.py`:

```python
"""The Test buttons for the two OAuth client secrets, against the real Google and Microsoft apps."""
from __future__ import annotations

from nexus.engagement.credential_checks import check_google_client, check_microsoft_client
from tests_live.engagement.conftest import redirect_uri, require_env


async def test_the_real_google_client_is_recognised_and_a_wrong_secret_is_not():
    env = require_env("NEXUS_LIVE_GOOGLE_CLIENT_ID", "NEXUS_LIVE_GOOGLE_CLIENT_SECRET")
    uri = redirect_uri("google")

    good = await check_google_client(
        env["NEXUS_LIVE_GOOGLE_CLIENT_SECRET"], client_id=env["NEXUS_LIVE_GOOGLE_CLIENT_ID"],
        redirect_uri=uri,
    )
    assert good.ok and good.status == "probe_ok", good.detail

    bad = await check_google_client(
        "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0", client_id=env["NEXUS_LIVE_GOOGLE_CLIENT_ID"],
        redirect_uri=uri,
    )
    assert not bad.ok and bad.http_status == 401, (bad.http_status, bad.detail)


async def test_the_real_microsoft_client_is_recognised_and_a_wrong_secret_is_not():
    env = require_env(
        "NEXUS_LIVE_MICROSOFT_CLIENT_ID", "NEXUS_LIVE_MICROSOFT_CLIENT_SECRET",
        "NEXUS_LIVE_MICROSOFT_TENANT",
    )
    uri = redirect_uri("microsoft")

    good = await check_microsoft_client(
        env["NEXUS_LIVE_MICROSOFT_CLIENT_SECRET"], client_id=env["NEXUS_LIVE_MICROSOFT_CLIENT_ID"],
        tenant=env["NEXUS_LIVE_MICROSOFT_TENANT"], redirect_uri=uri,
    )
    assert good.ok and good.status == "probe_ok", good.detail

    bad = await check_microsoft_client(
        "not~the~secret~value~0000000000000000", client_id=env["NEXUS_LIVE_MICROSOFT_CLIENT_ID"],
        tenant=env["NEXUS_LIVE_MICROSOFT_TENANT"], redirect_uri=uri,
    )
    assert not bad.ok and "AADSTS7000215" in bad.detail, bad.detail
```

- [ ] **Step 7: Run the live tests**

Run: `pytest tests_live/engagement -n0 -q -rs`
Expected without secrets: `2 skipped` naming the missing variables. With the owner's secrets exported: `2 passed`.

- [ ] **Step 8: Commit**

```bash
git add nexus/engagement/credential_checks.py nexus/providers/testing.py tests/test_engagement_platform_config.py tests_live/engagement
git commit -m "feat(engagement): Test buttons for OAuth client secrets, ledger stores and the pseudonym secret"
```

---

### Task 5: Runtime settings and validators

**Files:**
- Modify: `nexus/runtime_config/catalog.py`, `nexus/runtime_config/service.py`
- Test: `tests/test_engagement_platform_config.py` (final version)

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_engagement_platform_config.py` with the final version:

```python
"""Superadmin configuration for mailbox OAuth apps and the ledger stores (spec §12).

Offline by design: nothing here calls Google, Microsoft or a store. The calls themselves are exercised
by `tests_live/engagement/test_credentials_live.py` against the real apps (D21).
"""
from __future__ import annotations

import pytest

from nexus.core.config import get_settings
from tests.conftest import assert_staff_surface_hidden, auth, signup


@pytest.fixture(autouse=True)
def _clean_settings_and_keys():
    from nexus.providers import resolver

    settings = get_settings()
    keys = ("engagement_public_base_url", "engagement_google_client_id",
            "engagement_microsoft_client_id", "engagement_microsoft_tenant",
            "engagement_campaigns_enabled", "engagement_google_pubsub_topic",
            "engagement_google_push_service_account")
    before = {k: getattr(settings, k) for k in keys}
    resolver.invalidate()
    yield
    for key, value in before.items():
        setattr(settings, key, value)
    resolver.invalidate()


def test_the_six_engagement_secrets_are_provider_keys_with_env_floors():
    from nexus.providers.catalog import PROVIDERS

    settings = get_settings()
    for key_id in ("google_oauth", "microsoft_oauth", "ledger_archive", "ledger_training",
                   "ledger_insights", "ledger_pseudonym"):
        assert key_id in PROVIDERS
        assert hasattr(settings, PROVIDERS[key_id].env_attr)


async def test_an_unconfigured_provider_names_every_missing_piece():
    from nexus.engagement import config

    app = await config.oauth_app(config.GOOGLE)
    assert not app.configured
    assert any("Public base URL" in m for m in app.missing)
    assert any("client id" in m for m in app.missing)
    assert any("google_oauth" in m for m in app.missing)


async def test_a_managed_secret_and_two_settings_configure_a_provider(monkeypatch):
    from nexus.engagement import config
    from nexus.providers import resolver
    from nexus.providers.service import add_key

    monkeypatch.setattr(get_settings(), "engagement_public_base_url", "https://app.example.com")
    monkeypatch.setattr(get_settings(), "engagement_microsoft_client_id",
                        "11111111-2222-3333-4444-555555555555")
    await add_key("microsoft_oauth", "prod app", "a-real-looking-client-secret-value")
    resolver.invalidate()

    app = await config.oauth_app(config.MICROSOFT)
    assert app.configured, app.missing
    assert app.client_secret == "a-real-looking-client-secret-value"
    assert app.tenant == "common"
    assert app.redirect_uri == (
        "https://app.example.com/api/engagement/mailboxes/oauth/microsoft/callback"
    )
    assert "https://graph.microsoft.com/Mail.Send" in app.scopes


def test_endpoints_are_derived_from_the_base_url_and_the_push_token_from_the_secret_key(
    monkeypatch,
):
    from nexus.engagement import config

    monkeypatch.setattr(get_settings(), "engagement_public_base_url", "https://app.example.com/")
    assert config.gmail_push_audience() == "https://app.example.com/api/engagement/webhooks/gmail"
    assert config.gmail_push_endpoint().startswith(config.gmail_push_audience() + "?token=")
    assert config.graph_notification_url() == (
        "https://app.example.com/api/engagement/webhooks/graph"
    )
    token = config.gmail_push_token()
    monkeypatch.setattr(get_settings(), "secret_key", "a-different-deployment-secret-key-0000")
    assert config.gmail_push_token() != token


def test_campaigns_are_off_until_switched_on():
    from nexus.core.config import Settings
    from nexus.engagement import config

    assert Settings.model_fields["engagement_campaigns_enabled"].default is False
    assert config.campaigns_enabled() is False


@pytest.mark.parametrize("key,good,bad", [
    ("engagement_public_base_url", "https://app.example.com", "https://app.example.com/path"),
    ("engagement_google_client_id", "123456789012-abc123.apps.googleusercontent.com",
     "abc123.googleusercontent.com"),
    ("engagement_google_pubsub_topic", "projects/my-project/topics/gmail-replies",
     "gmail-replies"),
    ("engagement_google_push_service_account", "gmail-push@my-project.iam.gserviceaccount.com",
     "someone@gmail.com"),
    ("engagement_microsoft_client_id", "11111111-2222-3333-4444-555555555555", "my-app"),
    ("engagement_microsoft_tenant", "contoso.onmicrosoft.com", "not a tenant"),
])
def test_each_engagement_setting_is_validated_before_it_is_stored(key, good, bad):
    from nexus.runtime_config.catalog import CATALOG
    from nexus.runtime_config.service import _VALIDATORS

    assert CATALOG[key].group == "Mailboxes & engagement"
    _VALIDATORS[key](good)
    with pytest.raises(ValueError):
        _VALIDATORS[key](bad)


def test_the_public_base_url_must_be_https_and_public_outside_a_local_stack(monkeypatch):
    from nexus.runtime_config.service import _VALIDATORS

    monkeypatch.setattr(get_settings(), "env", "prod")
    for url in ("http://app.example.com", "https://127.0.0.1", "https://169.254.169.254"):
        with pytest.raises(ValueError):
            _VALIDATORS["engagement_public_base_url"](url)


def test_the_pseudonymisation_secret_must_be_long_and_varied():
    from nexus.engagement.credential_checks import check_pseudonym_secret

    assert not check_pseudonym_secret("short").ok
    assert not check_pseudonym_secret("a" * 64).ok
    good = check_pseudonym_secret("q3Jv9wXkP2mN7rT5yH8bL4cF6dS1gZ0aEuIoWnMk")
    assert good.ok and good.status == "verified"


async def test_a_store_dsn_pointing_inside_the_network_is_refused_before_connecting(monkeypatch):
    from nexus.engagement.credential_checks import check_store_dsn

    monkeypatch.setattr(get_settings(), "env", "prod")
    result = await check_store_dsn("postgresql://ledger:pw@169.254.169.254:5432/postgres",
                                   store="archive")
    assert not result.ok and "refusing" in result.detail


async def test_the_setup_screen_shows_what_to_paste_and_never_a_secret(client, monkeypatch):
    from nexus.providers.service import add_key

    email = "owner@platformco.com"
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    monkeypatch.setattr(get_settings(), "engagement_public_base_url", "https://app.example.com")
    token = await signup(client, slug="plat", email=email, company="Platform")
    await add_key("google_oauth", "prod", "google-client-secret-never-shown")

    r = await client.get("/api/admin/engagement/setup", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "google-client-secret-never-shown" not in r.text
    google = next(a for a in body["mailbox_apps"] if a["provider"] == "google")
    assert google["redirect_uri"].endswith("/api/engagement/mailboxes/oauth/google/callback")
    assert body["gmail_push_endpoint"].startswith(
        "https://app.example.com/api/engagement/webhooks/gmail?token="
    )
    assert body["ledger_stores"] == {"archive": False, "training": False, "insights": False}


async def test_the_setup_screen_is_hidden_from_workspace_members(client):
    token = await signup(client, slug="member", email="rep@memberco.com", company="Member")
    assert_staff_surface_hidden(
        await client.get("/api/admin/engagement/setup", headers=auth(token))
    )
```

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_platform_config.py -n0 -q`
Expected: FAIL — `KeyError: 'engagement_public_base_url'` in the validator tests, and the setup-screen tests 404.

- [ ] **Step 3: Add the group and settings**

In `nexus/runtime_config/catalog.py`, after `RELIABILITY = "Reliability"`:

```python
ENGAGEMENT = "Mailboxes & engagement"
```

Change `GROUP_ORDER` to:

```python
GROUP_ORDER: tuple[str, ...] = (
    EMAIL, CONTACTS, PERSONALIZATION, AI, SIGNALS, AUTOMATION, OUTREACH, ENGAGEMENT, BILLING,
    ACCESS, RELIABILITY,
)
```

In `_SPECS`, immediately before the `# ---- money ----` comment, add:

```python
    # ---- mailboxes and engagement (spec §12) ----------------------------------------------------
    SettingSpec(
        key="engagement_public_base_url", label="Public base URL", group=ENGAGEMENT, kind="str",
        effect="The https address Google and Microsoft call back to: OAuth redirects, Gmail push "
               "notifications and Microsoft Graph notifications are all built from it.",
        warning="Changing it breaks every redirect URI and notification endpoint registered in "
                "Google Cloud and Azure until they are updated to match. Mailboxes stop "
                "connecting and replies stop arriving, silently.",
        risk="high", placeholder="https://app.example.com",
    ),
    SettingSpec(
        key="engagement_google_client_id", label="Google OAuth client id", group=ENGAGEMENT,
        kind="str",
        effect="Which Google Cloud OAuth client SDRs connect Gmail through. Its secret lives in "
               "Provider keys as google_oauth.",
        warning="A different client invalidates every connected Gmail mailbox: each SDR has to "
                "connect again.",
        risk="high", placeholder="123456789012-abc123.apps.googleusercontent.com",
    ),
    SettingSpec(
        key="engagement_google_pubsub_topic", label="Gmail notification topic", group=ENGAGEMENT,
        kind="str",
        effect="The Pub/Sub topic Gmail publishes to when a connected mailbox changes. Replies "
               "arrive within seconds instead of on the few-minute poll.",
        warning="A topic Gmail cannot publish to makes every watch fail; replies then arrive "
                "only on the poll. The topic must grant Publisher to "
                "gmail-api-push@system.gserviceaccount.com.",
        risk="medium", placeholder="projects/my-project/topics/gmail-replies",
    ),
    SettingSpec(
        key="engagement_google_push_service_account", label="Gmail push service account",
        group=ENGAGEMENT, kind="str",
        effect="The service account the Pub/Sub push subscription signs its requests with. A "
               "push signed by any other account is refused.",
        warning="If it does not match the subscription's authentication setting, every push is "
                "refused and replies arrive only on the poll.",
        risk="medium", placeholder="gmail-push@my-project.iam.gserviceaccount.com",
    ),
    SettingSpec(
        key="engagement_microsoft_client_id", label="Microsoft app (client) id",
        group=ENGAGEMENT, kind="str",
        effect="Which Azure app registration SDRs connect Microsoft 365 through. Its secret lives "
               "in Provider keys as microsoft_oauth.",
        warning="A different app invalidates every connected Microsoft mailbox: each SDR has to "
                "connect again.",
        risk="high", placeholder="00000000-0000-0000-0000-000000000000",
    ),
    SettingSpec(
        key="engagement_microsoft_tenant", label="Microsoft tenant", group=ENGAGEMENT, kind="str",
        effect="Which Microsoft accounts may connect: common (any work or school account and "
               "personal), organizations (work or school only), or one tenant id or domain.",
        warning="Narrowing it stops SDRs outside that tenant from connecting or refreshing.",
        risk="medium", placeholder="common",
    ),
    SettingSpec(
        key="engagement_campaigns_enabled", label="Campaigns and reply desk", group=ENGAGEMENT,
        kind="bool",
        effect="Turns on the engagement campaigns, the reply desk and the workers that send "
               "follow-ups and read replies.",
        warning="Campaign steps send real email from SDR mailboxes and replies are read and "
                "acted on. Switch on only after the cutover dry run has been reviewed.",
        risk="high",
    ),

```

- [ ] **Step 4: Add the validators**

In `nexus/runtime_config/service.py`, immediately before the comment `# Settings whose value does NOT live on the \`Settings\` object.`, add:

```python
def _validate_public_base_url(value) -> None:
    """Google and Microsoft redirect browsers here and POST notifications here. It must be an
    https origin with no path, because every endpoint is appended to it; plain http is allowed
    only on a local stack."""
    from urllib.parse import urlparse

    from nexus.core.config import get_settings
    from nexus.sources.safety import _is_blocked_host

    raw = str(value or "").strip()
    parsed = urlparse(raw)
    local = get_settings().env in ("local", "test")
    allowed = ("https", "http") if local else ("https",)
    if (parsed.scheme or "").lower() not in allowed:
        raise ValueError("the public base URL must start with https://")
    if not parsed.hostname:
        raise ValueError("the public base URL has no host")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("give only the origin, like https://app.example.com, with no path")
    blocked, why = _is_blocked_host(parsed.hostname, allow_private=local)
    if blocked:
        raise ValueError(f"refusing this host: {why}")


def _regex_validator(pattern: str, message: str):
    import re

    compiled = re.compile(pattern)

    def _validate(value) -> None:
        if not compiled.fullmatch(str(value or "").strip()):
            raise ValueError(message)

    return _validate


_validate_google_client_id = _regex_validator(
    r"[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com",
    "a Google OAuth client id looks like 123456789012-abc123.apps.googleusercontent.com",
)
_validate_pubsub_topic = _regex_validator(
    r"projects/[a-z][-a-z0-9]{4,28}[a-z0-9]/topics/[A-Za-z][-A-Za-z0-9._~%+]{2,254}",
    "a Pub/Sub topic looks like projects/my-project/topics/gmail-replies",
)
_validate_service_account = _regex_validator(
    r"[a-z][-a-z0-9]{4,28}[a-z0-9]@[a-z][-a-z0-9]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com",
    "a service account looks like gmail-push@my-project.iam.gserviceaccount.com",
)
_validate_microsoft_client_id = _regex_validator(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
    "an Azure application (client) id is a GUID like 00000000-0000-0000-0000-000000000000",
)
_validate_microsoft_tenant = _regex_validator(
    r"common|organizations|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}|[a-z0-9-]+(\.[a-z0-9-]+)+",
    "use common, organizations, a tenant id (GUID) or a domain like contoso.onmicrosoft.com",
)


```

and add to `_VALIDATORS`, after `"billing_dunning_schedule_days": _validate_dunning_schedule,`:

```python
    "engagement_public_base_url": _validate_public_base_url,
    "engagement_google_client_id": _validate_google_client_id,
    "engagement_google_pubsub_topic": _validate_pubsub_topic,
    "engagement_google_push_service_account": _validate_service_account,
    "engagement_microsoft_client_id": _validate_microsoft_client_id,
    "engagement_microsoft_tenant": _validate_microsoft_tenant,
```

The setup-screen tests still fail until Task 7; the validator tests pass now.

- [ ] **Step 5: Run the control-plane guards**

Run: `pytest tests/test_runtime_control_plane.py tests/test_runtime_config.py -n0 -q`
Expected: all pass. `test_every_setting_in_the_panel_is_read_by_something` passes because `nexus/engagement/config.py` reads all seven; `test_a_free_text_setting_is_validated_before_it_is_stored` passes because all six free-text settings have validators.

- [ ] **Step 6: Commit**

```bash
git add nexus/runtime_config/catalog.py nexus/runtime_config/service.py tests/test_engagement_platform_config.py
git commit -m "feat(engagement): Mailboxes & engagement runtime settings with validators"
```

---

### Task 6: Platform health rows

**Files:**
- Modify: `nexus/api/routers/admin_health.py`

- [ ] **Step 1: Add the probes**

Immediately before `_PROBES: tuple[tuple[str, Any], ...] = (` add:

```python
async def _probe_mailbox_apps() -> tuple[str, str]:
    """Whether SDRs can connect Gmail and Microsoft 365. Each configured client is checked against
    its token endpoint; an unconfigured one is reported as such, never as broken."""
    from nexus.engagement import config
    from nexus.engagement.credential_checks import check_google_client, check_microsoft_client

    parts, statuses = [], []
    for provider in config.MAILBOX_PROVIDERS:
        app = await config.oauth_app(provider)
        if not app.configured:
            parts.append(f"{provider}: not configured ({'; '.join(app.missing)})")
            statuses.append(UNCONFIGURED)
            continue
        if provider == config.GOOGLE:
            result = await check_google_client(app.client_secret, client_id=app.client_id,
                                               redirect_uri=app.redirect_uri)
        else:
            result = await check_microsoft_client(app.client_secret, client_id=app.client_id,
                                                  tenant=app.tenant,
                                                  redirect_uri=app.redirect_uri)
        parts.append(f"{provider}: {result.detail}")
        statuses.append(OK if result.ok else ERROR)
    if ERROR in statuses:
        return ERROR, "; ".join(parts)
    if all(s == UNCONFIGURED for s in statuses):
        return UNCONFIGURED, "; ".join(parts)
    return (DEGRADED if UNCONFIGURED in statuses else OK), "; ".join(parts)


async def _probe_ledger_stores() -> tuple[str, str]:
    """Whether the three training & insights stores accept a connection with a scoped role."""
    from nexus.engagement import config
    from nexus.engagement.credential_checks import check_store_dsn

    parts, statuses = [], []
    for store in config.LEDGER_STORES:
        dsn = await config.ledger_dsn(store)
        if not dsn:
            parts.append(f"{store}: not configured")
            statuses.append(UNCONFIGURED)
            continue
        result = await check_store_dsn(dsn, store=store)
        parts.append(f"{store}: {result.detail}")
        statuses.append(OK if result.ok else ERROR)
    if not await config.pseudonym_secret():
        parts.append("pseudonymisation secret: not configured")
        statuses.append(UNCONFIGURED)
    if ERROR in statuses:
        return ERROR, "; ".join(parts)
    if all(s == UNCONFIGURED for s in statuses):
        return UNCONFIGURED, "; ".join(parts)
    return (DEGRADED if UNCONFIGURED in statuses else OK), "; ".join(parts)


```

and append two entries to `_PROBES`, after `("phone lookup", _probe_phone_lookup),`:

```python
    ("mailbox apps", _probe_mailbox_apps),
    ("ledger stores", _probe_ledger_stores),
```

- [ ] **Step 2: Run the health tests**

Run: `pytest tests/test_admin_health.py -n0 -q`
Expected: all pass (the dependency assertion is a subset check; offline both new rows read `unconfigured` without any network call).

- [ ] **Step 3: Commit**

```bash
git add nexus/api/routers/admin_health.py
git commit -m "feat(engagement): platform health rows for mailbox apps and ledger stores"
```

---

### Task 7: The setup endpoint

**Files:**
- Create: `nexus/api/routers/admin_engagement.py`
- Modify: `nexus/api/routers/__init__.py`

- [ ] **Step 1: See the setup tests fail**

Run: `pytest tests/test_engagement_platform_config.py -n0 -q -k setup_screen`
Expected: FAIL — 404 for the superadmin.

- [ ] **Step 2: Implement**

Create `nexus/api/routers/admin_engagement.py`:

```python
# nexus/api/routers/admin_engagement.py
"""What an operator pastes into Google Cloud and Azure, and whether each piece is in place (§12).

Setting up mailbox connection is two consoles the product does not control. Every value those
consoles ask for — redirect URIs, the Pub/Sub push endpoint and its OIDC audience, the Graph
notification URL, the scopes to request — is derived here from the public base URL, so the
operator copies it rather than composing it, and a typo cannot silently break the flow.

Gated on ``providers.manage``: the push endpoint carries the deployment's push token, and the
people who hold the client secrets are the people who need to see it. No secret is in the response;
``configured`` booleans say whether one is stored.
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
    gmail_push_endpoint: str
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
        gmail_push_endpoint=config.gmail_push_endpoint(),
        gmail_push_audience=config.gmail_push_audience(),
        graph_notification_url=config.graph_notification_url(),
        ledger_stores={s: bool(await config.ledger_dsn(s)) for s in config.LEDGER_STORES},
        pseudonym_secret_configured=bool(await config.pseudonym_secret()),
    )
```

In `nexus/api/routers/__init__.py`, add `admin_engagement,` to the import list after `admin_billing_write,`, and `admin_engagement.router,` to `all_routers` after `admin_health.router,`.

- [ ] **Step 3: Run**

Run: `pytest tests/test_engagement_platform_config.py tests/test_admin_routes_are_not_discoverable.py -n0 -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add nexus/api/routers/admin_engagement.py nexus/api/routers/__init__.py
git commit -m "feat(engagement): GET /admin/engagement/setup - values to paste into Google Cloud and Azure"
```

---

### Task 8: The Mailbox apps tab

Invoke the `impeccable` skill before this task.

**Files:**
- Create: `frontend/src/pages/admin/EngagementSetupTab.tsx`, `frontend/src/pages/admin/EngagementSetupTab.module.css`
- Modify: `frontend/src/pages/AdminBillingPage.tsx`, `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`
- Test: `tests/test_engagement_setup_ui.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_engagement_setup_ui.py`:

```python
"""The Control plane shows what to paste into Google Cloud and Azure (spec §12).

There is no frontend test runner, so these read the source, as the other UI tests here do.
"""
from __future__ import annotations

import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"


def _read(rel: str) -> str:
    path = SRC / rel
    assert path.exists(), f"{rel} is missing"
    return path.read_text(encoding="utf-8")


def test_the_mailbox_apps_tab_is_offered_to_whoever_manages_provider_keys():
    page = _read("pages/AdminBillingPage.tsx")
    assert 'can(PROVIDERS_MANAGE) ? [{ value: "engagement", label: "Mailbox apps" }]' in page
    assert '{tab === "engagement" && can(PROVIDERS_MANAGE) && <EngagementSetupTab />}' in page


def test_every_value_an_operator_pastes_can_be_copied_and_no_secret_is_rendered():
    tab = _read("pages/admin/EngagementSetupTab.tsx")
    assert "api.engagementSetup" in tab
    for label in ("Redirect URI", "Push endpoint", "OIDC audience", "Notification URL", "Scopes"):
        assert f'<CopyRow label="{label}"' in tab, f"{label} cannot be copied"
    assert "navigator.clipboard.writeText" in tab
    assert "app.missing.map" in tab, "what is missing is not listed"
    assert "client_secret" not in tab and "secret}" not in tab


def test_the_client_knows_the_setup_shape():
    assert 'this.request<EngagementSetup>("/admin/engagement/setup"' in _read("lib/api.ts")
    types = _read("lib/types.ts")
    assert "export interface EngagementSetup" in types
    assert "export interface MailboxAppSetup" in types
```

- [ ] **Step 2: Run to see it fail**

Run: `pytest tests/test_engagement_setup_ui.py -n0 -q`
Expected: FAIL — `pages/admin/EngagementSetupTab.tsx is missing`.

- [ ] **Step 3: Types and client**

Append to `frontend/src/lib/types.ts`:

```ts
/** One mailbox OAuth app as the Control plane reports it. No secret is ever included. */
export interface MailboxAppSetup {
  provider: "google" | "microsoft";
  configured: boolean;
  missing: string[];
  client_id: string;
  tenant: string;
  redirect_uri: string;
  scopes: string[];
}

/** GET /admin/engagement/setup — what to paste into Google Cloud and Azure (spec §12). */
export interface EngagementSetup {
  public_base_url: string;
  campaigns_enabled: boolean;
  mailbox_apps: MailboxAppSetup[];
  gmail_pubsub_topic: string;
  gmail_push_service_account: string;
  gmail_push_endpoint: string;
  gmail_push_audience: string;
  graph_notification_url: string;
  ledger_stores: Record<string, boolean>;
  pseudonym_secret_configured: boolean;
}
```

In `frontend/src/lib/api.ts`, add `EngagementSetup,` to the `import type { ... }` list after `SupportedProvider,`, and add this method immediately before `supportedProviders(signal?: AbortSignal) {`:

```ts
  engagementSetup(signal?: AbortSignal) {
    return this.request<EngagementSetup>("/admin/engagement/setup", { signal });
  }
```

- [ ] **Step 4: The tab**

Create `frontend/src/pages/admin/EngagementSetupTab.tsx`:

```tsx
import { Badge, Button, CardHeader, Skeleton } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { useToast } from "@/components/ui/Toast";
import type { EngagementSetup, MailboxAppSetup } from "@/lib/types";
import styles from "./EngagementSetupTab.module.css";

/**
 * What to paste into Google Cloud and Azure, and whether each piece is in place (spec §12).
 *
 * Connecting SDR mailboxes depends on two consoles the product does not control. Every value those
 * consoles ask for is derived on the server from the public base URL, so it is copied rather than
 * composed: a hand-typed redirect URI with one wrong character fails the OAuth flow with an error
 * that names neither the URI nor the fix.
 *
 * Secrets are never shown. They are entered in Provider keys, where each has a Test button; this
 * screen says only whether one is stored. The step-by-step guides live in `docs/engagement/`.
 */
export function EngagementSetupTab() {
  const api = useApiClient();
  const setup = useApi<EngagementSetup>((signal) => api.engagementSetup(signal), []);

  return (
    <div className={styles.wrap}>
      <CardHeader
        title="Mailbox apps"
        subtitle="The Google and Microsoft apps SDRs connect their mailboxes through, the notification endpoints replies arrive on, and the training ledger stores. Secrets go in Provider keys."
      />
      <DataState
        state={setup}
        errorTitle="Couldn't load the mailbox app setup"
        skeleton={<Skeleton width="100%" height={320} />}
      >
        {(s) => (
          <>
            <section className={styles.section} aria-labelledby="eng-base">
              <h3 id="eng-base" className={styles.heading}>Public base URL</h3>
              {s.public_base_url ? (
                <CopyRow label="Base URL" value={s.public_base_url} />
              ) : (
                <p className={styles.missing}>
                  Not set. Set it under Configuration → Mailboxes &amp; engagement; every URL below is
                  built from it.
                </p>
              )}
              <p className={styles.note}>
                Campaigns and reply desk:{" "}
                <Badge tone={s.campaigns_enabled ? "success" : "neutral"}>
                  {s.campaigns_enabled ? "On" : "Off"}
                </Badge>
              </p>
            </section>

            {s.mailbox_apps.map((app) => (
              <MailboxAppSection key={app.provider} app={app} />
            ))}

            <section className={styles.section} aria-labelledby="eng-gmail-push">
              <h3 id="eng-gmail-push" className={styles.heading}>Gmail reply notifications</h3>
              <CopyRow label="Push endpoint" value={s.gmail_push_endpoint} />
              <CopyRow label="OIDC audience" value={s.gmail_push_audience} />
              <ValueRow label="Topic" value={s.gmail_pubsub_topic} />
              <ValueRow label="Push service account" value={s.gmail_push_service_account} />
            </section>

            <section className={styles.section} aria-labelledby="eng-graph">
              <h3 id="eng-graph" className={styles.heading}>Microsoft reply notifications</h3>
              <CopyRow label="Notification URL" value={s.graph_notification_url} />
            </section>

            <section className={styles.section} aria-labelledby="eng-ledger">
              <h3 id="eng-ledger" className={styles.heading}>Training &amp; insights ledger</h3>
              <ul className={styles.checks}>
                {Object.entries(s.ledger_stores).map(([store, ok]) => (
                  <li key={store}>
                    <Badge tone={ok ? "success" : "warning"} dot>
                      {ok ? "Stored" : "Missing"}
                    </Badge>{" "}
                    {store} store connection (Provider keys → ledger_{store})
                  </li>
                ))}
                <li>
                  <Badge tone={s.pseudonym_secret_configured ? "success" : "warning"} dot>
                    {s.pseudonym_secret_configured ? "Stored" : "Missing"}
                  </Badge>{" "}
                  Pseudonymisation secret (Provider keys → ledger_pseudonym)
                </li>
              </ul>
            </section>
          </>
        )}
      </DataState>
    </div>
  );
}

function MailboxAppSection({ app }: { app: MailboxAppSetup }) {
  const title = app.provider === "google" ? "Google (Gmail)" : "Microsoft 365 (Outlook)";
  return (
    <section className={styles.section} aria-labelledby={`eng-${app.provider}`}>
      <h3 id={`eng-${app.provider}`} className={styles.heading}>
        {title}{" "}
        <Badge tone={app.configured ? "success" : "warning"}>
          {app.configured ? "Configured" : "Not configured"}
        </Badge>
      </h3>
      {app.missing.length > 0 && (
        <ul className={styles.missingList} aria-label={`${title}: still missing`}>
          {app.missing.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
      )}
      <CopyRow label="Redirect URI" value={app.redirect_uri} />
      <ValueRow label="Client id" value={app.client_id} />
      {app.provider === "microsoft" && <ValueRow label="Tenant" value={app.tenant} />}
      <CopyRow label="Scopes" value={app.scopes.join(" ")} />
    </section>
  );
}

function ValueRow({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.row}>
      <span className={styles.label}>{label}</span>
      <code className={styles.value}>{value || "—"}</code>
    </div>
  );
}

function CopyRow({ label, value }: { label: string; value: string }) {
  const toast = useToast();
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      toast.success("Copied", `${label} is on your clipboard.`);
    } catch {
      toast.error("Couldn't copy", "Select the value and copy it by hand.");
    }
  }
  return (
    <div className={styles.row}>
      <span className={styles.label}>{label}</span>
      <code className={styles.value}>{value || "—"}</code>
      <Button size="sm" variant="secondary" onClick={copy} disabled={!value}
        aria-label={`Copy ${label}`}>
        Copy
      </Button>
    </div>
  );
}
```

Create `frontend/src/pages/admin/EngagementSetupTab.module.css`:

```css
.wrap {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

.section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.heading {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0 0 var(--space-1);
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--text);
}

.row {
  display: grid;
  grid-template-columns: minmax(9rem, 12rem) minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--space-3);
}

.label {
  font-size: var(--text-sm);
  color: var(--text-muted);
}

.value {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: var(--text-sm);
  color: var(--text);
}

.note,
.missing {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
  line-height: var(--leading);
}

.missing {
  color: var(--warning);
}

.missingList,
.checks {
  margin: 0;
  padding-left: var(--space-5);
  font-size: var(--text-sm);
  color: var(--text);
  line-height: var(--leading);
}

.missingList {
  color: var(--warning);
}

@media (max-width: 640px) {
  .row {
    grid-template-columns: 1fr;
    align-items: start;
  }
}
```

In `frontend/src/pages/AdminBillingPage.tsx`:
- after `import { FeatureSwitchesTab } from "./admin/FeatureSwitchesTab";` add `import { EngagementSetupTab } from "./admin/EngagementSetupTab";`
- after the `"keys"` tab entry add:

```tsx
    // Beside Provider keys because the two are one job: the secrets go there, and this shows the
    // redirect URIs, notification endpoints and scopes to paste into Google Cloud and Azure.
    ...(can(PROVIDERS_MANAGE) ? [{ value: "engagement", label: "Mailbox apps" }] : []),
```

- after `{tab === "keys" && can(PROVIDERS_MANAGE) && <ProviderKeysTab />}` add:

```tsx
          {tab === "engagement" && can(PROVIDERS_MANAGE) && <EngagementSetupTab />}
```

- [ ] **Step 5: Run the UI test and the typecheck**

Run: `pytest tests/test_engagement_setup_ui.py -n0 -q`
Expected: `3 passed`

Run: `cd frontend && npm run typecheck`
Expected: exits 0 with no errors.

- [ ] **Step 6: Look at it**

Start the dev preview (`preview_start`), sign in as a platform admin, open Control plane → Mailbox apps, and check at desktop and 375 px widths: every section renders, Copy buttons copy, missing pieces are listed in the warning colour, no horizontal scroll.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/admin/EngagementSetupTab.tsx frontend/src/pages/admin/EngagementSetupTab.module.css frontend/src/pages/AdminBillingPage.tsx frontend/src/lib/api.ts frontend/src/lib/types.ts tests/test_engagement_setup_ui.py
git commit -m "feat(engagement): Control plane Mailbox apps tab with copyable setup values"
```

---

### Task 9: Owner guides and the live CI job

**Files:**
- Create: `docs/engagement/setup-google.md`, `docs/engagement/setup-microsoft.md`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Write the guides**

Create `docs/engagement/setup-google.md`:

````markdown
# Connecting Gmail: Google Cloud setup

The owner does these steps in Google Cloud. Nobody pastes a secret into chat, a ticket or a file in
the repo; secrets go only into the Control plane.

You need: a Google Workspace or Google account that can create Cloud projects, and the deployment's
public base URL (for the local deploy, `https://localhost`).

Open **Control plane → Mailbox apps** first. Every value below marked *(copy)* is shown there with a
Copy button; copy it rather than typing it.

## 1. Project

1. Go to <https://console.cloud.google.com/projectcreate>.
2. Name it (for example `nexus-mailboxes`) and create it. Note the **project id**.
3. Select the project in the top bar for every step below.

## 2. Enable the APIs

1. **APIs & Services → Library**.
2. Enable **Gmail API**.
3. Enable **Cloud Pub/Sub API**.

## 3. OAuth consent screen

1. **APIs & Services → OAuth consent screen** (in the new console: **Google Auth Platform →
   Branding / Audience / Data access**).
2. User type: **External**.
3. App name, support email, developer contact email: your company's.
4. Authorised domains: the domain of the public base URL (skip for `localhost`).
5. **Data access → Add or remove scopes**, add exactly:
   - `openid`
   - `.../auth/userinfo.email` (shown as `email`)
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
6. **Audience → Test users**: add the Gmail addresses of the SDRs and the live-test mailbox. While
   the app is in *Testing*, only these can connect, and their refresh tokens expire after 7 days.

## 4. OAuth client

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Web application**. Name: `nexus-mailboxes`.
3. **Authorised redirect URIs → Add URI**: the **Google redirect URI** *(copy)*, which is
   `<public base URL>/api/engagement/mailboxes/oauth/google/callback`. Add one per environment.
4. Create. Copy the **Client ID** and **Client secret** from the dialog.
5. In the Control plane:
   - **Configuration → Mailboxes & engagement → Google OAuth client id**: paste the Client ID, Save.
   - **Provider keys → Add key**: provider **Google OAuth client secret (mailboxes)**, paste the
     secret, Save, then press **Test**. Expected: *probe ok — valid client, not yet authorised by a
     user*. *invalid_client* means the secret or client id is wrong.

## 5. Pub/Sub for reply notifications

Replies still arrive without this (a poll every few minutes), but with it they arrive within seconds.
It needs a public https base URL; it cannot work against `localhost`.

1. **Pub/Sub → Topics → Create topic**, id `gmail-replies`, no default subscription.
2. Open the topic → **Permissions → Add principal**: `gmail-api-push@system.gserviceaccount.com`,
   role **Pub/Sub Publisher**. Without this Gmail cannot publish and every watch fails.
3. **IAM & Admin → Service accounts → Create**: `gmail-push`. No roles needed on the project.
4. Grant your own user **Service Account Token Creator** on that service account if the console
   asks when creating the subscription.
5. **Pub/Sub → Subscriptions → Create subscription**:
   - Topic: `gmail-replies`
   - Delivery type: **Push**
   - Endpoint URL: the **Push endpoint** *(copy)*
   - **Enable authentication**, service account `gmail-push@<project-id>.iam.gserviceaccount.com`,
     audience: the **OIDC audience** *(copy)*
   - Acknowledgement deadline 30 s, retry policy *exponential backoff*.
6. In the Control plane, **Configuration → Mailboxes & engagement**:
   - **Gmail notification topic**: `projects/<project-id>/topics/gmail-replies`
   - **Gmail push service account**: `gmail-push@<project-id>.iam.gserviceaccount.com`

## 6. Verification and CASA (start early — weeks, not days)

`gmail.readonly` and `gmail.compose` are **restricted scopes**. Until Google verifies the app, only
test users can connect, and each connection lapses after 7 days.

1. **OAuth consent screen → Publish app** (moves to *In production*, status *Needs verification*).
2. **Prepare for verification**: homepage, privacy policy and terms URLs on your domain; a YouTube
   (unlisted) video showing the OAuth flow and how each scope is used (connect mailbox → campaign
   send → reply appears in the reply desk).
3. Justification per scope: *gmail.compose* sends the SDR's approved outreach and saves drafts from
   their own mailbox; *gmail.readonly* detects replies to that outreach so follow-ups stop and the SDR
   is alerted; mail not belonging to a conversation the app started is discarded without storing.
4. Submit. Google then requires a **CASA Tier 2** security assessment from an authorised assessor;
   follow the link in the verification email.

## 7. Check

**Control plane → Health**: the *mailbox apps* row reads `google: valid client, not yet authorised
by a user`. After phase 03 ships, an SDR pressing **Connect Google** in Settings → Mailboxes completes
the flow.
````

Create `docs/engagement/setup-microsoft.md`:

````markdown
# Connecting Microsoft 365: Azure setup

The owner does these steps in the Microsoft Entra admin center. Secrets go only into the Control
plane, never into chat, tickets or the repo.

You need: an account that can register applications in Microsoft Entra ID, and the deployment's
public base URL (local deploy: `https://localhost`). Open **Control plane → Mailbox apps** for the
values marked *(copy)*.

## 1. App registration

1. <https://entra.microsoft.com> → **Identity → Applications → App registrations → New
   registration**.
2. Name: `nexus-mailboxes`.
3. Supported account types: **Accounts in any organizational directory and personal Microsoft
   accounts** (matches tenant `common`). Choose *single tenant* only if every SDR is in your own
   directory, and then set the tenant in step 5.
4. Redirect URI: platform **Web**, value: the **Microsoft redirect URI** *(copy)*, which is
   `<public base URL>/api/engagement/mailboxes/oauth/microsoft/callback`.
5. Register. Copy the **Application (client) ID** and **Directory (tenant) ID** from Overview.

## 2. More redirect URIs

**Authentication → Web → Add URI** for each additional environment (local, staging, production).

## 3. API permissions (delegated)

1. **API permissions → Add a permission → Microsoft Graph → Delegated permissions**, add:
   - `openid`, `email`, `offline_access`
   - `User.Read`
   - `Mail.ReadWrite`
   - `Mail.Send`
2. None of these needs admin consent in a default tenant; each SDR consents when they connect. If
   your organisation disables user consent, press **Grant admin consent** here.

## 4. Client secret

1. **Certificates & secrets → Client secrets → New client secret**, description `nexus`, expiry
   **24 months** (put the expiry date in the calendar: an expired secret stops every refresh).
2. Copy the **Value** immediately (not the Secret ID); it is shown once.

## 5. Control plane

1. **Configuration → Mailboxes & engagement**:
   - **Microsoft app (client) id**: the Application (client) ID.
   - **Microsoft tenant**: `common` (or your tenant id / domain for single tenant).
2. **Provider keys → Add key**: provider **Microsoft app client secret (mailboxes)**, paste the value,
   Save, **Test**. Expected: *probe ok — valid client, not yet authorised by a user*.
   `AADSTS7000215` = wrong secret value (you may have copied the Secret ID); `AADSTS7000222` =
   expired; `AADSTS700016` = wrong client id or tenant.

## 6. Reply notifications

Microsoft Graph calls the **Notification URL** *(copy)* when a connected mailbox receives mail. It
must be a public https address with a valid certificate; it cannot work against `localhost`, where
replies arrive on the few-minute poll instead. Nothing to register in Azure: subscriptions are
created per mailbox by the app.

## 7. Publisher verification (for customers outside your organisation)

**Branding & properties → Publisher verification**: associate a verified Microsoft Partner Network
(MPN / Cloud Partner Program) id. Without it, users in other organisations see an "unverified" warning
and many tenants block the consent.

## 8. Check

**Control plane → Health**: the *mailbox apps* row reads `microsoft: valid client, not yet authorised
by a user`.
````

- [ ] **Step 2: Add the live CI job**

In `.github/workflows/ci.yml`, add `  workflow_dispatch:` under `on:` (after the `push` block), and add this job immediately before the `# ---------- Single required status check for branch protection ----------` comment:

```yaml
  # ---------- Live engagement suite: real Google, Microsoft, Supabase, LLM (D21) ----------
  # Not in `quality-gate` until the cutover (phase 15): until the owner has created the test
  # mailboxes and stores, every test here skips with the name of the missing secret. Never runs on
  # pull requests, whose forks cannot read secrets and must not send real mail.
  live-engagement:
    name: Live engagement suite
    runs-on: ubuntu-latest
    timeout-minutes: 30
    if: github.event_name == 'push' || github.event_name == 'workflow_dispatch'
    concurrency:
      # One run at a time: the suite shares real mailboxes and stores.
      group: live-engagement
      cancel-in-progress: false
    env:
      NEXUS_LIVE_REDIRECT_BASE: ${{ secrets.NEXUS_LIVE_REDIRECT_BASE }}
      NEXUS_LIVE_GOOGLE_CLIENT_ID: ${{ secrets.NEXUS_LIVE_GOOGLE_CLIENT_ID }}
      NEXUS_LIVE_GOOGLE_CLIENT_SECRET: ${{ secrets.NEXUS_LIVE_GOOGLE_CLIENT_SECRET }}
      NEXUS_LIVE_MICROSOFT_CLIENT_ID: ${{ secrets.NEXUS_LIVE_MICROSOFT_CLIENT_ID }}
      NEXUS_LIVE_MICROSOFT_CLIENT_SECRET: ${{ secrets.NEXUS_LIVE_MICROSOFT_CLIENT_SECRET }}
      NEXUS_LIVE_MICROSOFT_TENANT: ${{ secrets.NEXUS_LIVE_MICROSOFT_TENANT }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.10", cache: pip }
      - run: pip install -e ".[dev,postgres]"
      - name: Live engagement tests (serial; skips name the missing secret)
        run: pytest tests_live/engagement -n0 -p no:cacheprovider --timeout=300 -q -rs
```

- [ ] **Step 3: Commit**

```bash
git add docs/engagement/setup-google.md docs/engagement/setup-microsoft.md .github/workflows/ci.yml
git commit -m "docs(engagement): Google Cloud and Azure setup guides; live engagement CI job"
```

---

### Task 10: Verify the phase

- [ ] **Step 1: Lint and the full suite**

Run: `ruff check nexus tests tests_live`
Expected: `All checks passed!`

Run: `pytest -n auto -p no:cacheprovider --timeout=120 -q`
Expected: the whole suite passes.

- [ ] **Step 2: Frontend**

Run: `cd frontend && npm run typecheck && npm run build`
Expected: both exit 0.

- [ ] **Step 3: Live, when the owner's secrets exist**

Run: `pytest tests_live/engagement -n0 -q -rs`
Expected: `2 passed`, or `2 skipped` naming the missing secrets.

---

## Spec coverage for this phase

| Spec item | Task |
|---|---|
| §12 secrets in the provider-keys store, sealed, last four only | 2 (store is existing) |
| §12 Test distinguishes an invalid client from a valid client not yet authorised | 4 (+ live test) |
| §12 non-secret settings in runtime settings with validators (client ids, project/topic, tenant `common`, public webhook base URL) | 5 |
| §12 ledger store credentials in the same encrypted store with a Test that checks connectivity and role | 2, 4 (grant check completes in phase 06) |
| §12 guided setup for Google Cloud and Azure | 9 |
| §12 until configured, a plain "not configured" state | 3 (`missing`), 6, 8 |
| §15 Release A dark switch | 1, 5 (`engagement_campaigns_enabled`) |
| §18.2 each store connects with a role scoped to its job | 4 (refuses superuser / `postgres`) |
| D21 live CI job | 4, 9 |
