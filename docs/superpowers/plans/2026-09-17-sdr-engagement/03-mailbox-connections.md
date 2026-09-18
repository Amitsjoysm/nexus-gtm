# Phase 03: Mailbox Connections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let every SDR connect their own Gmail or Microsoft 365 mailbox by OAuth, keep it connected, and give the engine one provider interface to send, draft, read changes and reconcile — proven against real mailboxes.

**Architecture:** `nexus/engagement/mailboxes/` holds the `MailProvider` protocol and its two adapters (`GmailProvider` over Gmail REST v1, `GraphProvider` over Microsoft Graph v1.0), one HTTP path with pure error mapping (`transport.py`), the OAuth flow (`oauth.py`), sealed token bundles with refresh (`tokens.py`) and the connection service (`service.py`). A tenant router serves My mailboxes and the public OAuth callback; a worker job refreshes each connection daily so a revoked grant shows Reconnect before a campaign needs it. Every decision is offline-tested; the adapters are tested live.

**Tech Stack:** httpx, python-jose, stdlib `email`, FastAPI, React + TypeScript.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §3 (`MailProvider`), §5 (threading, limits), §9 (Settings → Mailboxes), §12, D1, D2, D16, D21. **Depends on:** phases 01, 02.

**Verified:** the code in this plan was run in the CI image on top of phases 01–02: `tests/test_engagement_mailboxes.py`, `test_continuous_automation.py`, `test_plan_gated_nav.py`, `test_rls_binding_guard.py`, `test_job_durability.py`, `test_admin_health.py` (96 passed), `ruff check nexus tests tests_live scripts`, `npm run typecheck`, and `pytest tests_live/engagement` (5 skipped, each naming its missing secrets). Amended after the phases 01–05 full-suite run (2026-09-18): `tests/test_crm_auto_sync.py` asserts the whole set of enqueued jobs, so it needs `refresh_mailbox_tokens` too. The live round trip itself runs once the owner's test mailboxes exist.

---

## Owner prerequisites

The code merges without these; the live tests skip until they exist. Follow `docs/engagement/live-tests.md` (created in Task 9): two test mailboxes, a Gmail delete filter for `[nexus-live`, the `http://localhost:8765/callback` redirect URI on both apps, refresh tokens from `scripts/engagement_live_token.py`, and the four GitHub secrets `NEXUS_LIVE_GMAIL_ADDRESS`, `NEXUS_LIVE_GMAIL_REFRESH_TOKEN`, `NEXUS_LIVE_M365_ADDRESS`, `NEXUS_LIVE_M365_REFRESH_TOKEN`.

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Create | `nexus/engagement/mailboxes/__init__.py` | package |
| Create | `nexus/engagement/mailboxes/provider.py` | protocol, value types, errors |
| Create | `nexus/engagement/mailboxes/transport.py` | authenticated requests, pure error mapping |
| Create | `nexus/engagement/mailboxes/oauth.py` | state, authorize URL, code exchange, refresh, revoke |
| Create | `nexus/engagement/mailboxes/tokens.py` | sealing, expiry, `fresh_access_token` |
| Create | `nexus/engagement/mailboxes/gmail.py` | `GmailProvider` |
| Create | `nexus/engagement/mailboxes/graph.py` | `GraphProvider`, conversation index |
| Create | `nexus/engagement/mailboxes/registry.py` | connection → provider |
| Create | `nexus/engagement/mailboxes/service.py` | connect, list, edit, check, disconnect |
| Create | `nexus/api/routers/engagement_mailboxes.py` | My mailboxes API + OAuth callback |
| Modify | `nexus/api/routers/__init__.py` | register the router |
| Modify | `nexus/workers/tasks.py`, `nexus/workers/scheduler.py` | `refresh_mailbox_tokens` job |
| Modify | `tests/test_continuous_automation.py`, `tests/test_crm_auto_sync.py` | the scheduler enqueues one more job |
| Create | `frontend/src/pages/engagement/MailboxesPage.tsx` (+ `.module.css`) | My mailboxes |
| Modify | `frontend/src/App.tsx`, `frontend/src/app/nav.tsx`, `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts` | route, nav item, client |
| Create | `tests/test_engagement_mailboxes.py` | offline behaviour |
| Create | `tests_live/engagement/test_mailboxes_live.py` | Gmail ⇄ Microsoft round trip |
| Create | `scripts/engagement_live_token.py` | owner's refresh-token helper |
| Create | `docs/engagement/live-tests.md` | owner guide for the live suite |
| Modify | `.github/workflows/ci.yml` | four more live secrets |

---

### Task 1: The provider contract

**Files:**
- Create: `nexus/engagement/mailboxes/__init__.py`, `nexus/engagement/mailboxes/provider.py`

- [ ] **Step 1: Create the package and contract**

`nexus/engagement/mailboxes/__init__.py`:

```python
"""SDR mailboxes connected by OAuth (spec §3 mailboxes/)."""
```

`nexus/engagement/mailboxes/provider.py`:

```python
"""The contract every mailbox provider implements (spec §3), and the errors it may raise.

A provider instance is bound to ONE connected mailbox and one live access token; the caller gets it
from ``registry.provider_for`` after ``tokens.fresh_access_token``. Methods take and return plain
values, so the engine above never sees Gmail's or Graph's shapes.

There are exactly two implementations, ``GmailProvider`` and ``GraphProvider``, and deliberately no
third, fake one (D21): the engine's decisions are pure functions tested offline, and these adapters
are exercised against real mailboxes in ``tests_live/engagement``.

The error classes are the vocabulary the sending and sync code branch on, so they carry what the
caller needs to decide, not what the provider said:

* ``AuthExpired`` — the refresh token is revoked or expired. The mailbox needs the SDR to reconnect;
  retrying cannot help.
* ``ProviderLimit`` — a quota or rate limit. ``retry_at`` is when to try again (spec §5: the mailbox
  pauses until then; nothing is lost or duplicated).
* ``CursorExpired`` — the stored sync cursor is too old; resynchronise from a recent window.
* ``NotFound`` — the message or thread no longer exists (deleted by the SDR).
* ``TransientError`` — a 5xx or a network failure. Safe to retry the SAME operation later; for a
  send, reconcile first (spec §5 idempotency).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class ProviderError(RuntimeError):
    """Base class. ``detail`` is the provider's own words, trimmed; never a token."""

    def __init__(self, detail: str = "", *, status: int | None = None):
        super().__init__(detail)
        self.detail = detail
        self.status = status


class AuthExpired(ProviderError):
    pass


class ProviderLimit(ProviderError):
    def __init__(self, detail: str = "", *, status: int | None = None,
                 retry_at: datetime | None = None):
        super().__init__(detail, status=status)
        self.retry_at = retry_at


class CursorExpired(ProviderError):
    pass


class NotFound(ProviderError):
    pass


class TransientError(ProviderError):
    pass


@dataclass(frozen=True, slots=True)
class MailboxProfile:
    email: str
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class ThreadRef:
    """Where a reply or follow-up goes (D16): the provider's thread, and the message it answers."""

    provider_thread_id: str
    #: The latest message in the conversation, by the provider's id (Graph needs it for the
    #: conversation index) and by its RFC Message-ID (In-Reply-To).
    reply_to_provider_message_id: str = ""
    in_reply_to: str = ""
    references: str = ""


@dataclass(frozen=True, slots=True)
class SentRef:
    provider_message_id: str
    provider_thread_id: str
    rfc_message_id: str = ""


@dataclass(frozen=True, slots=True)
class InboundMessage:
    provider_message_id: str
    provider_thread_id: str
    raw: bytes
    received_at: datetime
    #: True when the mailbox itself sent it (Gmail SENT label; Graph from == mailbox address).
    outgoing: bool = False


@dataclass(frozen=True, slots=True)
class ChangeBatch:
    """Provider message ids that appeared since ``cursor``, oldest first, and the next cursor."""

    message_ids: list[str] = field(default_factory=list)
    next_cursor: str = ""


class MailProvider(Protocol):
    provider: str

    async def profile(self) -> MailboxProfile: ...

    async def send(self, mime: bytes, *, thread: ThreadRef | None) -> SentRef: ...

    async def create_draft(self, mime: bytes, *, thread: ThreadRef | None) -> str: ...

    async def fetch_changes(self, cursor: str | None) -> ChangeBatch: ...

    async def get_message(self, provider_message_id: str) -> InboundMessage: ...

    async def find_sent(
        self, *, ref_header: str, to: str, around: datetime
    ) -> SentRef | None: ...

    async def search_sent(
        self, *, to: str, subject: str, around: datetime
    ) -> SentRef | None: ...
```

- [ ] **Step 2: Check it imports**

Run: `python -c "from nexus.engagement.mailboxes.provider import MailProvider, ProviderLimit; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add nexus/engagement/mailboxes/__init__.py nexus/engagement/mailboxes/provider.py
git commit -m "feat(engagement): MailProvider contract, value types and errors"
```

---

### Task 2: One HTTP path with pure error mapping

**Files:**
- Create: `nexus/engagement/mailboxes/transport.py`
- Test: `tests/test_engagement_mailboxes.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_engagement_mailboxes.py` with the module docstring, imports, `NOW`, and `test_errors_map_to_what_the_caller_must_do` from the final file in Task 8 Step 1.

- [ ] **Step 2: Run to see it fail**

Run: `pytest tests/test_engagement_mailboxes.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.mailboxes.transport'`

- [ ] **Step 3: Implement**

`nexus/engagement/mailboxes/transport.py` (named `transport`, not `http`, so nothing in the package can shadow the standard library):

```python
"""One HTTP path for both mailbox providers: bearer auth, timeouts, and error mapping.

``classify_error`` is pure — status, headers and body text in, a ``ProviderError`` out — so the
rules for "this is a quota, pause until X" are tested offline on the cases they encode, and the real
payloads are exercised by the live suite.

Retry policy is deliberately NOT here. A GET can be retried blindly; a send cannot (spec §5): it
must be reconciled against the Sent folder first. So this module raises, and each caller decides.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from nexus.engagement.mailboxes.provider import (
    AuthExpired,
    NotFound,
    ProviderError,
    ProviderLimit,
    TransientError,
)

TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: When a limit carries no reset time. A quota that resets daily and says so is honoured exactly;
#: one that says nothing gets an hour, which is long enough not to hammer and short enough that a
#: burst limit does not stall a campaign for a day.
DEFAULT_LIMIT_BACKOFF = timedelta(hours=1)
DAILY_LIMIT_BACKOFF = timedelta(hours=24)

_RETRY_AFTER_ISO = re.compile(r"retry after (\d{4}-\d{2}-\d{2}T[0-9:.]+Z)", re.IGNORECASE)
_DAILY = re.compile(r"daily|per day|dailylimit|submission quota|recipient.*limit", re.IGNORECASE)
_LIMIT = re.compile(
    r"rate ?limit|quota|too many|throttl|userRateLimitExceeded|MailboxConcurrency",
    re.IGNORECASE,
)


def _retry_after(headers: dict, body: str, now: datetime) -> datetime | None:
    raw = (headers.get("retry-after") or headers.get("Retry-After") or "").strip()
    if raw.isdigit():
        return now + timedelta(seconds=int(raw))
    match = _RETRY_AFTER_ISO.search(body or "")
    if match:
        try:
            return datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def classify_error(status: int, headers: dict, body: str, *, now: datetime | None = None
                   ) -> ProviderError:
    """The ``ProviderError`` a non-2xx response means."""
    now = now or datetime.now(timezone.utc)
    text = (body or "")[:500]
    if status == 401:
        return AuthExpired(text, status=status)
    if status == 404:
        return NotFound(text, status=status)
    if status == 429 or (status == 403 and _LIMIT.search(text)):
        retry_at = _retry_after(headers, text, now)
        if retry_at is None:
            retry_at = now + (DAILY_LIMIT_BACKOFF if _DAILY.search(text) else DEFAULT_LIMIT_BACKOFF)
        return ProviderLimit(text, status=status, retry_at=retry_at)
    if status >= 500:
        return TransientError(text, status=status)
    return ProviderError(text, status=status)


async def request(
    method: str, url: str, *, token: str, params: dict | None = None, json: dict | None = None,
    content: bytes | str | None = None, headers: dict | None = None,
) -> httpx.Response:
    """Issue one authenticated request; raise the mapped ``ProviderError`` on any non-2xx."""
    merged = {"Authorization": f"Bearer {token}", **(headers or {})}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.request(method, url, params=params, json=json, content=content,
                                        headers=merged)
    except httpx.HTTPError as exc:
        raise TransientError(f"{type(exc).__name__}: could not reach the provider") from exc
    if resp.status_code >= 400:
        raise classify_error(resp.status_code, dict(resp.headers), resp.text)
    return resp
```

- [ ] **Step 4: Run to see it pass**

Run: `pytest tests/test_engagement_mailboxes.py -n0 -q`
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/mailboxes/transport.py tests/test_engagement_mailboxes.py
git commit -m "feat(engagement): provider transport with quota, auth and transient error mapping"
```

---

### Task 3: OAuth and tokens

**Files:**
- Create: `nexus/engagement/mailboxes/oauth.py`, `nexus/engagement/mailboxes/tokens.py`
- Test: `tests/test_engagement_mailboxes.py`

- [ ] **Step 1: Write the failing tests**

Add `_app`, `test_the_state_round_trips_and_refuses_another_provider_or_a_network_state`, `test_authorize_urls_ask_for_offline_access_and_pkce`, `test_a_grant_missing_mail_access_is_detected`, `test_a_refresh_keeps_the_old_refresh_token_when_the_provider_omits_one` and `test_token_lifetimes` from the final file (Task 8 Step 1).

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_mailboxes.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.mailboxes.oauth'`

- [ ] **Step 3: Implement OAuth**

`nexus/engagement/mailboxes/oauth.py`:

```python
"""OAuth for connecting an SDR's own mailbox: authorize URL, code exchange, refresh (D1, D2).

The state round-tripped through Google or Microsoft is a short-lived signed JWT carrying the user,
tenant, provider, PKCE verifier and the browser's timezone, so the callback resumes with no
server-side session store. Its ``typ`` differs from the network connectors' state, so a state minted
for one flow is refused by the other.

Token requests go through ``post_token``, which maps the two refusals that matter:

* ``invalid_grant`` on a refresh — the refresh token is revoked or expired → ``AuthExpired``; the
  mailbox needs the SDR to reconnect.
* anything else 4xx → ``ProviderError`` with the provider's words.
"""
from __future__ import annotations

import time
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from jose import JWTError, jwt

from nexus.core.config import get_settings
from nexus.core.db import utcnow
from nexus.engagement.config import GOOGLE, MICROSOFT, OAuthApp
from nexus.engagement.mailboxes.provider import AuthExpired, ProviderError, TransientError
from nexus.network.oauth import make_pkce

_STATE_TYP = "mailbox_oauth"
_STATE_TTL_S = 600
GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"


def microsoft_authorize(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant or 'common'}/oauth2/v2.0/authorize"


def microsoft_token(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant or 'common'}/oauth2/v2.0/token"


def sign_state(*, user_id: str, tenant_id: str, provider: str, verifier: str, timezone: str) -> str:
    s = get_settings()
    now = utcnow()
    claims = {
        "typ": _STATE_TYP, "uid": user_id, "tid": tenant_id, "prov": provider, "pkce": verifier,
        "tz": (timezone or "UTC")[:64],
        "iat": int(now.timestamp()), "exp": int((now + timedelta(seconds=_STATE_TTL_S)).timestamp()),
    }
    return jwt.encode(claims, s.secret_key, algorithm=s.jwt_algorithm)


def verify_state(token: str, *, provider: str) -> dict | None:
    s = get_settings()
    try:
        claims = jwt.decode(token or "", s.secret_key, algorithms=[s.jwt_algorithm])
    except JWTError:
        return None
    if claims.get("typ") != _STATE_TYP or claims.get("prov") != provider:
        return None
    if not (claims.get("uid") and claims.get("tid") and claims.get("pkce")):
        return None
    return claims


def authorize_url(app: OAuthApp, *, state: str, challenge: str, login_hint: str = "") -> str:
    params = {
        "client_id": app.client_id, "redirect_uri": app.redirect_uri, "response_type": "code",
        "scope": " ".join(app.scopes), "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if app.provider == GOOGLE:
        # offline + consent: a refresh token is issued on every connect, not only the first.
        params.update({"access_type": "offline", "prompt": "consent"})
        base = GOOGLE_AUTHORIZE
    elif app.provider == MICROSOFT:
        params.update({"response_mode": "query", "prompt": "select_account"})
        base = microsoft_authorize(app.tenant)
    else:
        raise ValueError(f"unknown mailbox provider {app.provider!r}")
    if login_hint:
        params["login_hint"] = login_hint
    return f"{base}?{urlencode(params)}"


def start(app: OAuthApp, *, user_id: str, tenant_id: str, timezone: str) -> str:
    """A ready-to-open authorize URL with fresh PKCE and a signed state."""
    verifier, challenge = make_pkce()
    state = sign_state(user_id=user_id, tenant_id=tenant_id, provider=app.provider,
                       verifier=verifier, timezone=timezone)
    return authorize_url(app, state=state, challenge=challenge)


def missing_scopes(app: OAuthApp, granted: str) -> list[str]:
    """Scopes we asked for that the grant does not include. A user can untick Gmail access on
    Google's consent screen; a mailbox without it would connect and then fail every sync."""
    have = {s.strip().lower() for s in (granted or "").split() if s.strip()}
    # Microsoft reports Graph scopes without the resource prefix ("Mail.Send"); openid/email may be
    # omitted from the granted list even when granted.
    have |= {f"https://graph.microsoft.com/{s}" for s in list(have) if "/" not in s}
    required = [s for s in app.scopes if s not in ("openid", "email", "offline_access")]
    return [s for s in required if s.lower() not in have]


def bundle_from_response(data: dict, *, previous: dict | None = None) -> dict:
    """The token bundle to seal. Google omits ``refresh_token`` on refresh, so the old one is kept;
    Microsoft rotates it, so the new one wins."""
    previous = previous or {}
    now = int(time.time())
    return {
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token") or previous.get("refresh_token", ""),
        "expires_at": now + int(data.get("expires_in") or 3600),
        "scope": data.get("scope") or previous.get("scope", ""),
        "token_type": data.get("token_type", "Bearer"),
        "refreshed_at": now,
    }


def token_url(app: OAuthApp) -> str:
    return GOOGLE_TOKEN if app.provider == GOOGLE else microsoft_token(app.tenant)


async def post_token(app: OAuthApp, form: dict) -> dict:
    data = {"client_id": app.client_id, "client_secret": app.client_secret, **form}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(token_url(app), data=data)
    except httpx.HTTPError as exc:
        raise TransientError(f"{type(exc).__name__}: could not reach the token endpoint") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 500:
        raise TransientError(resp.text[:300], status=resp.status_code)
    if resp.status_code >= 400:
        detail = str(body.get("error_description") or body.get("error") or resp.text)[:300]
        if body.get("error") == "invalid_grant" and form.get("grant_type") == "refresh_token":
            raise AuthExpired(detail, status=resp.status_code)
        raise ProviderError(detail, status=resp.status_code)
    return body


async def exchange_code(app: OAuthApp, *, code: str, verifier: str) -> dict:
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": app.redirect_uri,
            "code_verifier": verifier}
    if app.provider == MICROSOFT:
        form["scope"] = " ".join(app.scopes)
    return await post_token(app, form)


async def refresh(app: OAuthApp, refresh_token: str) -> dict:
    if not refresh_token:
        raise AuthExpired("no refresh token is stored for this mailbox")
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if app.provider == MICROSOFT:
        form["scope"] = " ".join(app.scopes)
    return await post_token(app, form)


async def revoke(app: OAuthApp, bundle: dict) -> None:
    """Best effort. Google revokes the grant; Microsoft has no per-app revoke for delegated tokens
    short of signing the user out everywhere, so locally deleting the tokens is the disconnect."""
    token = bundle.get("refresh_token") or bundle.get("access_token")
    if app.provider != GOOGLE or not token:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(GOOGLE_REVOKE, data={"token": token})
    except httpx.HTTPError:
        return
```

- [ ] **Step 4: Implement tokens**

`nexus/engagement/mailboxes/tokens.py`:

```python
"""Sealed token bundles and "give me a working access token for this mailbox".

Tokens are sealed with the same Fernet helper the network connectors use
(``nexus.network.crypto``, key ``network_token_enc_key`` or derived from ``secret_key``) and never
leave the server. A bundle that cannot be unsealed reads as empty, which ``fresh_access_token``
turns into ``needs_reauth`` — reconnecting fixes it, a crash would not.
"""
from __future__ import annotations

import time

from nexus.core.db import utcnow
from nexus.engagement.mailboxes.provider import AuthExpired, ProviderError, TransientError
from nexus.network.crypto import seal_tokens, unseal_tokens

#: Refresh this many seconds before expiry, so a token never expires mid-request.
EXPIRY_SKEW_S = 120
#: A connected mailbox is refreshed at least this often, so a revoked grant is noticed within a day
#: rather than at the moment a campaign tries to send.
LIVENESS_INTERVAL_S = 24 * 3600


def seal(bundle: dict) -> dict:
    return seal_tokens(bundle)


def unseal(stored: dict | None) -> dict:
    return unseal_tokens(stored)


def needs_refresh(bundle: dict, *, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    if not bundle.get("access_token"):
        return True
    return int(bundle.get("expires_at") or 0) - EXPIRY_SKEW_S <= now


def liveness_due(bundle: dict, *, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    return now - int(bundle.get("refreshed_at") or 0) >= LIVENESS_INTERVAL_S


async def fresh_access_token(ts, connection, *, force: bool = False) -> str:
    """A usable access token for ``connection``, refreshing and re-sealing when needed.

    On a revoked or expired grant the connection is marked ``needs_reauth`` (in the caller's
    transaction) and ``AuthExpired`` is raised.
    """
    from nexus.engagement.config import oauth_app
    from nexus.engagement.mailboxes import oauth

    if connection.status in ("revoked",):
        raise AuthExpired("this mailbox was disconnected")
    bundle = unseal(connection.tokens)
    if not force and not needs_refresh(bundle):
        return bundle["access_token"]
    app = await oauth_app(connection.provider)
    try:
        data = await oauth.refresh(app, bundle.get("refresh_token", ""))
    except AuthExpired as exc:
        connection.status = "needs_reauth"
        connection.last_error = f"Reconnect this mailbox: {exc.detail}"[:500]
        await ts.flush()
        raise
    except TransientError:
        raise  # the provider is unreachable; nothing is known about the grant
    except ProviderError as exc:
        # invalid_client and friends: the APP is misconfigured (a rotated client secret), which no
        # amount of reconnecting by the SDR can fix. Reported as `error` so the screen says so.
        connection.status = "error"
        connection.last_error = f"The mailbox app is misconfigured: {exc.detail}"[:500]
        await ts.flush()
        raise
    bundle = oauth.bundle_from_response(data, previous=bundle)
    connection.tokens = seal(bundle)
    if connection.status != "connected":
        connection.status = "connected"
    connection.last_error = None
    connection.updated_at = utcnow()
    await ts.flush()
    return bundle["access_token"]
```

- [ ] **Step 5: Run to see them pass**

Run: `pytest tests/test_engagement_mailboxes.py -n0 -q`
Expected: `6 passed`

- [ ] **Step 6: Commit**

```bash
git add nexus/engagement/mailboxes/oauth.py nexus/engagement/mailboxes/tokens.py tests/test_engagement_mailboxes.py
git commit -m "feat(engagement): mailbox OAuth with PKCE, scope checks and sealed refreshing tokens"
```

---

### Task 4: The Gmail adapter

**Files:**
- Create: `nexus/engagement/mailboxes/gmail.py`

- [ ] **Step 1: Implement**

`nexus/engagement/mailboxes/gmail.py`:

```python
"""GmailProvider: one connected Gmail mailbox through the Gmail REST API v1.

Scopes (spec §12): ``gmail.readonly`` for history, messages and watch; ``gmail.compose`` for
``messages.send`` and drafts. Both are restricted scopes (Google verification + CASA).

Four behaviours worth knowing:

* **The Message-ID is read back after sending.** Gmail may rewrite the Message-ID of a message sent
  through the API; the id stored for threading and reply matching is the one Gmail actually sent,
  read from the sent message's headers.
* **Sent and draft messages are not "changes".** ``history.list`` reports our own sends as
  ``messageAdded`` with the SENT label; reply detection wants what arrived, so SENT, DRAFT and CHAT
  are skipped.
* **The first sync starts now.** With no cursor, the current ``historyId`` becomes the cursor and no
  messages are returned: the mailbox's past is not read, only what arrives after it is connected.
* **A cursor Gmail has forgotten is ``CursorExpired``** (``history.list`` answers 404 once a
  ``historyId`` falls out of its retention); the sync resynchronises from a recent window.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from nexus.engagement.mailboxes import transport
from nexus.engagement.mailboxes.provider import (
    ChangeBatch,
    CursorExpired,
    InboundMessage,
    MailboxProfile,
    NotFound,
    SentRef,
    ThreadRef,
)
from nexus.engagement.subjects import normalize_subject

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_SKIP_LABELS = {"SENT", "DRAFT", "CHAT"}
_MAX_PAGES = 50


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def from_b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


class GmailProvider:
    provider = "google"

    def __init__(self, *, access_token: str, email: str):
        self._token = access_token
        self.email = (email or "").lower()

    async def _get(self, path: str, params=None) -> dict:
        resp = await transport.request("GET", f"{BASE}{path}", token=self._token, params=params)
        return resp.json()

    async def _post(self, path: str, body: dict) -> dict:
        resp = await transport.request("POST", f"{BASE}{path}", token=self._token, json=body)
        return resp.json() if resp.content else {}

    async def _headers(self, message_id: str, names: tuple[str, ...]) -> tuple[dict, dict]:
        params = [("format", "metadata")] + [("metadataHeaders", n) for n in names]
        data = await self._get(f"/messages/{message_id}", params=params)
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in (data.get("payload") or {}).get("headers", [])
        }
        return data, headers

    async def profile(self) -> MailboxProfile:
        data = await self._get("/profile")
        return MailboxProfile(email=str(data.get("emailAddress", "")).lower())

    async def send(self, mime: bytes, *, thread: ThreadRef | None) -> SentRef:
        body: dict = {"raw": b64url(mime)}
        if thread and thread.provider_thread_id:
            body["threadId"] = thread.provider_thread_id
        sent = await self._post("/messages/send", body)
        _data, headers = await self._headers(sent["id"], ("Message-ID",))
        return SentRef(
            provider_message_id=sent["id"], provider_thread_id=sent.get("threadId", ""),
            rfc_message_id=headers.get("message-id", ""),
        )

    async def create_draft(self, mime: bytes, *, thread: ThreadRef | None) -> str:
        message: dict = {"raw": b64url(mime)}
        if thread and thread.provider_thread_id:
            message["threadId"] = thread.provider_thread_id
        draft = await self._post("/drafts", {"message": message})
        return str(draft.get("id", ""))

    async def fetch_changes(self, cursor: str | None) -> ChangeBatch:
        if not cursor:
            profile = await self._get("/profile")
            return ChangeBatch(message_ids=[], next_cursor=str(profile.get("historyId", "")))
        ids: list[str] = []
        seen: set[str] = set()
        latest = cursor
        page = None
        for _ in range(_MAX_PAGES):
            params = {"startHistoryId": cursor, "historyTypes": "messageAdded", "maxResults": 500}
            if page:
                params["pageToken"] = page
            try:
                data = await self._get("/history", params=params)
            except NotFound as exc:
                raise CursorExpired(exc.detail, status=exc.status) from exc
            for entry in data.get("history", []) or []:
                for added in entry.get("messagesAdded", []) or []:
                    message = added.get("message") or {}
                    labels = set(message.get("labelIds") or [])
                    mid = message.get("id")
                    if not mid or mid in seen or labels & _SKIP_LABELS:
                        continue
                    seen.add(mid)
                    ids.append(mid)
            latest = str(data.get("historyId") or latest)
            page = data.get("nextPageToken")
            if not page:
                break
        return ChangeBatch(message_ids=ids, next_cursor=latest)

    async def resync(self, since: datetime) -> ChangeBatch:
        """Messages received after ``since`` (bounded), and a fresh cursor. Used after
        ``CursorExpired``; duplicates are harmless because messages are unique per mailbox."""
        profile = await self._get("/profile")
        query = f"after:{_epoch(since)} -in:sent -in:drafts -in:chats"
        data = await self._get("/messages", params={"q": query, "maxResults": 200})
        ids = [m["id"] for m in reversed(data.get("messages", []) or []) if m.get("id")]
        return ChangeBatch(message_ids=ids, next_cursor=str(profile.get("historyId", "")))

    async def get_message(self, provider_message_id: str) -> InboundMessage:
        data = await self._get(f"/messages/{provider_message_id}", params={"format": "raw"})
        received = datetime.fromtimestamp(int(data.get("internalDate", "0")) / 1000, tz=timezone.utc)
        return InboundMessage(
            provider_message_id=data["id"], provider_thread_id=data.get("threadId", ""),
            raw=from_b64url(data.get("raw", "")), received_at=received,
            outgoing="SENT" in set(data.get("labelIds") or []),
        )

    async def _sent_candidates(self, *, to: str, start: datetime, end: datetime) -> list[dict]:
        query = f"in:sent to:{to} after:{_epoch(start)} before:{_epoch(end)}"
        data = await self._get("/messages", params={"q": query, "maxResults": 25})
        return data.get("messages", []) or []

    async def find_sent(self, *, ref_header: str, to: str, around: datetime) -> SentRef | None:
        start, end = around - timedelta(days=2), around + timedelta(days=1)
        for candidate in await self._sent_candidates(to=to, start=start, end=end):
            _data, headers = await self._headers(candidate["id"], ("X-Nexus-Ref", "Message-ID"))
            if headers.get("x-nexus-ref", "").strip() == ref_header:
                return SentRef(candidate["id"], candidate.get("threadId", ""),
                               headers.get("message-id", ""))
        return None

    async def search_sent(self, *, to: str, subject: str, around: datetime) -> SentRef | None:
        wanted = normalize_subject(subject).lower()
        best: tuple[float, SentRef] | None = None
        start, end = around - timedelta(days=3), around + timedelta(days=3)
        for candidate in await self._sent_candidates(to=to, start=start, end=end):
            data, headers = await self._headers(candidate["id"], ("Subject", "Message-ID"))
            if normalize_subject(headers.get("subject", "")).lower() != wanted:
                continue
            sent_at = datetime.fromtimestamp(int(data.get("internalDate", "0")) / 1000,
                                             tz=timezone.utc)
            distance = abs((sent_at - around).total_seconds())
            ref = SentRef(candidate["id"], candidate.get("threadId", ""),
                          headers.get("message-id", ""))
            if best is None or distance < best[0]:
                best = (distance, ref)
        return best[1] if best else None
```

- [ ] **Step 2: Check it imports and lints**

Run: `python -c "from nexus.engagement.mailboxes.gmail import GmailProvider; print('ok')" && ruff check nexus/engagement/mailboxes`
Expected: `ok` then `All checks passed!` (behaviour is proven live in Task 9).

- [ ] **Step 3: Commit**

```bash
git add nexus/engagement/mailboxes/gmail.py
git commit -m "feat(engagement): GmailProvider - send, drafts, history changes, sent reconciliation"
```

---

### Task 5: The Microsoft Graph adapter

**Files:**
- Create: `nexus/engagement/mailboxes/graph.py`
- Test: `tests/test_engagement_mailboxes.py`

- [ ] **Step 1: Write the failing tests**

Add `test_a_follow_up_extends_the_parents_conversation_index` and `test_a_header_is_set_without_touching_the_body` from the final file (Task 8 Step 1).

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_mailboxes.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.mailboxes.graph'`

- [ ] **Step 3: Implement**

`nexus/engagement/mailboxes/graph.py`:

```python
"""GraphProvider: one connected Microsoft 365 / Outlook mailbox through Microsoft Graph v1.0.

Scopes (spec §12): ``Mail.ReadWrite`` (read replies, create drafts), ``Mail.Send``, ``User.Read``,
``offline_access``.

**Why messages are imported as MIME rather than built with Graph's JSON.** The unsubscribe headers
(``List-Unsubscribe``, ``List-Unsubscribe-Post``, D11) are not ``X-`` headers, and Graph's JSON API
only accepts custom ``X-`` headers. So every send creates a draft from the complete RFC 5322 message
(``POST /me/messages`` with a base64 MIME body) and then sends that draft. Creating it first also
gives us the message id, conversation id and Message-ID, which ``sendMail`` never returns.

**Immutable ids.** Every call carries ``Prefer: IdType="ImmutableId"``: a draft moves to Sent Items
when it is sent, and a regular Graph id changes when a message changes folder. The immutable id the
draft had is the id the sent message keeps.

**Staying in the same conversation (D16).** References/In-Reply-To keep the RECIPIENT's thread. The
SDR's own mailbox groups a conversation by its conversation index, so a follow-up copies the parent's
``Thread-Index`` and appends a child block (MS-OXOMSG 2.2.1.3); without it Exchange starts a new
conversation. ``tests_live/engagement/test_mailboxes_live.py`` asserts the conversation id holds.

**Changes are a timestamp window**, not a delta token: ``receivedDateTime ge <cursor>`` across all
folders, excluding drafts and our own sends. It survives folder moves and rules, never expires, and
the boundary duplicate it returns is rejected by the unique message index.
"""
from __future__ import annotations

import base64
import os
import uuid
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser

from nexus.engagement.mailboxes import transport
from nexus.engagement.mailboxes.provider import (
    ChangeBatch,
    InboundMessage,
    MailboxProfile,
    SentRef,
    ThreadRef,
)
from nexus.engagement.subjects import normalize_subject

GRAPH = "https://graph.microsoft.com/v1.0"
IMMUTABLE = {"Prefer": 'IdType="ImmutableId"'}
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_MAX_PAGES = 50


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filetime(moment: datetime) -> int:
    return int((moment - _FILETIME_EPOCH).total_seconds() * 10_000_000)


def new_thread_index(now: datetime) -> str:
    """A conversation header block: 6 bytes of FILETIME (whose first byte is the reserved 0x01 for
    any date this century) followed by a random 16-byte GUID."""
    header = _filetime(now).to_bytes(8, "big")[:6] + uuid.uuid4().bytes
    return base64.b64encode(header).decode("ascii")


def child_thread_index(parent: str | None, now: datetime) -> str:
    """The parent's index plus one 5-byte child block. A missing or malformed parent starts a new
    conversation rather than raising."""
    try:
        raw = base64.b64decode(parent or "", validate=True)
    except (ValueError, TypeError):
        raw = b""
    if len(raw) < 22 or (len(raw) - 22) % 5:
        return new_thread_index(now)
    header_time = int.from_bytes(raw[:6] + b"\x00\x00", "big")
    delta = max(0, _filetime(now) - header_time)
    if delta < (1 << 49):
        block = (delta >> 18) & 0x7FFFFFFF
    else:
        block = (1 << 31) | ((delta >> 23) & 0x7FFFFFFF)
    child = block.to_bytes(4, "big") + os.urandom(1)
    return base64.b64encode(raw + child).decode("ascii")


def with_header(mime: bytes, name: str, value: str) -> bytes:
    message = BytesParser(policy=policy.SMTP).parsebytes(mime)
    del message[name]
    message[name] = value
    return message.as_bytes(policy=policy.SMTP)


class GraphProvider:
    provider = "microsoft"

    def __init__(self, *, access_token: str, email: str):
        self._token = access_token
        self.email = (email or "").lower()

    async def _get(self, url: str, params: dict | None = None) -> dict:
        full = url if url.startswith("https://") else f"{GRAPH}{url}"
        resp = await transport.request("GET", full, token=self._token, params=params,
                                       headers=IMMUTABLE)
        return resp.json()

    async def profile(self) -> MailboxProfile:
        data = await self._get("/me", {"$select": "mail,userPrincipalName,displayName"})
        email = data.get("mail") or data.get("userPrincipalName") or ""
        return MailboxProfile(email=str(email).lower(), display_name=data.get("displayName") or "")

    async def _thread_index_of(self, message_id: str) -> str | None:
        data = await self._get(f"/me/messages/{message_id}", {"$select": "internetMessageHeaders"})
        for header in data.get("internetMessageHeaders") or []:
            if str(header.get("name", "")).lower() == "thread-index":
                return header.get("value")
        return None

    async def _import(self, mime: bytes, thread: ThreadRef | None) -> dict:
        if thread and thread.reply_to_provider_message_id:
            parent = await self._thread_index_of(thread.reply_to_provider_message_id)
            mime = with_header(mime, "Thread-Index",
                               child_thread_index(parent, datetime.now(timezone.utc)))
        resp = await transport.request(
            "POST", f"{GRAPH}/me/messages", token=self._token,
            content=base64.b64encode(mime), headers={**IMMUTABLE, "Content-Type": "text/plain"},
        )
        return resp.json()

    async def send(self, mime: bytes, *, thread: ThreadRef | None) -> SentRef:
        draft = await self._import(mime, thread)
        await transport.request("POST", f"{GRAPH}/me/messages/{draft['id']}/send",
                                token=self._token, headers=IMMUTABLE)
        return SentRef(
            provider_message_id=draft["id"], provider_thread_id=draft.get("conversationId", ""),
            rfc_message_id=draft.get("internetMessageId", ""),
        )

    async def create_draft(self, mime: bytes, *, thread: ThreadRef | None) -> str:
        draft = await self._import(mime, thread)
        return str(draft.get("id", ""))

    async def _window(self, since: str) -> ChangeBatch:
        ids: list[str] = []
        latest = since
        url: str | None = f"{GRAPH}/me/messages"
        params: dict | None = {
            "$filter": f"receivedDateTime ge {since} and isDraft eq false",
            "$orderby": "receivedDateTime asc",
            "$select": "id,receivedDateTime,from",
            "$top": "50",
        }
        for _ in range(_MAX_PAGES):
            if url is None:
                break
            data = await self._get(url, params)
            for message in data.get("value", []) or []:
                sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address", "")
                latest = message.get("receivedDateTime") or latest
                if str(sender).lower() == self.email:
                    continue
                ids.append(message["id"])
            url, params = data.get("@odata.nextLink"), None
        return ChangeBatch(message_ids=ids, next_cursor=latest)

    async def fetch_changes(self, cursor: str | None) -> ChangeBatch:
        if not cursor:
            return ChangeBatch(message_ids=[], next_cursor=_iso(datetime.now(timezone.utc)))
        return await self._window(cursor)

    async def resync(self, since: datetime) -> ChangeBatch:
        return await self._window(_iso(since))

    async def get_message(self, provider_message_id: str) -> InboundMessage:
        meta = await self._get(f"/me/messages/{provider_message_id}",
                               {"$select": "id,conversationId,receivedDateTime,from"})
        raw = await transport.request("GET", f"{GRAPH}/me/messages/{provider_message_id}/$value",
                                      token=self._token, headers=IMMUTABLE)
        sender = ((meta.get("from") or {}).get("emailAddress") or {}).get("address", "")
        received = datetime.fromisoformat(
            str(meta.get("receivedDateTime", "1970-01-01T00:00:00Z")).replace("Z", "+00:00")
        )
        return InboundMessage(
            provider_message_id=meta["id"], provider_thread_id=meta.get("conversationId", ""),
            raw=raw.content, received_at=received, outgoing=str(sender).lower() == self.email,
        )

    async def _sent_window(self, start: datetime, end: datetime, select: str) -> list[dict]:
        data = await self._get(
            f"{GRAPH}/me/mailFolders/sentitems/messages",
            {"$filter": f"sentDateTime ge {_iso(start)} and sentDateTime le {_iso(end)}",
             "$select": select, "$top": "50"},
        )
        return data.get("value", []) or []

    @staticmethod
    def _addressed_to(message: dict, to: str) -> bool:
        wanted = (to or "").lower()
        return any(
            ((r.get("emailAddress") or {}).get("address", "")).lower() == wanted
            for r in message.get("toRecipients") or []
        )

    async def find_sent(self, *, ref_header: str, to: str, around: datetime) -> SentRef | None:
        select = "id,conversationId,internetMessageId,internetMessageHeaders,toRecipients"
        for message in await self._sent_window(around - timedelta(days=2),
                                               around + timedelta(days=1), select):
            if not self._addressed_to(message, to):
                continue
            for header in message.get("internetMessageHeaders") or []:
                if (str(header.get("name", "")).lower() == "x-nexus-ref"
                        and str(header.get("value", "")).strip() == ref_header):
                    return SentRef(message["id"], message.get("conversationId", ""),
                                   message.get("internetMessageId", ""))
        return None

    async def search_sent(self, *, to: str, subject: str, around: datetime) -> SentRef | None:
        wanted = normalize_subject(subject).lower()
        select = "id,conversationId,internetMessageId,subject,toRecipients,sentDateTime"
        best: tuple[float, SentRef] | None = None
        for message in await self._sent_window(around - timedelta(days=3),
                                               around + timedelta(days=3), select):
            if not self._addressed_to(message, to):
                continue
            if normalize_subject(message.get("subject", "")).lower() != wanted:
                continue
            sent_at = datetime.fromisoformat(str(message["sentDateTime"]).replace("Z", "+00:00"))
            distance = abs((sent_at - around).total_seconds())
            ref = SentRef(message["id"], message.get("conversationId", ""),
                          message.get("internetMessageId", ""))
            if best is None or distance < best[0]:
                best = (distance, ref)
        return best[1] if best else None
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_mailboxes.py -n0 -q`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/mailboxes/graph.py tests/test_engagement_mailboxes.py
git commit -m "feat(engagement): GraphProvider - MIME import, conversation index, changes window"
```

---

### Task 6: Registry and connection service

**Files:**
- Create: `nexus/engagement/mailboxes/registry.py`, `nexus/engagement/mailboxes/service.py`
- Test: `tests/test_engagement_mailboxes.py`

- [ ] **Step 1: Write the failing tests**

Add `_owner`, `_bundle`, `test_a_connection_is_sealed_and_a_reconnect_updates_the_same_row`, `test_a_colleagues_mailbox_cannot_be_taken_over` and `test_edits_are_validated_and_a_disconnect_deletes_the_tokens` from the final file (Task 8 Step 1).

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_mailboxes.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.mailboxes.service'`

- [ ] **Step 3: Implement**

`nexus/engagement/mailboxes/registry.py`:

```python
"""The one place a connected mailbox becomes a provider object."""
from __future__ import annotations

from nexus.engagement.mailboxes.provider import MailProvider


def make_provider(provider: str, *, access_token: str, email: str = "") -> MailProvider:
    if provider == "google":
        from nexus.engagement.mailboxes.gmail import GmailProvider

        return GmailProvider(access_token=access_token, email=email)
    if provider == "microsoft":
        from nexus.engagement.mailboxes.graph import GraphProvider

        return GraphProvider(access_token=access_token, email=email)
    raise ValueError(f"unknown mailbox provider {provider!r}")


def provider_for(connection, access_token: str) -> MailProvider:
    return make_provider(connection.provider, access_token=access_token, email=connection.email)


async def open_provider(ts, connection) -> MailProvider:
    """A provider with a fresh access token. Raises ``AuthExpired`` (and marks the connection
    ``needs_reauth``) when the grant is gone."""
    from nexus.engagement.mailboxes.tokens import fresh_access_token

    return provider_for(connection, await fresh_access_token(ts, connection))
```

`nexus/engagement/mailboxes/service.py`:

```python
"""Connected mailboxes: connect, list, edit, check, disconnect (spec §9 Settings → Mailboxes).

**A mailbox belongs to the SDR who connected it.** Replies go back to whoever sent the email, and
reading a mailbox is reading a person's mail, so an address already connected by a colleague cannot
be taken over by connecting it again; the colleague disconnects first. The same person reconnecting
(after ``needs_reauth``, or to change timezone) updates their row in place.

Statuses: ``connected``; ``needs_reauth`` (the grant was revoked or expired — the SDR reconnects);
``error`` (the APP is misconfigured, which reconnecting cannot fix); ``revoked`` (disconnected, tokens
deleted, row kept so history still names the mailbox).
"""
from __future__ import annotations

from datetime import datetime

from nexus.core.audit import record_audit
from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.engagement.mailboxes import tokens
from nexus.engagement.timekeeping import zone_or_none
from nexus.models.engagement import MailboxConnection

_UNSET = object()


class MailboxOwnedByColleague(ValueError):
    """The address is connected by someone else in this workspace."""


async def upsert_connection(
    ts: TenantSession, *, owner_user_id: str, provider: str, email: str, display_name: str,
    bundle: dict, scopes: list[str], timezone: str,
) -> MailboxConnection:
    address = (email or "").strip().lower()
    existing = await ts.first(MailboxConnection, MailboxConnection.email == address)
    zone = timezone if zone_or_none(timezone) else "UTC"
    if existing is not None and existing.owner_user_id != owner_user_id \
            and existing.status != "revoked":
        raise MailboxOwnedByColleague(address)
    if existing is None:
        existing = MailboxConnection(owner_user_id=owner_user_id, provider=provider, email=address,
                                     timezone=zone)
        ts.add(existing)
        action = "mailbox.connect"
    else:
        existing.owner_user_id = owner_user_id
        existing.provider = provider
        action = "mailbox.reconnect"
    existing.display_name = (display_name or "")[:200]
    existing.tokens = tokens.seal(bundle)
    existing.scopes = list(scopes)
    existing.status = "connected"
    existing.last_error = None
    if not existing.timezone or existing.timezone == "UTC":
        existing.timezone = zone
    await ts.flush()
    await record_audit(ts, action, actor_user_id=owner_user_id, target_type="mailbox",
                       target_id=existing.id, meta={"provider": provider})
    return existing


async def list_mailboxes(ts: TenantSession, *, user_id: str, team: bool) -> list[MailboxConnection]:
    where = [] if team else [MailboxConnection.owner_user_id == user_id]
    stmt = ts.select(MailboxConnection, *where).order_by(MailboxConnection.created_at)
    return list((await ts.session.scalars(stmt)).all())


async def update_mailbox(
    ts: TenantSession, connection: MailboxConnection, *, timezone=_UNSET, signature=_UNSET,
    reply_confidence=_UNSET,
) -> MailboxConnection:
    """Edit the SDR's own settings. Raises ``ValueError`` naming the problem; writes nothing then."""
    from nexus.engagement.settings import read_settings, validate_mailbox_confidence
    from nexus.models.identity import Tenant

    if timezone is not _UNSET:
        if zone_or_none(timezone) is None:
            raise ValueError(f"{timezone!r} is not a timezone name like Europe/London")
    if reply_confidence is not _UNSET:
        tenant = await ts.session.get(Tenant, ts.tenant_id)
        reply_confidence = validate_mailbox_confidence(
            read_settings(getattr(tenant, "email_settings", None)), reply_confidence
        )
    if timezone is not _UNSET:
        connection.timezone = timezone
    if signature is not _UNSET:
        connection.signature = (signature or "")[:4000]
    if reply_confidence is not _UNSET:
        connection.reply_confidence = reply_confidence
    await ts.flush()
    return connection


async def disconnect(ts: TenantSession, connection: MailboxConnection, *, actor_user_id: str) -> None:
    """Revoke at the provider (best effort), delete the tokens, keep the row as history."""
    from nexus.engagement.config import oauth_app
    from nexus.engagement.mailboxes import oauth

    bundle = tokens.unseal(connection.tokens)
    try:
        await oauth.revoke(await oauth_app(connection.provider), bundle)
    except Exception:  # a failed revoke must not keep the tokens: deleting them is the disconnect
        pass
    connection.tokens = {}
    connection.status = "revoked"
    connection.sync_cursor = None
    connection.notifications_expire_at = None
    connection.notification_subscription_id = None
    connection.last_error = None
    await ts.flush()
    await record_audit(ts, "mailbox.disconnect", actor_user_id=actor_user_id,
                       target_type="mailbox", target_id=connection.id,
                       meta={"provider": connection.provider,
                             "by_owner": actor_user_id == connection.owner_user_id})


async def check_connection(ts: TenantSession, connection: MailboxConnection):
    """Refresh the token and read the profile: proves the mailbox can be used right now."""
    from nexus.engagement.mailboxes.provider import ProviderError
    from nexus.engagement.mailboxes.registry import provider_for

    token = await tokens.fresh_access_token(ts, connection, force=True)
    profile = await provider_for(connection, token).profile()
    if profile.email and profile.email != connection.email:
        connection.status = "error"
        connection.last_error = (
            f"This connection now signs in as {profile.email}, not {connection.email}. "
            "Disconnect it and connect the right mailbox."
        )
        await ts.flush()
        raise ProviderError(connection.last_error)
    return profile


def sending_state(connection: MailboxConnection, *, now: datetime | None = None) -> tuple[bool, str]:
    """Whether this mailbox may send right now, and why not (pre-send check 4, spec §5)."""
    from nexus.core.db import ensure_aware

    now = now or utcnow()
    if connection.status != "connected":
        return False, f"mailbox {connection.status}"
    paused = ensure_aware(connection.paused_until)
    if paused is not None and paused > now:
        return False, "provider limit"
    return True, ""
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_mailboxes.py -n0 -q`
Expected: `11 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/mailboxes/registry.py nexus/engagement/mailboxes/service.py tests/test_engagement_mailboxes.py
git commit -m "feat(engagement): mailbox connection service - one owner per address, sealed tokens"
```

---

### Task 7: My mailboxes API and the OAuth callback

**Files:**
- Create: `nexus/api/routers/engagement_mailboxes.py`
- Modify: `nexus/api/routers/__init__.py`
- Test: `tests/test_engagement_mailboxes.py`

- [ ] **Step 1: Write the failing tests**

Add `test_connecting_an_unconfigured_provider_says_what_is_missing`, `test_a_configured_provider_returns_an_authorize_url`, `test_a_callback_with_a_bad_state_redirects_with_an_error` and `test_reps_see_their_own_mailboxes_and_only_managers_the_team` from the final file (Task 8 Step 1).

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_mailboxes.py -n0 -q`
Expected: FAIL — 404 on `/api/engagement/mailboxes/providers`.

- [ ] **Step 3: Implement the router**

`nexus/api/routers/engagement_mailboxes.py`:

```python
# nexus/api/routers/engagement_mailboxes.py
"""SDR mailboxes connected by OAuth: My mailboxes (spec §9, D1, D2).

Every member can connect their own mailbox (``run_engagement``); managers can list the team's and
disconnect one (``manage_engagement``), for the day someone leaves. Only the owner edits a mailbox's
timezone, signature and confidence bar: those describe the person, not the team.

The OAuth callback carries no bearer token — the browser arrives from Google or Microsoft — so the
signed ``state`` is the credential, exactly as in ``routers/network.py``. Every outcome is a redirect
back to ``/mailboxes`` with ``connected=`` or ``error=``; nothing on that path renders a stack trace.

No token ever appears in a response model.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission, has_permission
from nexus.core.tenancy import TenantSession
from nexus.models.engagement import MailboxConnection

logger = logging.getLogger("nexus.api.engagement_mailboxes")

router = APIRouter(prefix="/engagement/mailboxes", tags=["engagement"])

PROVIDERS = ("google", "microsoft")


class MailboxOut(BaseModel):
    id: str
    provider: str
    email: str
    display_name: str
    owner_user_id: str
    mine: bool
    status: str
    last_error: str | None
    timezone: str
    signature: str
    reply_confidence: float | None
    effective_reply_confidence: float
    reply_confidence_min: float
    reply_confidence_max: float
    paused_until: datetime | None
    last_synced_at: datetime | None
    created_at: datetime


class ProviderStateOut(BaseModel):
    provider: str
    configured: bool


class StartIn(BaseModel):
    model_config = {"extra": "forbid"}

    timezone: str = Field(default="UTC", max_length=64)


class StartOut(BaseModel):
    authorize_url: str


class MailboxPatch(BaseModel):
    model_config = {"extra": "forbid"}

    timezone: str | None = Field(default=None, max_length=64)
    signature: str | None = Field(default=None, max_length=4000)
    reply_confidence: float | None = None
    #: Explicitly clear the SDR's own bar and fall back to the workspace default.
    clear_reply_confidence: bool = False


async def _settings(ts: TenantSession):
    from nexus.engagement.settings import read_settings
    from nexus.models.identity import Tenant

    tenant = await ts.session.get(Tenant, ts.tenant_id)
    return read_settings(getattr(tenant, "email_settings", None))


async def _out(ts: TenantSession, row: MailboxConnection, principal: Principal) -> MailboxOut:
    from nexus.engagement.settings import effective_confidence

    settings = await _settings(ts)
    return MailboxOut(
        id=row.id, provider=row.provider, email=row.email, display_name=row.display_name or "",
        owner_user_id=row.owner_user_id, mine=row.owner_user_id == principal.user_id,
        status=row.status, last_error=row.last_error, timezone=row.timezone or "UTC",
        signature=row.signature or "", reply_confidence=row.reply_confidence,
        effective_reply_confidence=effective_confidence(settings, row.reply_confidence),
        reply_confidence_min=settings.reply_confidence_min,
        reply_confidence_max=settings.reply_confidence_max,
        paused_until=row.paused_until, last_synced_at=row.last_synced_at,
        created_at=row.created_at,
    )


async def _row(ts: TenantSession, mailbox_id: str) -> MailboxConnection:
    row = await ts.get(MailboxConnection, mailbox_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found")
    return row


def _is_manager(principal: Principal) -> bool:
    from nexus.core.rbac import Role

    return has_permission(Role(principal.role), Permission.manage_engagement)


@router.get("", response_model=list[MailboxOut])
async def list_mailboxes(
    team: bool = False,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> list[MailboxOut]:
    from nexus.engagement.mailboxes.service import list_mailboxes as _list

    if team and not _is_manager(principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only managers can see the team's mailboxes")
    rows = await _list(ts, user_id=principal.user_id, team=team)
    return [await _out(ts, r, principal) for r in rows]


@router.get("/providers", response_model=list[ProviderStateOut])
async def provider_states(
    _: Principal = Depends(require(Permission.run_engagement)),
) -> list[ProviderStateOut]:
    from nexus.engagement.config import oauth_app

    return [ProviderStateOut(provider=p, configured=(await oauth_app(p)).configured)
            for p in PROVIDERS]


@router.post("/oauth/{provider}/start", response_model=StartOut)
async def start_connect(
    provider: str,
    body: StartIn,
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> StartOut:
    from nexus.engagement.config import oauth_app
    from nexus.engagement.mailboxes import oauth

    if provider not in PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown mailbox provider")
    app = await oauth_app(provider)
    if not app.configured:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"message": f"{provider.title()} mailboxes are not set up for this deployment yet. "
                        "Ask your administrator.", "missing": list(app.missing)},
        )
    url = oauth.start(app, user_id=principal.user_id, tenant_id=principal.tenant_id,
                      timezone=body.timezone)
    return StartOut(authorize_url=url)


@router.get("/oauth/{provider}/callback", include_in_schema=False)
async def oauth_callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    from sqlalchemy import select

    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import apply_rls
    from nexus.engagement.config import oauth_app, public_base_url
    from nexus.engagement.mailboxes import oauth
    from nexus.engagement.mailboxes.provider import ProviderError
    from nexus.engagement.mailboxes.registry import make_provider
    from nexus.engagement.mailboxes.service import MailboxOwnedByColleague, upsert_connection
    from nexus.models.identity import Membership

    def _to(query: str) -> RedirectResponse:
        return RedirectResponse(f"{public_base_url()}/mailboxes?{query}", status_code=302)

    if provider not in PROVIDERS:
        return _to("error=unknown_provider")
    if error or not code or not state:
        return _to(f"error={'denied' if error == 'access_denied' else 'oauth_failed'}")
    claims = oauth.verify_state(state, provider=provider)
    if claims is None:
        return _to("error=bad_state")
    app = await oauth_app(provider)
    if not app.configured:
        return _to("error=not_configured")
    try:
        data = await oauth.exchange_code(app, code=code, verifier=claims["pkce"])
    except ProviderError:
        logger.warning("mailbox OAuth code exchange failed for %s", provider, exc_info=True)
        return _to("error=exchange_failed")
    if oauth.missing_scopes(app, data.get("scope", "")):
        return _to("error=missing_scopes")
    bundle = oauth.bundle_from_response(data)
    try:
        profile = await make_provider(provider, access_token=bundle["access_token"]).profile()
    except ProviderError:
        return _to("error=profile_failed")
    if not profile.email:
        return _to("error=profile_failed")

    tenant_id, user_id = claims["tid"], claims["uid"]
    async with get_sessionmaker()() as session:
        await apply_rls(session, tenant_id)
        member = (await session.scalars(select(Membership).where(
            Membership.tenant_id == tenant_id, Membership.user_id == user_id,
        ))).first()
        if member is None:
            return _to("error=not_a_member")
        ts = TenantSession(session, tenant_id)
        try:
            await upsert_connection(
                ts, owner_user_id=user_id, provider=provider, email=profile.email,
                display_name=profile.display_name, bundle=bundle,
                scopes=(data.get("scope") or "").split(), timezone=claims.get("tz", "UTC"),
            )
            await session.commit()
        except MailboxOwnedByColleague:
            await session.rollback()
            return _to("error=owned_by_colleague")
    return _to(f"connected={provider}")


@router.patch("/{mailbox_id}", response_model=MailboxOut)
async def update_mailbox(
    mailbox_id: str,
    body: MailboxPatch,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> MailboxOut:
    from nexus.engagement.mailboxes import service

    row = await _row(ts, mailbox_id)
    if row.owner_user_id != principal.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the mailbox's owner can change it")
    changes: dict = {}
    if body.timezone is not None:
        changes["timezone"] = body.timezone
    if body.signature is not None:
        changes["signature"] = body.signature
    if body.clear_reply_confidence:
        changes["reply_confidence"] = None
    elif body.reply_confidence is not None:
        changes["reply_confidence"] = body.reply_confidence
    try:
        await service.update_mailbox(ts, row, **changes)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return await _out(ts, row, principal)


@router.post("/{mailbox_id}/check", response_model=MailboxOut)
async def check_mailbox(
    mailbox_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> MailboxOut:
    from nexus.engagement.mailboxes import service
    from nexus.engagement.mailboxes.provider import ProviderError

    row = await _row(ts, mailbox_id)
    if row.owner_user_id != principal.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the mailbox's owner can check it")
    try:
        await service.check_connection(ts, row)
    except ProviderError:
        pass  # the row now carries the status and the provider's words; the screen shows them
    return await _out(ts, row, principal)


@router.delete("/{mailbox_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_mailbox(
    mailbox_id: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> Response:
    from nexus.engagement.mailboxes import service

    row = await _row(ts, mailbox_id)
    if row.owner_user_id != principal.user_id and not _is_manager(principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Only the owner or a manager can disconnect a mailbox")
    await service.disconnect(ts, row, actor_user_id=principal.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

In `nexus/api/routers/__init__.py`, add `engagement_mailboxes,` to the import list after `custom_fields,` and `engagement_mailboxes.router,` to `all_routers` after `admin_engagement.router,`.

- [ ] **Step 4: Run to see them pass, and the RLS guard**

Run: `pytest tests/test_engagement_mailboxes.py tests/test_rls_binding_guard.py -n0 -q`
Expected: all pass. The callback builds a `TenantSession` only after `apply_rls`, which the guard checks.

- [ ] **Step 5: Commit**

```bash
git add nexus/api/routers/engagement_mailboxes.py nexus/api/routers/__init__.py tests/test_engagement_mailboxes.py
git commit -m "feat(engagement): My mailboxes API and OAuth callback"
```

---

### Task 8: Daily token refresh job

**Files:**
- Modify: `nexus/workers/tasks.py`, `nexus/workers/scheduler.py`, `tests/test_continuous_automation.py`
- Test: `tests/test_engagement_mailboxes.py` (final version)

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_engagement_mailboxes.py` with the final version:

```python
"""Connecting SDR mailboxes: OAuth state, scopes, token lifetimes, error mapping, ownership, API.

Offline: nothing here calls Google or Microsoft. The adapters themselves are exercised against real
mailboxes by `tests_live/engagement/test_mailboxes_live.py` (D21).
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, principal_from_token, signup, tenant_session

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)


def _app(provider: str = "google"):
    from nexus.engagement.config import GOOGLE_SCOPES, MICROSOFT_SCOPES, OAuthApp

    return OAuthApp(
        provider=provider, client_id="client-id", client_secret="client-secret",
        tenant="common" if provider == "microsoft" else "",
        redirect_uri=f"https://app.example.com/api/engagement/mailboxes/oauth/{provider}/callback",
        scopes=GOOGLE_SCOPES if provider == "google" else MICROSOFT_SCOPES, missing=(),
    )


# ---- OAuth -----------------------------------------------------------------------------------------

def test_the_state_round_trips_and_refuses_another_provider_or_a_network_state():
    from nexus.engagement.mailboxes import oauth
    from nexus.network.oauth import sign_state as network_state

    token = oauth.sign_state(user_id="u1", tenant_id="t1", provider="google", verifier="v",
                             timezone="Europe/London")
    claims = oauth.verify_state(token, provider="google")
    assert (claims["uid"], claims["tid"], claims["pkce"], claims["tz"]) == (
        "u1", "t1", "v", "Europe/London")
    assert oauth.verify_state(token, provider="microsoft") is None
    assert oauth.verify_state(token + "x", provider="google") is None
    foreign = network_state(member_id="u1", tenant_id="t1", provider="google", verifier="v")
    assert oauth.verify_state(foreign, provider="google") is None


def test_authorize_urls_ask_for_offline_access_and_pkce():
    from nexus.engagement.mailboxes import oauth

    google = urlparse(oauth.authorize_url(_app("google"), state="s", challenge="c"))
    q = parse_qs(google.query)
    assert google.netloc == "accounts.google.com"
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["code_challenge_method"] == ["S256"] and q["state"] == ["s"]
    assert "https://www.googleapis.com/auth/gmail.compose" in q["scope"][0].split()

    microsoft = urlparse(oauth.authorize_url(_app("microsoft"), state="s", challenge="c"))
    assert microsoft.path == "/common/oauth2/v2.0/authorize"
    assert "offline_access" in parse_qs(microsoft.query)["scope"][0].split()


def test_a_grant_missing_mail_access_is_detected():
    from nexus.engagement.mailboxes import oauth

    full_google = ("openid https://www.googleapis.com/auth/userinfo.email "
                   "https://www.googleapis.com/auth/gmail.readonly "
                   "https://www.googleapis.com/auth/gmail.compose")
    assert oauth.missing_scopes(_app("google"), full_google) == []
    assert oauth.missing_scopes(_app("google"), "openid email") == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    ]
    # Microsoft reports Graph scopes without the resource prefix.
    granted = "Mail.ReadWrite Mail.Send User.Read openid profile email offline_access"
    assert oauth.missing_scopes(_app("microsoft"), granted) == []


def test_a_refresh_keeps_the_old_refresh_token_when_the_provider_omits_one():
    from nexus.engagement.mailboxes import oauth

    first = oauth.bundle_from_response({"access_token": "a1", "refresh_token": "r1",
                                        "expires_in": 3599})
    google_refresh = oauth.bundle_from_response({"access_token": "a2", "expires_in": 3599},
                                                previous=first)
    assert google_refresh["refresh_token"] == "r1"
    microsoft_refresh = oauth.bundle_from_response(
        {"access_token": "a3", "refresh_token": "r2", "expires_in": 3599}, previous=first)
    assert microsoft_refresh["refresh_token"] == "r2"


def test_token_lifetimes():
    from nexus.engagement.mailboxes import tokens

    now = 1_800_000_000
    assert tokens.needs_refresh({}, now=now)
    assert tokens.needs_refresh({"access_token": "a", "expires_at": now + 60}, now=now)
    assert not tokens.needs_refresh({"access_token": "a", "expires_at": now + 3000}, now=now)
    assert tokens.liveness_due({"refreshed_at": now - 25 * 3600}, now=now)
    assert not tokens.liveness_due({"refreshed_at": now - 3600}, now=now)


# ---- provider error mapping ------------------------------------------------------------------------

def test_errors_map_to_what_the_caller_must_do():
    from nexus.engagement.mailboxes.provider import (
        AuthExpired,
        NotFound,
        ProviderError,
        ProviderLimit,
        TransientError,
    )
    from nexus.engagement.mailboxes.transport import classify_error

    assert isinstance(classify_error(401, {}, "", now=NOW), AuthExpired)
    assert isinstance(classify_error(404, {}, "", now=NOW), NotFound)
    assert isinstance(classify_error(503, {}, "", now=NOW), TransientError)

    retry = classify_error(429, {"Retry-After": "120"}, "", now=NOW)
    assert isinstance(retry, ProviderLimit) and retry.retry_at == NOW + timedelta(seconds=120)

    stamped = classify_error(
        403, {}, "User-rate limit exceeded.  Retry after 2026-09-17T11:30:00.000Z", now=NOW)
    assert stamped.retry_at == datetime(2026, 9, 17, 11, 30, tzinfo=timezone.utc)

    daily = classify_error(403, {}, "Daily user sending quota exceeded.", now=NOW)
    assert isinstance(daily, ProviderLimit) and daily.retry_at == NOW + timedelta(hours=24)

    permission = classify_error(403, {}, "Request had insufficient authentication scopes.", now=NOW)
    assert type(permission) is ProviderError


# ---- Outlook conversation index ----------------------------------------------------------------------

def test_a_follow_up_extends_the_parents_conversation_index():
    from nexus.engagement.mailboxes.graph import child_thread_index, new_thread_index

    parent = new_thread_index(NOW)
    raw_parent = base64.b64decode(parent)
    assert len(raw_parent) == 22 and raw_parent[0] == 0x01
    child = base64.b64decode(child_thread_index(parent, NOW + timedelta(hours=26)))
    assert len(child) == 27 and child[:22] == raw_parent
    grandchild = base64.b64decode(child_thread_index(base64.b64encode(child).decode(),
                                                     NOW + timedelta(days=5)))
    assert len(grandchild) == 32 and grandchild[:27] == child
    assert len(base64.b64decode(child_thread_index("not base64!", NOW))) == 22


def test_a_header_is_set_without_touching_the_body():
    from email import message_from_bytes
    from email.message import EmailMessage

    from nexus.engagement.mailboxes.graph import with_header

    original = EmailMessage()
    original["Subject"] = "Re: pricing"
    original["Thread-Index"] = "old"
    original.set_content("Hi Jane,\n\nFollowing up on pricing.\n\nBest,\nSam\n")
    updated = message_from_bytes(with_header(original.as_bytes(), "Thread-Index", "new"))
    assert updated.get_all("Thread-Index") == ["new"]
    assert "Following up on pricing." in updated.get_payload(decode=True).decode()


# ---- service ---------------------------------------------------------------------------------------

async def _owner(client, slug: str):
    token = await signup(client, slug=slug, email=f"sdr@{slug}co.com", company=slug.title())
    return token, principal_from_token(token)


def _bundle():
    return {"access_token": "at-plaintext", "refresh_token": "rt-plaintext",
            "expires_at": 1_900_000_000, "refreshed_at": 1_800_000_000}


async def test_a_connection_is_sealed_and_a_reconnect_updates_the_same_row(client):
    from nexus.engagement.mailboxes.service import upsert_connection
    from nexus.engagement.mailboxes.tokens import unseal

    _token, me = await _owner(client, "seal")
    async with tenant_session(me.tenant_id) as ts:
        first = await upsert_connection(
            ts, owner_user_id=me.user_id, provider="google", email="SDR@SealCo.com",
            display_name="Sam", bundle=_bundle(), scopes=["gmail.readonly"],
            timezone="Europe/London")
        assert first.email == "sdr@sealco.com" and first.timezone == "Europe/London"
        assert "rt-plaintext" not in str(first.tokens) and "enc" in first.tokens
        assert unseal(first.tokens)["refresh_token"] == "rt-plaintext"
        first.status = "needs_reauth"
        await ts.flush()
        again = await upsert_connection(
            ts, owner_user_id=me.user_id, provider="google", email="sdr@sealco.com",
            display_name="Sam", bundle={**_bundle(), "refresh_token": "rt-new"}, scopes=[],
            timezone="America/New_York")
        assert again.id == first.id and again.status == "connected"
        assert again.timezone == "Europe/London"  # a chosen timezone is not overwritten
        assert unseal(again.tokens)["refresh_token"] == "rt-new"


async def test_a_colleagues_mailbox_cannot_be_taken_over(client):
    from nexus.engagement.mailboxes.service import MailboxOwnedByColleague, upsert_connection

    _token, me = await _owner(client, "own")
    async with tenant_session(me.tenant_id) as ts:
        await upsert_connection(ts, owner_user_id=me.user_id, provider="google",
                                email="shared@ownco.com", display_name="", bundle=_bundle(),
                                scopes=[], timezone="UTC")
    with pytest.raises(MailboxOwnedByColleague):
        async with tenant_session(me.tenant_id) as ts:
            await upsert_connection(ts, owner_user_id="someone-else", provider="google",
                                    email="shared@ownco.com", display_name="", bundle=_bundle(),
                                    scopes=[], timezone="UTC")


async def test_edits_are_validated_and_a_disconnect_deletes_the_tokens(client):
    from nexus.engagement.mailboxes import service

    _token, me = await _owner(client, "edit")
    async with tenant_session(me.tenant_id) as ts:
        row = await service.upsert_connection(
            ts, owner_user_id=me.user_id, provider="microsoft", email="sdr@editco.com",
            display_name="", bundle=_bundle(), scopes=[], timezone="UTC")
        with pytest.raises(ValueError, match="not a timezone"):
            await service.update_mailbox(ts, row, timezone="Mars/Olympus")
        with pytest.raises(ValueError, match="between 0.50 and 0.99"):
            await service.update_mailbox(ts, row, reply_confidence=0.3)
        await service.update_mailbox(ts, row, timezone="Asia/Kolkata", reply_confidence=0.9,
                                     signature="Sam\nSDR")
        assert (row.timezone, row.reply_confidence) == ("Asia/Kolkata", 0.9)
        await service.disconnect(ts, row, actor_user_id=me.user_id)
        assert row.status == "revoked" and row.tokens == {}
        assert service.sending_state(row)[0] is False


# ---- API -------------------------------------------------------------------------------------------

async def test_connecting_an_unconfigured_provider_says_what_is_missing(client):
    token, _me = await _owner(client, "noconf")
    providers = await client.get("/api/engagement/mailboxes/providers", headers=auth(token))
    assert providers.json() == [{"provider": "google", "configured": False},
                                {"provider": "microsoft", "configured": False}]
    r = await client.post("/api/engagement/mailboxes/oauth/google/start",
                          json={"timezone": "UTC"}, headers=auth(token))
    assert r.status_code == 409
    assert any("client id" in m for m in r.json()["detail"]["missing"])


async def test_a_configured_provider_returns_an_authorize_url(client, monkeypatch):
    from nexus.providers import resolver
    from nexus.providers.service import add_key

    monkeypatch.setattr(get_settings(), "engagement_public_base_url", "https://app.example.com")
    monkeypatch.setattr(get_settings(), "engagement_google_client_id",
                        "123456789012-abc123.apps.googleusercontent.com")
    await add_key("google_oauth", "", "google-client-secret-value")
    resolver.invalidate()
    token, _me = await _owner(client, "conf")
    r = await client.post("/api/engagement/mailboxes/oauth/google/start",
                          json={"timezone": "Europe/Paris"}, headers=auth(token))
    assert r.status_code == 200, r.text
    query = parse_qs(urlparse(r.json()["authorize_url"]).query)
    assert query["redirect_uri"] == [
        "https://app.example.com/api/engagement/mailboxes/oauth/google/callback"]
    assert query["state"] and query["code_challenge"]
    resolver.invalidate()


async def test_a_callback_with_a_bad_state_redirects_with_an_error(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_public_base_url", "https://app.example.com")
    r = await client.get("/api/engagement/mailboxes/oauth/google/callback",
                         params={"code": "c", "state": "forged"})
    assert r.status_code == 302
    assert r.headers["location"] == "https://app.example.com/mailboxes?error=bad_state"


async def test_reps_see_their_own_mailboxes_and_only_managers_the_team(client):
    from nexus.engagement.mailboxes.service import upsert_connection

    token, me = await _owner(client, "list")
    async with tenant_session(me.tenant_id) as ts:
        await upsert_connection(ts, owner_user_id=me.user_id, provider="google",
                                email="sdr@listco.com", display_name="", bundle=_bundle(),
                                scopes=[], timezone="UTC")
    rows = (await client.get("/api/engagement/mailboxes", headers=auth(token))).json()
    assert [r["email"] for r in rows] == ["sdr@listco.com"] and rows[0]["mine"] is True
    assert "tokens" not in rows[0] and "at-plaintext" not in str(rows)

    from nexus.core.security import create_access_token

    rep = create_access_token(user_id="rep-user", tenant_id=me.tenant_id, role="rep")
    team = await client.get("/api/engagement/mailboxes", params={"team": True},
                            headers=auth(rep))
    assert team.status_code == 403


async def test_the_refresh_job_is_registered_and_idle_without_mailboxes():
    from nexus.workers.tasks import HANDLERS, handle_refresh_mailbox_tokens

    assert "refresh_mailbox_tokens" in HANDLERS
    assert await handle_refresh_mailbox_tokens({}) == {
        "refreshed": 0, "needs_reauth": 0, "failed": 0}


def test_the_mailboxes_page_is_reachable_by_every_member():
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"
    app = (src / "App.tsx").read_text(encoding="utf-8")
    nav = (src / "app" / "nav.tsx").read_text(encoding="utf-8")
    page = (src / "pages" / "engagement" / "MailboxesPage.tsx").read_text(encoding="utf-8")
    assert 'path="/mailboxes"' in app and 'capability="module.outreach" name="My mailboxes"' in app
    assert '{ to: "/mailboxes", label: "My mailboxes"' in nav
    assert "api.startMailboxConnect" in page and "browserTimezone()" in page
    assert "window.location.assign(authorize_url)" in page
    for code in ("owned_by_colleague", "missing_scopes", "bad_state", "denied"):
        assert code in page, f"the page does not explain {code}"
```

In `tests/test_continuous_automation.py`:
- `test_enqueue_due_enqueues_both_drivers_when_enabled`: `assert count == 12` → `assert count == 13` (add the comment `# +1 for refresh_mailbox_tokens, which keeps SDR mailbox status honest whether or not automation is on.`) and add `"refresh_mailbox_tokens",` to the expected set.
- `test_enqueue_due_noop_when_disabled`: `assert count == 8` → `assert count == 9` and add `"refresh_mailbox_tokens",` to the expected set.

In `tests/test_crm_auto_sync.py`, both `test_scheduler_enqueues_crm_sweep_when_crm_sync_enabled` and
`test_scheduler_omits_crm_sweep_when_disabled` assert the WHOLE set of enqueued job names, so each
needs `"refresh_mailbox_tokens",` added to its expected set, with the comment:

```python
        # The mailbox refresh rides along too: a revoked grant must show Reconnect whether or
        # not this workspace switched automation on.
```

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_mailboxes.py tests/test_continuous_automation.py tests/test_crm_auto_sync.py -n0 -q`
Expected: FAIL — `ImportError: cannot import name 'handle_refresh_mailbox_tokens'` and `12 == 13`.

- [ ] **Step 3: Implement the job**

In `nexus/workers/tasks.py`, immediately before `async def enqueue_crawl_companies(`, add:

```python
async def handle_refresh_mailbox_tokens(payload: dict) -> dict:
    """Refresh each connected SDR mailbox at least once a day (spec §9 mailbox status).

    A revoked grant is otherwise discovered at the moment a campaign tries to send. Refreshing
    marks it ``needs_reauth`` a day earlier, where the SDR sees Reconnect. Microsoft refresh tokens
    also lapse after 90 days unused; a daily refresh keeps an idle mailbox connected.

    The scan reads only ids across tenants (the worker connects as the owner role); each refresh
    runs inside that tenant's own session."""
    from collections import defaultdict

    from sqlalchemy import select

    from nexus.engagement.mailboxes.provider import AuthExpired, ProviderError
    from nexus.engagement.mailboxes.tokens import fresh_access_token, liveness_due, unseal
    from nexus.models.engagement import MailboxConnection

    async with get_sessionmaker()() as session:
        rows = (await session.execute(
            select(MailboxConnection.tenant_id, MailboxConnection.id)
            .where(MailboxConnection.status == "connected")
        )).all()
    by_tenant: dict[str, list[str]] = defaultdict(list)
    for tenant_id, mailbox_id in rows:
        by_tenant[tenant_id].append(mailbox_id)

    refreshed = needs_reauth = failed = 0
    for tenant_id, mailbox_ids in by_tenant.items():
        async with tenant_session(tenant_id) as ts:
            for mailbox_id in mailbox_ids:
                connection = await ts.get(MailboxConnection, mailbox_id)
                if connection is None or connection.status != "connected":
                    continue
                if not liveness_due(unseal(connection.tokens)):
                    continue
                try:
                    await fresh_access_token(ts, connection, force=True)
                    refreshed += 1
                except AuthExpired:
                    needs_reauth += 1
                except ProviderError:
                    failed += 1
    return {"refreshed": refreshed, "needs_reauth": needs_reauth, "failed": failed}


async def enqueue_refresh_mailbox_tokens(*, queue: TaskQueue | None = None) -> None:
    queue = queue or get_task_queue()
    await queue.enqueue(Job(name="refresh_mailbox_tokens", payload={}))
```

Add `"refresh_mailbox_tokens": handle_refresh_mailbox_tokens,` as the last entry of `HANDLERS`.

In `nexus/workers/scheduler.py`, import `enqueue_refresh_mailbox_tokens` beside `enqueue_refresh_due_accounts`, and immediately after the `enqueue_crawl_companies` call and its `count += 2` add:

```python
            # A connected mailbox whose grant was revoked must show Reconnect before a campaign
            # needs it, whether or not automation is on. The handler touches only mailboxes due
            # their daily refresh, so enqueuing every tick costs one indexed query.
            await enqueue_refresh_mailbox_tokens(queue=queue)
            count += 1
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_mailboxes.py tests/test_continuous_automation.py tests/test_crm_auto_sync.py tests/test_job_durability.py -n0 -q`
Expected: all pass (the UI test in this file still fails until Task 10).

- [ ] **Step 5: Commit**

```bash
git add nexus/workers/tasks.py nexus/workers/scheduler.py tests/test_continuous_automation.py tests/test_crm_auto_sync.py tests/test_engagement_mailboxes.py
git commit -m "feat(engagement): daily mailbox token refresh so a revoked grant shows Reconnect early"
```

---

### Task 9: Live mailbox suite, token helper and guide

**Files:**
- Create: `tests_live/engagement/test_mailboxes_live.py`, `scripts/engagement_live_token.py`, `docs/engagement/live-tests.md`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: The live test**

`tests_live/engagement/test_mailboxes_live.py`:

```python
"""The two mailbox adapters against a real Gmail and a real Microsoft 365 test mailbox (D21).

Mail flows only between the two test mailboxes. Every subject carries ``[nexus-live <run id>]``; the
Gmail test mailbox has a filter that deletes such mail on arrival (see docs/engagement/live-tests.md),
and Microsoft messages are deleted at the end of the test, so the mailboxes stay empty.

One scenario, in order, because each step needs the previous step's real ids:
Gmail sends → Gmail finds its own send by X-Nexus-Ref → Microsoft sees it arrive → Microsoft replies
in the same conversation → Gmail sees the reply in the original thread → Gmail follows up in that
thread → drafts on both sides → Sent-folder search finds the first send.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import make_msgid

import pytest

from nexus.engagement.config import GOOGLE_SCOPES, MICROSOFT_SCOPES, OAuthApp
from nexus.engagement.mailboxes import oauth, transport
from nexus.engagement.mailboxes.gmail import GmailProvider
from nexus.engagement.mailboxes.graph import GRAPH, IMMUTABLE, GraphProvider
from nexus.engagement.mailboxes.provider import CursorExpired, ThreadRef
from nexus.engagement.subjects import reply_subject
from tests_live.engagement.conftest import redirect_uri, require_env

POLL_SECONDS = 150


async def _gmail() -> GmailProvider:
    env = require_env("NEXUS_LIVE_GOOGLE_CLIENT_ID", "NEXUS_LIVE_GOOGLE_CLIENT_SECRET",
                      "NEXUS_LIVE_GMAIL_ADDRESS", "NEXUS_LIVE_GMAIL_REFRESH_TOKEN")
    app = OAuthApp("google", env["NEXUS_LIVE_GOOGLE_CLIENT_ID"],
                   env["NEXUS_LIVE_GOOGLE_CLIENT_SECRET"], "", redirect_uri("google"),
                   GOOGLE_SCOPES, ())
    data = await oauth.refresh(app, env["NEXUS_LIVE_GMAIL_REFRESH_TOKEN"])
    return GmailProvider(access_token=data["access_token"], email=env["NEXUS_LIVE_GMAIL_ADDRESS"])


async def _graph() -> GraphProvider:
    env = require_env("NEXUS_LIVE_MICROSOFT_CLIENT_ID", "NEXUS_LIVE_MICROSOFT_CLIENT_SECRET",
                      "NEXUS_LIVE_MICROSOFT_TENANT", "NEXUS_LIVE_M365_ADDRESS",
                      "NEXUS_LIVE_M365_REFRESH_TOKEN")
    app = OAuthApp("microsoft", env["NEXUS_LIVE_MICROSOFT_CLIENT_ID"],
                   env["NEXUS_LIVE_MICROSOFT_CLIENT_SECRET"], env["NEXUS_LIVE_MICROSOFT_TENANT"],
                   redirect_uri("microsoft"), MICROSOFT_SCOPES, ())
    data = await oauth.refresh(app, env["NEXUS_LIVE_M365_REFRESH_TOKEN"])
    return GraphProvider(access_token=data["access_token"], email=env["NEXUS_LIVE_M365_ADDRESS"])


def _mime(*, sender: str, to: str, subject: str, body: str, ref: str,
          in_reply_to: str = "", references: str = "") -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain="nexus-live.invalid")
    message["X-Nexus-Ref"] = ref
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = references or in_reply_to
    message.set_content(body)
    return message.as_bytes()


async def _wait_for(provider, cursor: str, marker: str):
    """Poll ``fetch_changes`` until a message whose raw text contains ``marker`` arrives."""
    deadline = asyncio.get_running_loop().time() + POLL_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        batch = await provider.fetch_changes(cursor)
        for message_id in batch.message_ids:
            message = await provider.get_message(message_id)
            if marker.encode() in message.raw:
                return message, batch.next_cursor
        cursor = batch.next_cursor or cursor
        await asyncio.sleep(10)
    pytest.fail(f"no message containing {marker!r} arrived within {POLL_SECONDS}s")


async def test_both_mailboxes_identify_themselves():
    gmail, graph = await _gmail(), await _graph()
    assert (await gmail.profile()).email == gmail.email
    assert (await graph.profile()).email == graph.email


async def test_a_conversation_round_trip_between_gmail_and_microsoft():
    gmail, graph = await _gmail(), await _graph()
    run = uuid.uuid4().hex[:10]
    subject = f"[nexus-live {run}] quick question"
    graph_created: list[str] = []
    try:
        gmail_cursor = (await gmail.fetch_changes(None)).next_cursor
        graph_cursor = (await graph.fetch_changes(None)).next_cursor

        # 1. Gmail sends and can find its own send by our reference header.
        ref = f"ref-{run}-1"
        before = datetime.now(timezone.utc)
        first = await gmail.send(
            _mime(sender=gmail.email, to=graph.email, subject=subject, ref=ref,
                  body=f"Hi,\n\nLive test {run}.\n\nBest,\nSam\n"),
            thread=None,
        )
        assert first.provider_message_id and first.provider_thread_id and first.rfc_message_id
        found = None
        for _ in range(12):
            found = await gmail.find_sent(ref_header=ref, to=graph.email, around=before)
            if found:
                break
            await asyncio.sleep(5)
        assert found and found.provider_message_id == first.provider_message_id

        # 2. Microsoft sees it arrive, with our header intact.
        arrived, graph_cursor = await _wait_for(graph, graph_cursor, run)
        graph_created.append(arrived.provider_message_id)
        assert message_from_bytes(arrived.raw)["X-Nexus-Ref"] == ref
        assert not arrived.outgoing

        # 3. Microsoft replies in the same conversation.
        reply = await graph.send(
            _mime(sender=graph.email, to=gmail.email, subject=reply_subject(subject),
                  ref=f"ref-{run}-2", in_reply_to=first.rfc_message_id,
                  body=f"Hi Sam,\n\nTry me in June. ({run})\n\nJane\n"),
            thread=ThreadRef(provider_thread_id=arrived.provider_thread_id,
                             reply_to_provider_message_id=arrived.provider_message_id,
                             in_reply_to=first.rfc_message_id),
        )
        graph_created.append(reply.provider_message_id)
        assert reply.provider_thread_id == arrived.provider_thread_id, (
            "Exchange started a new conversation: the Thread-Index child block was not honoured"
        )

        # 4. Gmail sees the reply in the original thread.
        answer, gmail_cursor = await _wait_for(gmail, gmail_cursor, "Try me in June")
        assert answer.provider_thread_id == first.provider_thread_id

        # 5. Gmail follows up in that thread, with exactly one "Re:".
        follow = await gmail.send(
            _mime(sender=gmail.email, to=graph.email, subject=reply_subject(reply_subject(subject)),
                  ref=f"ref-{run}-3", in_reply_to=message_from_bytes(answer.raw)["Message-ID"],
                  references=f"{first.rfc_message_id} {message_from_bytes(answer.raw)['Message-ID']}",
                  body=f"Hi Jane,\n\nNoted, June it is. ({run})\n\nBest,\nSam\n"),
            thread=ThreadRef(provider_thread_id=first.provider_thread_id),
        )
        assert follow.provider_thread_id == first.provider_thread_id

        # 6. Drafts on both sides.
        assert await gmail.create_draft(
            _mime(sender=gmail.email, to=graph.email, subject=reply_subject(subject),
                  ref=f"ref-{run}-4", body="draft"),
            thread=ThreadRef(provider_thread_id=first.provider_thread_id))
        draft_id = await graph.create_draft(
            _mime(sender=graph.email, to=gmail.email, subject=reply_subject(subject),
                  ref=f"ref-{run}-5", body="draft"),
            thread=ThreadRef(provider_thread_id=arrived.provider_thread_id,
                             reply_to_provider_message_id=arrived.provider_message_id))
        assert draft_id
        graph_created.append(draft_id)

        # 7. The cutover's Sent-folder search finds the first send by recipient and subject.
        searched = await gmail.search_sent(to=graph.email, subject=subject, around=before)
        assert searched and searched.provider_thread_id == first.provider_thread_id
    finally:
        for message_id in graph_created:
            try:
                await transport.request("DELETE", f"{GRAPH}/me/messages/{message_id}",
                                        token=graph._token, headers=IMMUTABLE)
            except Exception:
                pass


async def test_a_forgotten_gmail_cursor_is_reported_so_the_sync_can_resync():
    gmail = await _gmail()
    with pytest.raises(CursorExpired):
        await gmail.fetch_changes("1")
```

- [ ] **Step 2: The owner's refresh-token helper**

`scripts/engagement_live_token.py`:

```python
"""Get a refresh token for a live-test mailbox. The OWNER runs this on their own machine.

    python scripts/engagement_live_token.py google
    python scripts/engagement_live_token.py microsoft

It reads the app credentials from the environment (the same variables CI uses), opens the consent
screen in a browser, receives the redirect on http://localhost:8765/callback, and prints the refresh
token ONCE so it can be pasted into the GitHub secret. Nothing is written to disk.

The redirect URI http://localhost:8765/callback must be registered on both apps (see
docs/engagement/live-tests.md). Sign in as the TEST mailbox, not your own.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

REDIRECT = "http://localhost:8765/callback"


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"set {name} first")
    return value


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("google", "microsoft"):
        sys.exit("usage: python scripts/engagement_live_token.py google|microsoft")
    provider = sys.argv[1]

    from nexus.engagement.config import GOOGLE_SCOPES, MICROSOFT_SCOPES, OAuthApp
    from nexus.engagement.mailboxes import oauth
    from nexus.network.oauth import make_pkce

    if provider == "google":
        app = OAuthApp("google", _env("NEXUS_LIVE_GOOGLE_CLIENT_ID"),
                       _env("NEXUS_LIVE_GOOGLE_CLIENT_SECRET"), "", REDIRECT, GOOGLE_SCOPES, ())
    else:
        app = OAuthApp("microsoft", _env("NEXUS_LIVE_MICROSOFT_CLIENT_ID"),
                       _env("NEXUS_LIVE_MICROSOFT_CLIENT_SECRET"),
                       os.environ.get("NEXUS_LIVE_MICROSOFT_TENANT", "common"), REDIRECT,
                       MICROSOFT_SCOPES, ())

    verifier, challenge = make_pkce()
    state = os.urandom(12).hex()
    received: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib name)
            query = parse_qs(urlparse(self.path).query)
            received.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. Return to the terminal.")

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 8765), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    url = oauth.authorize_url(app, state=state, challenge=challenge)
    print("Opening the consent screen. Sign in as the TEST mailbox.\n", url, "\n")
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()

    if received.get("state") != state or "code" not in received:
        sys.exit(f"no authorization code received: {received.get('error', 'timeout')}")
    data = asyncio.run(oauth.exchange_code(app, code=received["code"], verifier=verifier))
    missing = oauth.missing_scopes(app, data.get("scope", ""))
    if missing:
        sys.exit(f"the grant is missing scopes: {missing}")
    print("Refresh token (paste into the GitHub secret, then clear this terminal):\n")
    print(data["refresh_token"])


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: The guide**

`docs/engagement/live-tests.md`:

````markdown
# Live engagement tests: test mailboxes and CI secrets

The engagement engine has no fake mail provider (D21). Its adapters are proven against one real
Gmail mailbox and one real Microsoft 365 mailbox in the `live-engagement` CI job and locally with
`pytest tests_live/engagement -n0 -q -rs`. The owner sets these up once; Claude never handles the
credentials.

## 1. Two test mailboxes

- A Gmail (or Google Workspace) address used for nothing else, e.g. `nexus.live.gmail@…`.
- A Microsoft 365 (work or school) address used for nothing else, e.g. `nexus-live@…onmicrosoft.com`.
  A free Microsoft 365 developer tenant works.

Add the Gmail address as a **test user** on the Google OAuth consent screen
(docs/engagement/setup-google.md step 3).

## 2. Keep the Gmail test mailbox empty

The app requests only `gmail.readonly` and `gmail.compose`, which cannot delete mail, so the tests
cannot clean Gmail up themselves. In the Gmail test mailbox: **Settings → Filters → Create a new
filter**, Subject `[nexus-live`, **Create filter**, tick **Delete it**, **Create filter**. Test mail
still arrives (the history API reports trashed messages) and Trash empties itself after 30 days.

The Microsoft test deletes its own messages at the end of each run.

## 3. Register the token helper's redirect URI

On both apps add `http://localhost:8765/callback` as a redirect URI (Google: OAuth client →
Authorised redirect URIs; Azure: Authentication → Web → Add URI). Only the helper below uses it.

## 4. Get a refresh token for each mailbox

On your own machine, with the app credentials exported:

```bash
export NEXUS_LIVE_GOOGLE_CLIENT_ID=... NEXUS_LIVE_GOOGLE_CLIENT_SECRET=...
python scripts/engagement_live_token.py google       # sign in as the Gmail TEST mailbox
export NEXUS_LIVE_MICROSOFT_CLIENT_ID=... NEXUS_LIVE_MICROSOFT_CLIENT_SECRET=... NEXUS_LIVE_MICROSOFT_TENANT=common
python scripts/engagement_live_token.py microsoft    # sign in as the Microsoft TEST mailbox
```

Each prints a refresh token once. Paste it straight into the GitHub secret, then clear the terminal.
While the Google app is in *Testing*, Gmail refresh tokens expire after 7 days; rerun the helper when
the live job reports `invalid_grant`, or publish the app.

## 5. GitHub Actions secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `NEXUS_LIVE_REDIRECT_BASE` | the base URL registered on both apps, e.g. `https://localhost` |
| `NEXUS_LIVE_GOOGLE_CLIENT_ID` / `NEXUS_LIVE_GOOGLE_CLIENT_SECRET` | the Google OAuth client |
| `NEXUS_LIVE_MICROSOFT_CLIENT_ID` / `NEXUS_LIVE_MICROSOFT_CLIENT_SECRET` / `NEXUS_LIVE_MICROSOFT_TENANT` | the Azure app |
| `NEXUS_LIVE_GMAIL_ADDRESS` / `NEXUS_LIVE_GMAIL_REFRESH_TOKEN` | the Gmail test mailbox |
| `NEXUS_LIVE_M365_ADDRESS` / `NEXUS_LIVE_M365_REFRESH_TOKEN` | the Microsoft test mailbox |

Later phases add the ledger store connection strings (phase 06) and an LLM key (phase 09).

## 6. Run

Actions → CI → Run workflow (the `live-engagement` job), or locally with the same variables exported:
`pytest tests_live/engagement -n0 -q -rs`. A skipped test names the variable it is missing.
````

- [ ] **Step 4: CI secrets**

In `.github/workflows/ci.yml`, job `live-engagement`, after `NEXUS_LIVE_MICROSOFT_TENANT`, add:

```yaml
      NEXUS_LIVE_GMAIL_ADDRESS: ${{ secrets.NEXUS_LIVE_GMAIL_ADDRESS }}
      NEXUS_LIVE_GMAIL_REFRESH_TOKEN: ${{ secrets.NEXUS_LIVE_GMAIL_REFRESH_TOKEN }}
      NEXUS_LIVE_M365_ADDRESS: ${{ secrets.NEXUS_LIVE_M365_ADDRESS }}
      NEXUS_LIVE_M365_REFRESH_TOKEN: ${{ secrets.NEXUS_LIVE_M365_REFRESH_TOKEN }}
```

- [ ] **Step 5: Run the live suite**

Run: `pytest tests_live/engagement -n0 -q -rs`
Expected without secrets: every test skipped, each naming its missing variables. With the owner's secrets: `5 passed`. If `test_a_conversation_round_trip_between_gmail_and_microsoft` fails on the Exchange conversation assertion, stop and report: the Thread-Index approach needs revisiting before phase 07 relies on it.

- [ ] **Step 6: Commit**

```bash
git add tests_live/engagement/test_mailboxes_live.py scripts/engagement_live_token.py docs/engagement/live-tests.md .github/workflows/ci.yml
git commit -m "test(engagement): live Gmail <-> Microsoft 365 round trip; owner token helper and guide"
```

---

### Task 10: My mailboxes page

Invoke the `impeccable` skill before this task.

**Files:**
- Create: `frontend/src/pages/engagement/MailboxesPage.tsx`, `frontend/src/pages/engagement/MailboxesPage.module.css`
- Modify: `frontend/src/App.tsx`, `frontend/src/app/nav.tsx`, `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`
- Test: `tests/test_engagement_mailboxes.py::test_the_mailboxes_page_is_reachable_by_every_member`

- [ ] **Step 1: See the UI test fail**

Run: `pytest tests/test_engagement_mailboxes.py -n0 -q -k reachable`
Expected: FAIL — `App.tsx` has no `/mailboxes` route.

- [ ] **Step 2: Types and client**

Append to `frontend/src/lib/types.ts`:

```ts
/** An SDR mailbox connected by OAuth. Tokens never leave the server. */
export interface ConnectedMailbox {
  id: string;
  provider: "google" | "microsoft";
  email: string;
  display_name: string;
  owner_user_id: string;
  mine: boolean;
  status: "connected" | "needs_reauth" | "revoked" | "error";
  last_error: string | null;
  timezone: string;
  signature: string;
  reply_confidence: number | null;
  effective_reply_confidence: number;
  reply_confidence_min: number;
  reply_confidence_max: number;
  paused_until: string | null;
  last_synced_at: string | null;
  created_at: string;
}

export interface MailboxProviderState {
  provider: "google" | "microsoft";
  configured: boolean;
}
```

In `frontend/src/lib/api.ts`, add `ConnectedMailbox,` and `MailboxProviderState,` to the type imports after `EngagementSetup,`, and immediately before `engagementSetup(signal?: AbortSignal) {` add:

```ts
  // ---- engagement: my mailboxes ----
  listConnectedMailboxes(team = false, signal?: AbortSignal) {
    return this.request<ConnectedMailbox[]>("/engagement/mailboxes", { query: { team }, signal });
  }
  connectedMailboxProviders(signal?: AbortSignal) {
    return this.request<MailboxProviderState[]>("/engagement/mailboxes/providers", { signal });
  }
  startMailboxConnect(provider: "google" | "microsoft", timezone: string) {
    return this.request<{ authorize_url: string }>(
      `/engagement/mailboxes/oauth/${provider}/start`,
      { method: "POST", body: { timezone } },
    );
  }
  updateConnectedMailbox(
    id: string,
    body: { timezone?: string; signature?: string; reply_confidence?: number;
      clear_reply_confidence?: boolean },
  ) {
    return this.request<ConnectedMailbox>(`/engagement/mailboxes/${id}`, { method: "PATCH", body });
  }
  checkConnectedMailbox(id: string) {
    return this.request<ConnectedMailbox>(`/engagement/mailboxes/${id}/check`, {
      method: "POST",
    });
  }
  disconnectConnectedMailbox(id: string) {
    return this.request<null>(`/engagement/mailboxes/${id}`, { method: "DELETE" });
  }
```

- [ ] **Step 3: The page**

`frontend/src/pages/engagement/MailboxesPage.tsx`:

```tsx
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Icons,
  Input,
  Modal,
  Select,
  Skeleton,
  Textarea,
} from "@/components/ui";
import type { BadgeTone } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { ConnectedMailbox, MailboxProviderState } from "@/lib/types";
import styles from "./MailboxesPage.module.css";

/**
 * My mailboxes: an SDR connects their own Gmail or Microsoft 365 mailbox (spec §9, D1, D2).
 *
 * Campaign email is sent from, and replies are read in, the SDR's own mailbox, so this page belongs
 * to every member rather than to workspace Settings (admin-only). Connecting is OAuth: the browser
 * goes to Google or Microsoft and comes back here with `connected=` or `error=`.
 *
 * The browser's own timezone is sent when connecting, because follow-ups default to the SDR's local
 * working day when a contact's timezone cannot be resolved.
 */

const PROVIDER_LABEL: Record<ConnectedMailbox["provider"], string> = {
  google: "Google (Gmail)",
  microsoft: "Microsoft 365 (Outlook)",
};

const STATUS: Record<ConnectedMailbox["status"], { label: string; tone: BadgeTone }> = {
  connected: { label: "Connected", tone: "success" },
  needs_reauth: { label: "Reconnect needed", tone: "warning" },
  error: { label: "Setup problem", tone: "danger" },
  revoked: { label: "Disconnected", tone: "neutral" },
};

/** Why a connect attempt came back without a mailbox, in words an SDR can act on. */
const CONNECT_ERRORS: Record<string, string> = {
  denied: "You cancelled on the Google or Microsoft screen, so nothing was connected.",
  oauth_failed: "The sign-in did not complete. Try again.",
  bad_state: "That sign-in link expired. Start again from this page.",
  not_configured: "Your administrator has not finished setting up this provider.",
  exchange_failed: "Google or Microsoft refused the sign-in. Try again, or ask your administrator.",
  missing_scopes:
    "Mail access was not granted. Connect again and leave every permission ticked on the consent screen.",
  profile_failed: "The mailbox address could not be read. Try again.",
  owned_by_colleague:
    "A colleague has already connected that mailbox. They need to disconnect it before you can.",
  not_a_member: "You are no longer a member of this workspace.",
};

function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function timezoneOptions(current: string) {
  let zones: string[] = [];
  try {
    zones = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] })
      .supportedValuesOf?.("timeZone") ?? [];
  } catch {
    zones = [];
  }
  if (!zones.includes(current)) zones = [current, ...zones];
  return zones.map((z) => ({ value: z, label: z.replace(/_/g, " ") }));
}

export function MailboxesPage() {
  const api = useApiClient();
  const toast = useToast();
  const { session } = useAuth();
  const [params, setParams] = useSearchParams();
  const isManager = session?.role === "manager" || session?.role === "admin"
    || session?.role === "owner";
  const [team, setTeam] = useState(false);
  const [connecting, setConnecting] = useState<string | null>(null);

  const providers = useApi<MailboxProviderState[]>((s) => api.connectedMailboxProviders(s), []);
  const mailboxes = useApi<ConnectedMailbox[]>((s) => api.listConnectedMailboxes(team, s), [team]);

  useEffect(() => {
    const connected = params.get("connected");
    const error = params.get("error");
    if (!connected && !error) return;
    if (connected) {
      toast.success("Mailbox connected", "Campaign email will send from it and replies will be read in it.");
    } else if (error) {
      toast.error("Mailbox not connected", CONNECT_ERRORS[error] ?? "Something went wrong. Try again.");
    }
    setParams({}, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function connect(provider: "google" | "microsoft") {
    setConnecting(provider);
    try {
      const { authorize_url } = await api.startMailboxConnect(provider, browserTimezone());
      window.location.assign(authorize_url);
    } catch (err) {
      setConnecting(null);
      toast.error("Couldn't start connecting", err instanceof ApiError ? err.detail : "Please try again.");
    }
  }

  return (
    <div>
      <PageHeader
        title="My mailboxes"
        description="Connect the Gmail or Microsoft 365 mailbox you prospect from. Campaign emails send from it, replies are read in it, and nothing else in it is stored."
        actions={
          isManager ? (
            <Button variant="secondary" size="sm" onClick={() => setTeam((t) => !t)}
              aria-pressed={team}>
              {team ? "Show mine" : "Show team"}
            </Button>
          ) : undefined
        }
      />

      <Card padding="lg" className={styles.connect}>
        <h3 className={styles.sectionTitle}>Connect a mailbox</h3>
        <DataState
          state={providers}
          errorTitle="Couldn't check which providers are available"
          skeleton={<Skeleton width="100%" height={44} />}
        >
          {(list) => (
            <div className={styles.providerRow}>
              {list.map((p) => (
                <div key={p.provider} className={styles.provider}>
                  <Button
                    onClick={() => connect(p.provider)}
                    disabled={!p.configured || connecting !== null}
                    loading={connecting === p.provider}
                  >
                    Connect {PROVIDER_LABEL[p.provider]}
                  </Button>
                  {!p.configured && (
                    <span className={styles.hint}>Not set up yet. Ask your administrator.</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </DataState>
      </Card>

      <DataState
        state={mailboxes}
        errorTitle="Couldn't load mailboxes"
        skeleton={<Skeleton width="100%" height={180} />}
        isEmpty={(rows) => rows.length === 0}
        empty={
          <EmptyState
            icon={<Icons.MailIcon />}
            title={team ? "Nobody on the team has connected a mailbox" : "No mailbox connected yet"}
            description="Connect one above. Until then campaigns cannot send on your behalf."
          />
        }
      >
        {(rows) => (
          <ul className={styles.list}>
            {rows.map((m) => (
              <MailboxCard key={m.id} mailbox={m} onChanged={mailboxes.refetch}
                onReconnect={() => connect(m.provider)} canDisconnect={m.mine || isManager} />
            ))}
          </ul>
        )}
      </DataState>
    </div>
  );
}

function MailboxCard({
  mailbox, onChanged, onReconnect, canDisconnect,
}: {
  mailbox: ConnectedMailbox;
  onChanged: () => void;
  onReconnect: () => void;
  canDisconnect: boolean;
}) {
  const api = useApiClient();
  const toast = useToast();
  const [timezone, setTimezone] = useState(mailbox.timezone);
  const [signature, setSignature] = useState(mailbox.signature);
  const [confidence, setConfidence] = useState(
    mailbox.reply_confidence === null ? "" : String(mailbox.reply_confidence),
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const zones = useMemo(() => timezoneOptions(mailbox.timezone), [mailbox.timezone]);
  const status = STATUS[mailbox.status];
  const dirty = timezone !== mailbox.timezone || signature !== mailbox.signature
    || confidence !== (mailbox.reply_confidence === null ? "" : String(mailbox.reply_confidence));

  async function run(label: string, action: () => Promise<unknown>, success?: [string, string]) {
    setBusy(label);
    try {
      await action();
      if (success) toast.success(success[0], success[1]);
      onChanged();
    } catch (err) {
      toast.error("That didn't work", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(null);
    }
  }

  function save() {
    const body: Parameters<typeof api.updateConnectedMailbox>[1] = { timezone, signature };
    if (confidence.trim() === "") body.clear_reply_confidence = true;
    else body.reply_confidence = Number(confidence);
    return run("save", () => api.updateConnectedMailbox(mailbox.id, body), ["Saved", "Your mailbox settings are updated."]);
  }

  return (
    <li>
      <Card padding="lg" className={styles.card}>
        <div className={styles.cardHead}>
          <div>
            <p className={styles.email}>{mailbox.email}</p>
            <p className={styles.meta}>{PROVIDER_LABEL[mailbox.provider]}</p>
          </div>
          <Badge tone={status.tone} dot>{status.label}</Badge>
        </div>

        {mailbox.last_error && <p className={styles.error} role="status">{mailbox.last_error}</p>}

        {mailbox.mine && mailbox.status !== "revoked" && (
          <div className={styles.fields}>
            <Field label="Your timezone" hint="Used when a contact's own timezone is unknown.">
              <Select value={timezone} onChange={(e) => setTimezone(e.target.value)} options={zones} />
            </Field>
            <Field
              label="Reply confidence bar"
              hint={`How sure the AI must be before a reply is acted on without you. Leave blank for the workspace default (${mailbox.effective_reply_confidence.toFixed(2)}); allowed ${mailbox.reply_confidence_min.toFixed(2)}–${mailbox.reply_confidence_max.toFixed(2)}.`}
            >
              <Input
                type="number"
                inputMode="decimal"
                step="0.01"
                min={mailbox.reply_confidence_min}
                max={mailbox.reply_confidence_max}
                value={confidence}
                onChange={(e) => setConfidence(e.target.value)}
                placeholder={mailbox.effective_reply_confidence.toFixed(2)}
              />
            </Field>
            <Field label="Signature" hint="Added under every email this mailbox sends. Plain text.">
              <Textarea rows={4} value={signature} onChange={(e) => setSignature(e.target.value)} />
            </Field>
          </div>
        )}

        <div className={styles.actions}>
          {mailbox.mine && mailbox.status !== "revoked" && (
            <Button onClick={save} disabled={!dirty} loading={busy === "save"}>Save</Button>
          )}
          {mailbox.mine && mailbox.status === "connected" && (
            <Button variant="secondary" loading={busy === "check"}
              onClick={() => run("check", () => api.checkConnectedMailbox(mailbox.id),
                ["Checked", "The mailbox signed in."])}>
              Check connection
            </Button>
          )}
          {mailbox.mine && (mailbox.status === "needs_reauth" || mailbox.status === "revoked") && (
            <Button variant="secondary" onClick={onReconnect}>Reconnect</Button>
          )}
          {canDisconnect && mailbox.status !== "revoked" && (
            <Button variant="ghost" onClick={() => setConfirming(true)}>Disconnect</Button>
          )}
        </div>
      </Card>

      <Modal
        open={confirming}
        onClose={() => setConfirming(false)}
        title={`Disconnect ${mailbox.email}?`}
        description="Campaign steps from this mailbox pause and replies stop being read. Emails already sent stay in its history."
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
            <Button variant="danger" loading={busy === "disconnect"}
              onClick={() => run("disconnect", async () => {
                await api.disconnectConnectedMailbox(mailbox.id);
                setConfirming(false);
              }, ["Disconnected", `${mailbox.email} is no longer connected.`])}>
              Disconnect
            </Button>
          </>
        }
      >
        <p className={styles.meta}>You can connect it again at any time.</p>
      </Modal>
    </li>
  );
}

export default MailboxesPage;
```

`frontend/src/pages/engagement/MailboxesPage.module.css`:

```css
.connect {
  margin-bottom: var(--space-5);
}

.sectionTitle {
  margin: 0 0 var(--space-3);
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--text);
}

.providerRow {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4);
}

.provider {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.hint,
.meta {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-muted);
  line-height: var(--leading);
}

.list {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  margin: 0;
  padding: 0;
  list-style: none;
}

.card {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.cardHead {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.email {
  margin: 0;
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--text);
  overflow-wrap: anywhere;
}

.error {
  margin: 0;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--warning);
  border-radius: var(--radius);
  background: var(--warning-quiet);
  color: var(--text);
  font-size: var(--text-sm);
  line-height: var(--leading);
}

.fields {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
  gap: var(--space-4);
}

.fields > :last-child {
  grid-column: 1 / -1;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}
```

- [ ] **Step 4: Route and nav**

In `frontend/src/App.tsx`, after the `CadencesPage` lazy import:

```tsx
const MailboxesPage = lazyPage(() => import("@/pages/engagement/MailboxesPage"), "MailboxesPage");
```

and immediately before the `/cadences` route:

```tsx
                {/* Every member: an SDR connects their own mailbox. Gated like the email composer,
                    on module.outreach, so a plan without outreach hides it everywhere. */}
                <Route
                  path="/mailboxes"
                  element={
                    <RequireCapability capability="module.outreach" name="My mailboxes">
                      <MailboxesPage />
                    </RequireCapability>
                  }
                />
```

In `frontend/src/app/nav.tsx`, import `MailIcon` and add after the Contacts item:

```tsx
  { to: "/mailboxes", label: "My mailboxes", icon: <MailIcon />, capability: "module.outreach" },
```

- [ ] **Step 5: Tests and typecheck**

Run: `pytest tests/test_engagement_mailboxes.py tests/test_plan_gated_nav.py -n0 -q`
Expected: all pass (`test_the_routes_guard_the_same_capabilities_the_nav_hides` holds: the route and nav item both carry `module.outreach`).

Run: `cd frontend && npm run typecheck`
Expected: exits 0.

- [ ] **Step 6: Look at it**

Start the preview, sign in as a rep, open My mailboxes at desktop and 375 px: the connect buttons read "Not set up yet" without configuration; with a configured provider, Connect goes to Google or Microsoft; returning with `?error=owned_by_colleague` shows the explanation toast and cleans the URL.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/engagement frontend/src/App.tsx frontend/src/app/nav.tsx frontend/src/lib/api.ts frontend/src/lib/types.ts
git commit -m "feat(engagement): My mailboxes page - connect Gmail or Microsoft 365, reconnect, disconnect"
```

---

### Task 11: Verify the phase

- [ ] **Step 1:** `ruff check nexus tests tests_live scripts` → `All checks passed!`
- [ ] **Step 2:** `pytest -n auto -p no:cacheprovider --timeout=120 -q` → whole suite passes.
- [ ] **Step 3:** `cd frontend && npm run typecheck && npm run build` → both exit 0.
- [ ] **Step 4:** `pytest tests_live/engagement -n0 -q -rs` → passes with secrets, skips by name without.

---

## Spec coverage for this phase

| Spec item | Task |
|---|---|
| §3 `MailProvider`: send, fetch_changes, get_message, find_sent, search_sent (renew_notifications arrives with webhooks in phase 09) | 1, 4, 5 |
| §3 two implementations, no fake | 4, 5, 9 |
| §5 threading: Gmail `threadId`; Graph same conversation | 4, 5, 9 |
| §5 provider limits carry a reset time | 2 |
| §5 reconciliation by `X-Nexus-Ref` in the Sent folder | 4, 5, 9 |
| §6 cursor expiry → resync | 4, 9 |
| §9 Settings → Mailboxes: connect Google/Microsoft, status (today's count and badge arrive with sending in phase 07) | 7, 10 |
| §12 until configured, "not configured" | 7, 10 |
| §13 cutover thread recovery (`search_sent`) | 4, 5, 9 |
| §14 live suite: connect/refresh, send, thread follow-ups, exactly one Re:, reply matched in thread | 9 |
| D1, D2 OAuth only | 3, 7 |
| D16 same thread | 5, 9 |
| D21 no mocks, live CI | 9 |
