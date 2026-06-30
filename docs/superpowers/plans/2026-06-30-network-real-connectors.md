# Network — Real Connectors (Production) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the demo/sample data path with real ingestion — live Google + Microsoft OAuth sync (Contacts + Calendar) and LinkedIn data-export CSV upload — with production-grade token encryption, OAuth state+PKCE, refresh, and incremental sync. Remove all product mocks; keep `FixtureConnector` only as the offline test seam.

**Architecture:** New `OAuthConnector` base under the existing `NetworkConnector` Protocol; `GoogleConnector`/`MicrosoftConnector` call People/Calendar/Graph APIs via `httpx`. OAuth `state` is a short-TTL signed JWT carrying a PKCE verifier; tokens are encrypted at rest with Fernet (key derived from `secret_key`, no new dep/secret). LinkedIn enters via a `Connections.csv` upload parsed to `RawIdentity`. The worker refreshes tokens before each sync. Reuses the unchanged `ingest_batch` (resolve → strength → edges).

**Tech Stack:** Python 3.11, FastAPI, async SQLAlchemy 2.0, Pydantic v2, `httpx` (already a dep), `cryptography`/Fernet (transitive via `python-jose[cryptography]`), `python-jose` (JWT state), `python-multipart` (CSV upload). React 18 + TS + Vite frontend. Offline tests via `httpx.MockTransport`.

**Run tests with `py -3.10 -m pytest` on this Windows box** (bare `python` is 3.14 without dev deps). Frontend: `cd frontend && npm run build`.

**Spec:** [docs/superpowers/specs/2026-06-30-network-real-connectors-design.md](../specs/2026-06-30-network-real-connectors-design.md).

**Non-breaking:** No schema change (tokens reuse the encrypted `network_source_accounts.oauth` JSON blob + `sync_cursor`). New modules/routes/settings are additive; existing endpoints and the offline test path are unchanged. The only edits to existing files: `config.py` (settings), `connectors/registry.py` (provider map), `routers/network.py` (routes + restrict `POST /accounts`), `workers/tasks.py` (token refresh in the sync handler), and the frontend source.

---

## File structure

**Create:**
- `nexus/network/crypto.py` — Fernet seal/unseal of the token bundle (Task 2)
- `nexus/network/oauth.py` — PKCE + signed OAuth-state JWT (Task 3)
- `nexus/network/linkedin_csv.py` — parse a LinkedIn `Connections.csv` export (Task 4)
- `nexus/network/connectors/oauthbase.py` — `OAuthConnector` (authorize URL, token exchange, refresh, httpx client) (Task 5)
- `nexus/network/connectors/google.py` — `GoogleConnector.fetch` (People + Calendar) (Task 6)
- `nexus/network/connectors/microsoft.py` — `MicrosoftConnector.fetch` (Graph) (Task 7)
- `tests/test_network_crypto.py`, `tests/test_network_oauth_state.py`, `tests/test_network_linkedin_csv.py`, `tests/test_google_connector.py`, `tests/test_microsoft_connector.py`, `tests/test_network_oauth_api.py`

**Modify:**
- `nexus/core/config.py` — provider settings (Task 1)
- `nexus/network/connectors/registry.py` — map `google`/`microsoft` → real adapters, built from settings (Task 8)
- `nexus/workers/tasks.py` — `handle_sync_network_account` refreshes tokens + records errors (Task 9)
- `nexus/network/schemas.py` + `nexus/api/routers/network.py` — OAuth routes, LinkedIn import, restrict `POST /accounts` (Task 10)
- `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`, `frontend/src/pages/NetworkPage.tsx` — real OAuth/LinkedIn UI, remove mocks (Task 11)

---

## Task 1: Provider settings

**Files:** Modify `nexus/core/config.py`; Test: `tests/test_network_oauth_state.py` (settings assertion lives with the oauth tests — added in Task 3; here just add the fields).

- [ ] **Step 1: Add the settings**

In `nexus/core/config.py`, add these fields inside `class Settings` (place them right after the `metrics_enabled` field, before the "Hosted web-search API keys" block):

```python
    # Relationship-graph network connectors (real OAuth + token encryption). Empty default →
    # the provider is inert (its /oauth/start returns 400) — never a fake-data fallback.
    network_google_client_id: str = ""
    network_google_client_secret: str = ""
    network_microsoft_client_id: str = ""
    network_microsoft_client_secret: str = ""
    network_microsoft_tenant: str = "common"   # Azure tenant id, or "common" (work + personal)
    # Base URL the OAuth provider redirects back to, e.g. https://app.example.com. The callback
    # path (/api/network/oauth/{provider}/callback) is appended; never client-supplied.
    network_oauth_redirect_base: str = ""
    # Fernet key (urlsafe-b64, 32 bytes) for encrypting stored OAuth tokens. Empty → derived
    # deterministically from secret_key, so tokens are always encrypted with no extra secret.
    network_token_enc_key: str = ""
```

- [ ] **Step 2: Verify it loads**

Run: `py -3.10 -c "from nexus.core.config import Settings; s=Settings(); print(s.network_microsoft_tenant, repr(s.network_google_client_id))"`
Expected: `common ''`

- [ ] **Step 3: Commit**

```bash
git add nexus/core/config.py
git commit -m "feat(network): OAuth provider settings (inert until configured)"
```

---

## Task 2: Token encryption (Fernet)

**Files:** Create `nexus/network/crypto.py`; Test: `tests/test_network_crypto.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_crypto.py
from __future__ import annotations


def test_seal_unseal_round_trip_and_opacity():
    from nexus.network.crypto import seal_tokens, unseal_tokens

    bundle = {"access_token": "AT-123", "refresh_token": "RT-456", "expires_at": 1900000000}
    blob = seal_tokens(bundle)
    assert isinstance(blob, dict) and "enc" in blob
    assert "AT-123" not in blob["enc"]  # ciphertext, not plaintext
    assert unseal_tokens(blob) == bundle


def test_unseal_tolerates_empty_and_garbage():
    from nexus.network.crypto import unseal_tokens

    assert unseal_tokens({}) == {}
    assert unseal_tokens({"enc": "not-a-valid-token"}) == {}
    assert unseal_tokens(None) == {}  # type: ignore[arg-type]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_network_crypto.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.network.crypto'`

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/crypto.py
"""Encryption of stored OAuth token bundles (at rest).

Tokens live in ``network_source_accounts.oauth`` (a JSON column) as ``{"enc": "<fernet>"}`` and are
never serialized to a client. The Fernet key comes from ``network_token_enc_key`` or, when unset, is
derived deterministically from ``secret_key`` — so tokens are always encrypted with no extra required
secret. ``cryptography`` ships transitively via ``python-jose[cryptography]`` (no new dependency).
"""
from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken

from nexus.core.config import get_settings


def _fernet() -> Fernet:
    key = (get_settings().network_token_enc_key or "").strip()
    if not key:
        digest = hashlib.sha256(get_settings().secret_key.encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode())


def seal_tokens(bundle: dict) -> dict:
    """Encrypt a token bundle for storage. Returns the JSON-column value ``{"enc": "..."}``."""
    token = _fernet().encrypt(json.dumps(bundle).encode()).decode()
    return {"enc": token}


def unseal_tokens(oauth: dict | None) -> dict:
    """Decrypt a stored ``oauth`` value back to the token bundle. Tolerant: returns ``{}`` for
    empty/missing/tampered input rather than raising (a corrupt blob → 'reconnect', not a crash)."""
    if not oauth:
        return {}
    enc = oauth.get("enc")
    if not enc:
        return {}
    try:
        return json.loads(_fernet().decrypt(enc.encode()).decode())
    except (InvalidToken, ValueError, TypeError):
        return {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_network_crypto.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/network/crypto.py tests/test_network_crypto.py
git commit -m "feat(network): encrypt OAuth tokens at rest (Fernet)"
```

---

## Task 3: OAuth state token + PKCE

**Files:** Create `nexus/network/oauth.py`; Test: `tests/test_network_oauth_state.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_oauth_state.py
from __future__ import annotations

import time


def test_pkce_pair_is_valid():
    import base64
    import hashlib

    from nexus.network.oauth import make_pkce

    verifier, challenge = make_pkce()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_state_sign_verify_round_trip_and_tamper_reject():
    from nexus.network.oauth import sign_state, verify_state

    tok = sign_state(member_id="m1", tenant_id="t1", provider="google", verifier="v123")
    claims = verify_state(tok)
    assert claims and claims["mid"] == "m1" and claims["tid"] == "t1"
    assert claims["prov"] == "google" and claims["pkce"] == "v123"

    assert verify_state(tok + "x") is None          # tampered signature
    assert verify_state("not.a.jwt") is None         # garbage
    # an access token (wrong typ) must not pass as oauth state
    from nexus.core.security import create_access_token

    assert verify_state(create_access_token(user_id="u", tenant_id="t", role="rep")) is None


def test_state_expires():
    from nexus.network import oauth

    tok = oauth.sign_state(member_id="m1", tenant_id="t1", provider="google", verifier="v",
                           ttl_s=1)
    assert oauth.verify_state(tok) is not None
    time.sleep(2)
    assert oauth.verify_state(tok) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_network_oauth_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.network.oauth'`

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/oauth.py
"""OAuth helpers for the network connectors: PKCE and a signed, short-TTL state token.

The ``state`` round-tripped through the provider is a JWT (reusing the app's ``secret_key`` /
``jose``) carrying the member, tenant, provider, and the PKCE verifier — so the callback can resume
the flow with no server-side session store, and a tampered/expired/foreign token is rejected.
"""
from __future__ import annotations

import base64
import hashlib
import os
from datetime import timedelta

from jose import JWTError, jwt

from nexus.core.config import get_settings
from nexus.core.db import utcnow

_STATE_TYP = "net_oauth"
_DEFAULT_TTL_S = 600


def make_pkce() -> tuple[str, str]:
    """Return (verifier, S256 challenge) per RFC 7636."""
    verifier = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def sign_state(
    *, member_id: str, tenant_id: str, provider: str, verifier: str, ttl_s: int = _DEFAULT_TTL_S
) -> str:
    s = get_settings()
    now = utcnow()
    payload = {
        "typ": _STATE_TYP, "mid": member_id, "tid": tenant_id, "prov": provider, "pkce": verifier,
        "iat": int(now.timestamp()), "exp": int((now + timedelta(seconds=ttl_s)).timestamp()),
    }
    return jwt.encode(payload, s.secret_key, algorithm=s.jwt_algorithm)


def verify_state(token: str) -> dict | None:
    s = get_settings()
    try:
        claims = jwt.decode(token, s.secret_key, algorithms=[s.jwt_algorithm])
    except JWTError:
        return None
    return claims if claims.get("typ") == _STATE_TYP else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_network_oauth_state.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/network/oauth.py tests/test_network_oauth_state.py
git commit -m "feat(network): PKCE + signed OAuth state token"
```

---

## Task 4: LinkedIn CSV export parser

**Files:** Create `nexus/network/linkedin_csv.py`; Test: `tests/test_network_linkedin_csv.py`

- [ ] **Step 1: Write the failing test**

LinkedIn's `Connections.csv` begins with a 2–3 line "Notes:" preamble before the real header row `First Name,Last Name,URL,Email Address,Company,Position,Connected On`.

```python
# tests/test_network_linkedin_csv.py
from __future__ import annotations


def test_parse_linkedin_export_with_preamble():
    from nexus.network.linkedin_csv import parse_linkedin_csv

    raw = (
        "Notes:\n"
        '"When exporting your connection data, you may notice that...":\n'
        "\n"
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Ada,Okafor,https://www.linkedin.com/in/ada,ada@helix.com,Helix Health,CTO,01 Jun 2026\n"
        "Bob,Roy,https://www.linkedin.com/in/bobroy,,Nimbus Rx,VP Engineering,15 May 2026\n"
    )
    rows = parse_linkedin_csv(raw.encode("utf-8"))
    assert len(rows) == 2
    ada = rows[0]
    assert ada.external_id == "https://www.linkedin.com/in/ada"
    assert ada.email == "ada@helix.com"
    assert ada.name == "Ada Okafor"
    assert ada.title == "CTO"
    assert ada.company == "Helix Health"
    assert ada.relation == "linkedin_1st"
    # second row has no email but still imports (external_id from the profile URL)
    assert rows[1].email is None
    assert rows[1].name == "Bob Roy"


def test_parse_rejects_a_non_linkedin_csv():
    from nexus.network.linkedin_csv import LinkedInCsvError, parse_linkedin_csv

    try:
        parse_linkedin_csv(b"foo,bar\n1,2\n")
    except LinkedInCsvError:
        return
    raise AssertionError("expected LinkedInCsvError for a CSV without LinkedIn headers")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_network_linkedin_csv.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.network.linkedin_csv'`

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/linkedin_csv.py
"""Parse a member's official LinkedIn data export (``Connections.csv``) into RawIdentity rows.

LinkedIn has no API to read a member's connections within ToS; the compliant path is the member's
own export ("Settings → Get a copy of your data → Connections"). The file starts with a short
"Notes:" preamble before the real header row, which we skip.
"""
from __future__ import annotations

import csv
import io

from nexus.network.connectors.base import RawIdentity

_REQUIRED = {"First Name", "Last Name", "Company", "Position"}


class LinkedInCsvError(ValueError):
    """Raised when the upload is not a recognizable LinkedIn Connections export."""


def _decode(content: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def parse_linkedin_csv(content: bytes) -> list[RawIdentity]:
    text = _decode(content)
    lines = text.splitlines()
    # Find the header row (LinkedIn prepends a Notes preamble + a blank line).
    header_idx = next(
        (i for i, ln in enumerate(lines) if ln.startswith("First Name,")), None
    )
    if header_idx is None:
        raise LinkedInCsvError("not a LinkedIn Connections export (missing header row)")

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    if not _REQUIRED.issubset(set(reader.fieldnames or [])):
        raise LinkedInCsvError("LinkedIn export is missing expected columns")

    out: list[RawIdentity] = []
    for i, row in enumerate(reader):
        first = (row.get("First Name") or "").strip()
        last = (row.get("Last Name") or "").strip()
        name = " ".join(p for p in (first, last) if p)
        if not name:
            continue
        email = (row.get("Email Address") or "").strip() or None
        url = (row.get("URL") or "").strip()
        out.append(
            RawIdentity(
                external_id=url or f"linkedin:{name.lower()}:{i}",
                email=email,
                name=name,
                title=(row.get("Position") or "").strip() or None,
                company=(row.get("Company") or "").strip() or None,
                handle=url or None,
                relation="linkedin_1st",
            )
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_network_linkedin_csv.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/network/linkedin_csv.py tests/test_network_linkedin_csv.py
git commit -m "feat(network): LinkedIn data-export CSV parser"
```

---

## Task 5: OAuth connector base

**Files:** Create `nexus/network/connectors/oauthbase.py`; Test: `tests/test_google_connector.py` (shared helper added here; Google fetch test added in Task 6)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_google_connector.py
from __future__ import annotations

import httpx
import pytest


def test_authorize_url_includes_pkce_and_scopes():
    from nexus.network.connectors.google import GoogleConnector

    c = GoogleConnector(client_id="cid", client_secret="sec", redirect_uri="https://x/cb")
    url = c.authorize_url_for(state="ST", code_challenge="CH")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid" in url and "state=ST" in url
    assert "code_challenge=CH" in url and "code_challenge_method=S256" in url
    assert "access_type=offline" in url and "contacts.readonly" in url


async def test_exchange_code_posts_and_returns_tokens():
    from nexus.network.connectors.google import GoogleConnector

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://oauth2.googleapis.com/token"
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "CODE" and body["code_verifier"] == "VER"
        return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT",
                                         "expires_in": 3600})

    c = GoogleConnector(client_id="cid", client_secret="sec", redirect_uri="https://x/cb",
                        transport=httpx.MockTransport(handler))
    tokens = await c.exchange_code(code="CODE", code_verifier="VER")
    assert tokens["access_token"] == "AT" and tokens["refresh_token"] == "RT"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_google_connector.py::test_authorize_url_includes_pkce_and_scopes -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.network.connectors.google'` (Google lands in Task 6; the base it extends lands here)

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/connectors/oauthbase.py
"""Shared OAuth 2.0 (authorization-code + PKCE) machinery for real network connectors.

Subclasses set the provider endpoints/scopes and implement ``fetch``. This base owns: building the
authorize URL, exchanging a code for tokens, refreshing, and a configured httpx client (timeouts +
bounded retry/backoff on 429/5xx). A ``transport`` arg lets tests inject ``httpx.MockTransport`` so
the suite never touches the network.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlencode

import httpx

from nexus.network.connectors.base import NetworkSyncBatch, SourceAccountRef

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_RETRY_STATUS = {429, 500, 502, 503, 504}


class OAuthConnector:
    provider: str = ""
    authorize_url: str = ""
    token_url: str = ""
    scopes: list[str] = []
    extra_authorize_params: dict[str, str] = {}

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        transport: httpx.BaseTransport | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._transport = transport  # tests inject MockTransport; prod leaves None (real network)

    # ---- OAuth ----
    def authorize_url_for(self, *, state: str, code_challenge: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            **self.extra_authorize_params,
        }
        return f"{self.authorize_url}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, code_verifier: str) -> dict:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": code_verifier,
            }
        )

    async def refresh(self, refresh_token: str) -> dict:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def _token_request(self, data: dict) -> dict:
        data = {"client_id": self.client_id, "client_secret": self.client_secret, **data}
        async with self._client() as c:
            resp = await c.post(self.token_url, data=data)
            resp.raise_for_status()
            return resp.json()

    # ---- HTTP ----
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport)

    async def _get_json(
        self, client: httpx.AsyncClient, url: str, *, token: str, params: dict | None = None
    ) -> httpx.Response:
        """GET with bearer auth + up to 3 bounded retries on 429/5xx."""
        last: httpx.Response | None = None
        for attempt in range(3):
            resp = await client.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code not in _RETRY_STATUS:
                return resp
            last = resp
            await asyncio.sleep(0.5 * (2**attempt))
        return last  # type: ignore[return-value]

    # ---- contract ----
    async def fetch(self, account: SourceAccountRef, since: str | None) -> NetworkSyncBatch:
        raise NotImplementedError
```

- [ ] **Step 4: Run test to verify it (still) fails on the Google import**

Run: `py -3.10 -m pytest tests/test_google_connector.py -v`
Expected: FAIL — Google connector not yet created (Task 6). This task only lands the base it extends.

- [ ] **Step 5: Commit**

```bash
git add nexus/network/connectors/oauthbase.py
git commit -m "feat(network): OAuth connector base (authorize/exchange/refresh + httpx)"
```

---

## Task 6: Google connector (People + Calendar)

**Files:** Create `nexus/network/connectors/google.py`; Test: `tests/test_google_connector.py` (append the fetch test)

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_google_connector.py  (append)
async def test_fetch_merges_contacts_and_calendar_by_email():
    from nexus.network.connectors.base import SourceAccountRef
    from nexus.network.connectors.google import GoogleConnector

    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if "people.googleapis.com" in u:
            return httpx.Response(200, json={
                "connections": [
                    {"resourceName": "people/c1",
                     "names": [{"displayName": "Ada Okafor"}],
                     "emailAddresses": [{"value": "ada@helix.com"}],
                     "organizations": [{"name": "Helix Health", "title": "CTO"}]},
                    {"resourceName": "people/c2",
                     "names": [{"displayName": "No Email Person"}]},
                ],
                "nextSyncToken": "SYNC2",
            })
        if "calendar/v3" in u:
            return httpx.Response(200, json={
                "items": [
                    {"start": {"dateTime": "2026-06-20T10:00:00Z"},
                     "attendees": [
                         {"email": "self@me.com", "self": True},
                         {"email": "ada@helix.com", "displayName": "Ada Okafor"},
                     ]},
                ],
            })
        return httpx.Response(404)

    c = GoogleConnector(client_id="x", client_secret="y", redirect_uri="https://x/cb",
                        transport=httpx.MockTransport(handler))
    ref = SourceAccountRef(id="a", provider="google", external_account_id="self@me.com",
                           oauth={"access_token": "AT"})
    batch = await c.fetch(ref, None)

    # Ada appears once (contact + calendar merged by email), with a meeting touchpoint.
    ada = next(i for i in batch.identities if i.email == "ada@helix.com")
    assert ada.name == "Ada Okafor" and ada.title == "CTO" and ada.company == "Helix Health"
    assert {i.email for i in batch.identities} == {"ada@helix.com", None} or \
           {i.name for i in batch.identities} == {"Ada Okafor", "No Email Person"}
    meets = [t for t in batch.touchpoints if t.person_external_id == ada.external_id]
    assert len(meets) == 1 and meets[0].kind == "meeting"
    assert batch.next_cursor == "SYNC2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_google_connector.py::test_fetch_merges_contacts_and_calendar_by_email -v`
Expected: FAIL — `ModuleNotFoundError: nexus.network.connectors.google`

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/connectors/google.py
"""Real Google connector: People API contacts + Calendar meetings → graph identities/touchpoints.

Scopes (both Google "sensitive", not Gmail-grade restricted): contacts.readonly + calendar.readonly
+ openid/email (for the connected account label). Within one fetch, contacts and calendar attendees
are merged by email so each person yields ONE RawIdentity (with its meeting touchpoints), keeping the
ingest model 'one identity per person per source'.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, SourceAccountRef, Touchpoint
from nexus.network.connectors.oauthbase import OAuthConnector

_PEOPLE = "https://people.googleapis.com/v1/people/me/connections"
_CALENDAR = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_PERSON_FIELDS = "names,emailAddresses,organizations,metadata"


class GoogleConnector(OAuthConnector):
    provider = "google"
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    scopes = [
        "openid",
        "email",
        "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
    ]
    extra_authorize_params = {
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }

    async def fetch(self, account: SourceAccountRef, since: str | None) -> NetworkSyncBatch:
        token = account.oauth.get("access_token", "")
        self_email = (account.external_account_id or "").lower()
        # person keyed by lowercased email (or resourceName when no email) → one RawIdentity each.
        people: dict[str, dict] = {}
        touchpoints: list[Touchpoint] = []

        def upsert(key: str, **fields) -> str:
            cur = people.setdefault(key, {"external_id": key})
            for k, v in fields.items():
                if v and not cur.get(k):
                    cur[k] = v
            return cur["external_id"]

        async with self._client() as c:
            next_cursor = await self._load_contacts(c, token, since, upsert)
            await self._load_calendar(c, token, self_email, upsert, touchpoints)

        identities = [
            RawIdentity(
                external_id=p["external_id"], email=p.get("email"), name=p.get("name"),
                title=p.get("title"), company=p.get("company"),
                relation=p.get("relation", "contact"),
            )
            for p in people.values()
        ]
        return NetworkSyncBatch(identities=identities, touchpoints=touchpoints,
                                next_cursor=next_cursor)

    async def _load_contacts(self, c, token, since, upsert) -> str | None:
        next_cursor: str | None = None
        page: str | None = None
        while True:
            params = {"personFields": _PERSON_FIELDS, "pageSize": 200}
            if since:
                params["syncToken"] = since
            if page:
                params["pageToken"] = page
            resp = await self._get_json(c, _PEOPLE, token=token, params=params)
            # An expired syncToken (HTTP 400) → fall back to a full resync once.
            if resp.status_code == 400 and since:
                return await self._load_contacts(c, token, None, upsert)
            resp.raise_for_status()
            data = resp.json()
            for person in data.get("connections", []):
                email = _first(person.get("emailAddresses"), "value")
                key = (email or "").lower() or person.get("resourceName", "")
                org = (person.get("organizations") or [{}])[0]
                upsert(
                    key, email=(email.lower() if email else None),
                    name=_first(person.get("names"), "displayName"),
                    company=org.get("name"), title=org.get("title"), relation="contact",
                )
            page = data.get("nextPageToken")
            if not page:
                next_cursor = data.get("nextSyncToken")
                break
        return next_cursor

    async def _load_calendar(self, c, token, self_email, upsert, touchpoints) -> None:
        time_min = (datetime.now(timezone.utc) - timedelta(days=365)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        params = {"timeMin": time_min, "singleEvents": "true", "maxResults": 250,
                  "orderBy": "startTime"}
        resp = await self._get_json(c, _CALENDAR, token=token, params=params)
        if resp.status_code >= 400:
            return  # calendar is best-effort; contacts already loaded
        for ev in resp.json().get("items", []):
            start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date")
            at = _parse_dt(start)
            for att in ev.get("attendees") or []:
                if att.get("self") or att.get("resource"):
                    continue
                email = (att.get("email") or "").lower()
                if not email or email == self_email:
                    continue
                ext = upsert(email, email=email, name=att.get("displayName"), relation="calendar")
                if at is not None:
                    touchpoints.append(
                        Touchpoint(person_external_id=ext, kind="meeting", at=at)
                    )


def _first(items, key):
    if items and isinstance(items, list):
        return (items[0] or {}).get(key)
    return None


def _parse_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_google_connector.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/network/connectors/google.py tests/test_google_connector.py
git commit -m "feat(network): real Google connector (People + Calendar)"
```

---

## Task 7: Microsoft connector (Graph)

**Files:** Create `nexus/network/connectors/microsoft.py`; Test: `tests/test_microsoft_connector.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_microsoft_connector.py
from __future__ import annotations

import httpx


def test_authorize_url_targets_graph_scopes():
    from nexus.network.connectors.microsoft import MicrosoftConnector

    c = MicrosoftConnector(client_id="cid", client_secret="s", redirect_uri="https://x/cb",
                           tenant="common")
    url = c.authorize_url_for(state="ST", code_challenge="CH")
    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    assert "Contacts.Read" in url and "Calendars.Read" in url and "offline_access" in url


async def test_fetch_maps_contacts_and_events():
    from nexus.network.connectors.base import SourceAccountRef
    from nexus.network.connectors.microsoft import MicrosoftConnector

    def handler(request: httpx.Request) -> httpx.Response:
        u = str(request.url)
        if u.endswith("/me/contacts") or "/me/contacts?" in u:
            return httpx.Response(200, json={"value": [
                {"id": "ct1", "displayName": "Ada Okafor",
                 "emailAddresses": [{"address": "ada@helix.com"}],
                 "companyName": "Helix Health", "jobTitle": "CTO"},
            ]})
        if "/me/events" in u or "/me/calendarView" in u:
            return httpx.Response(200, json={"value": [
                {"start": {"dateTime": "2026-06-20T10:00:00.000000", "timeZone": "UTC"},
                 "attendees": [
                     {"emailAddress": {"address": "ada@helix.com", "name": "Ada Okafor"}},
                 ]},
            ]})
        return httpx.Response(404)

    c = MicrosoftConnector(client_id="x", client_secret="y", redirect_uri="https://x/cb",
                           tenant="common", transport=httpx.MockTransport(handler))
    ref = SourceAccountRef(id="a", provider="microsoft", external_account_id="self@me.com",
                           oauth={"access_token": "AT"})
    batch = await c.fetch(ref, None)
    ada = next(i for i in batch.identities if i.email == "ada@helix.com")
    assert ada.title == "CTO" and ada.company == "Helix Health"
    assert any(t.kind == "meeting" and t.person_external_id == ada.external_id
               for t in batch.touchpoints)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_microsoft_connector.py -v`
Expected: FAIL — `ModuleNotFoundError: nexus.network.connectors.microsoft`

- [ ] **Step 3: Write minimal implementation**

```python
# nexus/network/connectors/microsoft.py
"""Real Microsoft connector (Graph): Outlook contacts + calendar events → identities/touchpoints.

Mirrors the Google connector against Microsoft Graph. Scopes: Contacts.Read + Calendars.Read +
offline_access + openid/email. Contacts and event attendees are merged by email within one fetch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, SourceAccountRef, Touchpoint
from nexus.network.connectors.oauthbase import OAuthConnector

_CONTACTS = "https://graph.microsoft.com/v1.0/me/contacts"
_EVENTS = "https://graph.microsoft.com/v1.0/me/events"


class MicrosoftConnector(OAuthConnector):
    provider = "microsoft"
    scopes = [
        "openid",
        "email",
        "offline_access",
        "https://graph.microsoft.com/Contacts.Read",
        "https://graph.microsoft.com/Calendars.Read",
    ]

    def __init__(self, *, tenant: str = "common", **kw):
        super().__init__(**kw)
        self.tenant = tenant or "common"
        self.authorize_url = (
            f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/authorize"
        )
        self.token_url = f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"

    async def fetch(self, account: SourceAccountRef, since: str | None) -> NetworkSyncBatch:
        token = account.oauth.get("access_token", "")
        self_email = (account.external_account_id or "").lower()
        people: dict[str, dict] = {}
        touchpoints: list[Touchpoint] = []

        def upsert(key: str, **fields) -> str:
            cur = people.setdefault(key, {"external_id": key})
            for k, v in fields.items():
                if v and not cur.get(k):
                    cur[k] = v
            return cur["external_id"]

        async with self._client() as c:
            resp = await self._get_json(
                c, _CONTACTS, token=token,
                params={"$select": "displayName,emailAddresses,companyName,jobTitle", "$top": 200},
            )
            if resp.status_code < 400:
                for ct in resp.json().get("value", []):
                    email = _first_email(ct.get("emailAddresses"))
                    key = (email or "").lower() or ct.get("id", "")
                    upsert(key, email=(email.lower() if email else None),
                           name=ct.get("displayName"), company=ct.get("companyName"),
                           title=ct.get("jobTitle"), relation="contact")

            time_min = (datetime.now(timezone.utc) - timedelta(days=365)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            ev_resp = await self._get_json(
                c, _EVENTS, token=token,
                params={"$select": "start,attendees", "$top": 250,
                        "$filter": f"start/dateTime ge '{time_min}'"},
            )
            if ev_resp.status_code < 400:
                for ev in ev_resp.json().get("value", []):
                    at = _parse_dt((ev.get("start") or {}).get("dateTime"))
                    for att in ev.get("attendees") or []:
                        email = ((att.get("emailAddress") or {}).get("address") or "").lower()
                        if not email or email == self_email:
                            continue
                        name = (att.get("emailAddress") or {}).get("name")
                        ext = upsert(email, email=email, name=name, relation="calendar")
                        if at is not None:
                            touchpoints.append(
                                Touchpoint(person_external_id=ext, kind="meeting", at=at)
                            )

        identities = [
            RawIdentity(
                external_id=p["external_id"], email=p.get("email"), name=p.get("name"),
                title=p.get("title"), company=p.get("company"),
                relation=p.get("relation", "contact"),
            )
            for p in people.values()
        ]
        return NetworkSyncBatch(identities=identities, touchpoints=touchpoints, next_cursor=None)


def _first_email(items):
    if items and isinstance(items, list):
        return (items[0] or {}).get("address")
    return None


def _parse_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "").split(".")[0] + "+00:00")
    except ValueError:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_microsoft_connector.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/network/connectors/microsoft.py tests/test_microsoft_connector.py
git commit -m "feat(network): real Microsoft Graph connector"
```

---

## Task 8: Registry — build real connectors from settings

**Files:** Modify `nexus/network/connectors/registry.py`; Test: `tests/test_network_ingest.py` (append)

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_network_ingest.py  (append)
def test_registry_builds_configured_providers(monkeypatch):
    from nexus.core.config import get_settings
    from nexus.network.connectors.registry import get_network_connector, provider_configured

    get_settings.cache_clear()
    monkeypatch.setenv("NEXUS_NETWORK_GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("NEXUS_NETWORK_GOOGLE_CLIENT_SECRET", "gsec")
    monkeypatch.setenv("NEXUS_NETWORK_OAUTH_REDIRECT_BASE", "https://app.example.com")
    get_settings.cache_clear()
    try:
        assert provider_configured("google") is True
        assert provider_configured("microsoft") is False
        g = get_network_connector("google")
        assert g.provider == "google" and g.client_id == "gid"
        assert g.redirect_uri == "https://app.example.com/api/network/oauth/google/callback"
        # fixture still available for the offline suite
        assert get_network_connector("fixture").provider == "fixture"
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_network_ingest.py::test_registry_builds_configured_providers -v`
Expected: FAIL — `ImportError: cannot import name 'provider_configured'`

- [ ] **Step 3: Write minimal implementation**

Replace the body of `nexus/network/connectors/registry.py` with:

```python
# nexus/network/connectors/registry.py
"""Connector lookup. Real OAuth providers (google/microsoft) are built from settings; the offline
``fixture`` stays available for the test suite. A process-wide override lets a test inject a canned
connector for the sync-job path. ``provider_configured`` reports whether a real provider has
credentials (so the API can return a clear 'not configured' instead of inventing data)."""
from __future__ import annotations

from nexus.core.config import get_settings
from nexus.network.connectors.base import NetworkConnector
from nexus.network.connectors.fixture import FixtureConnector

_override: NetworkConnector | None = None


def set_network_connector(connector: NetworkConnector | None) -> None:
    """Test seam: force every lookup to return ``connector`` (or clear with ``None``)."""
    global _override
    _override = connector


def _redirect_uri(provider: str) -> str:
    base = get_settings().network_oauth_redirect_base.rstrip("/")
    return f"{base}/api/network/oauth/{provider}/callback"


def provider_configured(provider: str) -> bool:
    s = get_settings()
    if provider == "google":
        return bool(s.network_google_client_id and s.network_google_client_secret
                    and s.network_oauth_redirect_base)
    if provider == "microsoft":
        return bool(s.network_microsoft_client_id and s.network_microsoft_client_secret
                    and s.network_oauth_redirect_base)
    if provider in ("linkedin", "fixture"):
        return True
    return False


def get_network_connector(provider: str) -> NetworkConnector:
    if _override is not None:
        return _override
    s = get_settings()
    if provider == "google":
        from nexus.network.connectors.google import GoogleConnector

        return GoogleConnector(
            client_id=s.network_google_client_id,
            client_secret=s.network_google_client_secret,
            redirect_uri=_redirect_uri("google"),
        )
    if provider == "microsoft":
        from nexus.network.connectors.microsoft import MicrosoftConnector

        return MicrosoftConnector(
            tenant=s.network_microsoft_tenant,
            client_id=s.network_microsoft_client_id,
            client_secret=s.network_microsoft_client_secret,
            redirect_uri=_redirect_uri("microsoft"),
        )
    if provider == "fixture":
        return FixtureConnector()
    raise ValueError(f"unknown network provider: {provider}")
```

Note: this changes `GoogleConnector`/`MicrosoftConnector` construction to keyword args — they already use `__init__(*, client_id, client_secret, redirect_uri, transport=None)` from Task 5, and Microsoft adds `tenant`. The earlier connector tests call them with keyword args, so they remain valid.

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_network_ingest.py tests/test_google_connector.py tests/test_microsoft_connector.py -v`
Expected: PASS (all — the registry change is compatible with the connector tests)

- [ ] **Step 5: Commit**

```bash
git add nexus/network/connectors/registry.py tests/test_network_ingest.py
git commit -m "feat(network): build real OAuth connectors from settings"
```

---

## Task 9: Sync worker — token refresh + error capture

**Files:** Modify `nexus/workers/tasks.py` (`handle_sync_network_account`); Test: `tests/test_network_ingest.py` (append)

- [ ] **Step 1: Write the failing test (append)**

```python
# tests/test_network_ingest.py  (append)
async def test_sync_refreshes_expired_token_and_records_errors():
    import time

    from nexus.models.network import NetworkSourceAccount
    from nexus.network.connectors.base import NetworkSyncBatch, RawIdentity, SourceAccountRef
    from nexus.network.connectors.oauthbase import OAuthConnector
    from nexus.network.crypto import seal_tokens
    from nexus.network.connectors.registry import set_network_connector
    from nexus.workers.tasks import handle_sync_network_account

    class FakeOAuth(OAuthConnector):
        provider = "google"
        refreshed = False

        async def refresh(self, refresh_token):
            FakeOAuth.refreshed = True
            return {"access_token": "AT-NEW", "expires_in": 3600}

        async def fetch(self, account: SourceAccountRef, since):
            assert account.oauth["access_token"] == "AT-NEW"  # used the refreshed token
            return NetworkSyncBatch(identities=[RawIdentity(external_id="g1", email="z@a.com",
                                                            name="Zed")])

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _seed_account(ts)
        acc.provider = "google"
        acc.oauth = seal_tokens({"access_token": "AT-OLD", "refresh_token": "RT",
                                 "expires_at": int(time.time()) - 10})  # expired
        await ts.flush()
        acc_id = acc.id

    set_network_connector(FakeOAuth(client_id="x", client_secret="y", redirect_uri="https://x/cb"))
    try:
        res = await handle_sync_network_account({"tenant_id": tid, "account_id": acc_id})
    finally:
        set_network_connector(None)

    assert FakeOAuth.refreshed is True
    assert res["new_persons"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_network_ingest.py::test_sync_refreshes_expired_token_and_records_errors -v`
Expected: FAIL — the current handler doesn't refresh tokens (calls `fetch` with the raw `acc.oauth`, so `account.oauth["access_token"]` is the encrypted blob, and `refresh` is never called).

- [ ] **Step 3: Write minimal implementation**

Replace `handle_sync_network_account` in `nexus/workers/tasks.py` with:

```python
async def handle_sync_network_account(payload: dict) -> dict:
    """Pull a member's network source via its connector and fold the batch into the graph.

    OAuth providers: decrypt the stored token bundle, refresh it if the access token is expired
    (persisting the rotated bundle), then fetch with a valid access token. Connector/API failures
    are captured on the account (status=error, last_error) and surfaced in the UI, not swallowed.
    Idempotent: re-running re-upserts identities/edges and advances the sync cursor.
    """
    import time

    from nexus.models.network import NetworkSourceAccount
    from nexus.network.connectors.base import SourceAccountRef
    from nexus.network.connectors.oauthbase import OAuthConnector
    from nexus.network.connectors.registry import get_network_connector
    from nexus.network.crypto import seal_tokens, unseal_tokens
    from nexus.network.service import ingest_batch

    tid = payload["tenant_id"]
    account_id = payload["account_id"]
    async with tenant_session(tid) as ts:
        acc = await ts.get(NetworkSourceAccount, account_id)
        if acc is None:
            return {"error": "account_not_found", "account_id": account_id}
        connector = get_network_connector(acc.provider)

        oauth_for_fetch: dict = {}
        if isinstance(connector, OAuthConnector):
            bundle = unseal_tokens(acc.oauth)
            if not bundle.get("access_token") and not bundle.get("refresh_token"):
                acc.status = "error"
                acc.last_error = "not connected (no token) — reconnect required"
                return {"error": "not_connected", "account_id": account_id}
            if _token_expired(bundle, now=int(time.time())) and bundle.get("refresh_token"):
                try:
                    new = await connector.refresh(bundle["refresh_token"])
                except Exception as exc:  # refresh failed → user must reconnect
                    acc.status = "error"
                    acc.last_error = f"token refresh failed: {type(exc).__name__}"
                    return {"error": "refresh_failed", "account_id": account_id}
                bundle["access_token"] = new.get("access_token", bundle.get("access_token"))
                if new.get("refresh_token"):
                    bundle["refresh_token"] = new["refresh_token"]
                if new.get("expires_in"):
                    bundle["expires_at"] = int(time.time()) + int(new["expires_in"])
                acc.oauth = seal_tokens(bundle)
            oauth_for_fetch = {"access_token": bundle.get("access_token", "")}

        ref = SourceAccountRef(
            id=acc.id, provider=acc.provider,
            external_account_id=acc.external_account_id, oauth=oauth_for_fetch,
        )
        try:
            batch = await connector.fetch(ref, acc.sync_cursor)
        except Exception as exc:
            acc.status = "error"
            acc.last_error = f"sync failed: {type(exc).__name__}: {str(exc)[:200]}"
            return {"error": "fetch_failed", "account_id": account_id}
        acc.last_error = None
        res = await ingest_batch(ts, acc, batch)
    return {"account_id": account_id, **res}


def _token_expired(bundle: dict, *, now: int, skew_s: int = 60) -> bool:
    """True when there's an expiry and it's within ``skew_s`` of now (refresh proactively)."""
    exp = bundle.get("expires_at")
    return exp is not None and int(exp) <= now + skew_s
```

(Leave the `HANDLERS` registry entry and `enqueue_sync_network_account` as they are.)

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_network_ingest.py -v`
Expected: PASS (all ingest tests, including the new refresh test and the existing fixture sync test)

- [ ] **Step 5: Commit**

```bash
git add nexus/workers/tasks.py tests/test_network_ingest.py
git commit -m "feat(network): token refresh + error capture in the sync worker"
```

---

## Task 10: API — OAuth routes, LinkedIn import, restrict POST /accounts

**Files:** Modify `nexus/network/schemas.py`, `nexus/api/routers/network.py`; Test: `tests/test_network_oauth_api.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_network_oauth_api.py
from __future__ import annotations

import httpx
import pytest

from tests.conftest import auth, client, signup


async def test_oauth_start_unconfigured_returns_400(client):
    h = auth(await signup(client, slug="oa1", email="r@oa1.com", company="OA1"))
    r = await client.get("/api/network/oauth/google/start", headers=h)
    assert r.status_code == 400
    assert "configured" in r.json()["detail"].lower()


async def test_oauth_start_configured_returns_consent_url(client, monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setenv("NEXUS_NETWORK_GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("NEXUS_NETWORK_GOOGLE_CLIENT_SECRET", "gsec")
    monkeypatch.setenv("NEXUS_NETWORK_OAUTH_REDIRECT_BASE", "https://app.example.com")
    get_settings.cache_clear()
    try:
        h = auth(await signup(client, slug="oa2", email="r@oa2.com", company="OA2"))
        r = await client.get("/api/network/oauth/google/start", headers=h)
        assert r.status_code == 200
        url = r.json()["authorize_url"]
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "state=" in url and "code_challenge=" in url
    finally:
        get_settings.cache_clear()


async def test_post_accounts_rejects_oauth_provider(client):
    h = auth(await signup(client, slug="oa3", email="r@oa3.com", company="OA3"))
    r = await client.post("/api/network/accounts",
                          json={"provider": "google", "external_account_id": "x"}, headers=h)
    assert r.status_code == 400  # OAuth providers must use the /oauth flow


async def test_linkedin_import(client):
    h = auth(await signup(client, slug="oa4", email="r@oa4.com", company="OA4"))
    acc = (await client.post("/api/network/accounts",
                             json={"provider": "linkedin", "external_account_id": "me"},
                             headers=h)).json()["id"]
    csv = (
        "Notes:\n\n"
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Ada,Okafor,https://lnkd.in/ada,ada@helix.com,Helix Health,CTO,01 Jun 2026\n"
    ).encode()
    r = await client.post(
        f"/api/network/accounts/{acc}/import-linkedin",
        files={"file": ("Connections.csv", csv, "text/csv")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["new_persons"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -3.10 -m pytest tests/test_network_oauth_api.py -v`
Expected: FAIL — routes not present (404), and `POST /accounts` still accepts `google`.

- [ ] **Step 3: Write minimal implementation**

In `nexus/network/schemas.py`, add after `SyncEnqueuedOut`:

```python
class OAuthStartOut(BaseModel):
    authorize_url: str
```

In `nexus/api/routers/network.py`:

1. Extend imports:

```python
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse
```

(keep the existing `Principal, get_tenant_session, require` import line.) Add:

```python
from nexus.core.config import get_settings
from nexus.network import oauth as network_oauth
from nexus.network.connectors.oauthbase import OAuthConnector  # noqa: F401 (isinstance gate if used)
from nexus.network.connectors.registry import (
    get_network_connector,
    provider_configured,
)
from nexus.network.crypto import seal_tokens
from nexus.network.linkedin_csv import LinkedInCsvError, parse_linkedin_csv
```

Add `OAuthStartOut` to the **existing** `from nexus.network.schemas import (...)` block (don't create a second import line).

2. Restrict `POST /accounts` — replace the body of `connect_account` so OAuth providers are rejected:

```python
@router.post("/accounts", response_model=NetworkAccountOut, status_code=status.HTTP_201_CREATED)
async def connect_account(
    body: ConnectRequest,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> NetworkAccountOut:
    # OAuth providers must go through the consent flow (/oauth/{provider}/start), not a bare create.
    if body.provider in ("google", "microsoft"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"connect {body.provider} via /network/oauth/{body.provider}/start",
        )
    member = await _member(ts, principal)
    acc = NetworkSourceAccount(
        member_id=member.id, user_id=principal.user_id, provider=body.provider,
        external_account_id=body.external_account_id,
        display_email=body.display_email or body.external_account_id,
    )
    ts.add(acc)
    await ts.flush()
    return _account_out(acc)
```

3. Add the OAuth + LinkedIn endpoints (append before the final `get_intro_paths` or at the end of the router):

```python
@router.get("/oauth/{provider}/start", response_model=OAuthStartOut)
async def oauth_start(
    provider: str,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> OAuthStartOut:
    if provider not in ("google", "microsoft"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown provider")
    if not provider_configured(provider):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{provider} is not configured")
    member = await _member(ts, principal)
    connector = get_network_connector(provider)
    verifier, challenge = network_oauth.make_pkce()
    state = network_oauth.sign_state(
        member_id=member.id, tenant_id=ts.tenant_id, provider=provider, verifier=verifier
    )
    return OAuthStartOut(authorize_url=connector.authorize_url_for(state=state,
                                                                   code_challenge=challenge))


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Provider redirect target. Validates state, exchanges the code, stores encrypted tokens, and
    bounces the browser back to the SPA. No auth dependency: the signed ``state`` is the credential."""
    base = get_settings().network_oauth_redirect_base.rstrip("/")
    if error or not code or not state:
        return RedirectResponse(f"{base}/network?error={error or 'oauth_failed'}")
    claims = network_oauth.verify_state(state)
    if not claims or claims.get("prov") != provider:
        return RedirectResponse(f"{base}/network?error=bad_state")

    connector = get_network_connector(provider)
    try:
        tokens = await connector.exchange_code(code=code, code_verifier=claims["pkce"])
    except Exception:
        return RedirectResponse(f"{base}/network?error=exchange_failed")

    import time as _time

    bundle = {
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token", ""),
        "expires_at": int(_time.time()) + int(tokens.get("expires_in", 3600)),
    }
    tid, member_id = claims["tid"], claims["mid"]
    # Bind the tenant the same way the request pipeline does (callback has no Principal).
    from nexus.workers.tasks import tenant_session as _tenant_session

    async with _tenant_session(tid) as ts:
        existing = await ts.first(
            NetworkSourceAccount,
            NetworkSourceAccount.member_id == member_id,
            NetworkSourceAccount.provider == provider,
        )
        if existing is None:
            existing = NetworkSourceAccount(
                member_id=member_id, user_id=claims.get("uid", member_id), provider=provider,
                external_account_id=f"{provider}:{member_id}",
                display_email=f"{provider.title()} account",
            )
            ts.add(existing)
        existing.oauth = seal_tokens(bundle)
        existing.status = "connected"
        existing.last_error = None
        await ts.flush()
        await enqueue_sync_network_account(tid, existing.id)
    return RedirectResponse(f"{base}/network?connected={provider}")


@router.post("/accounts/{account_id}/import-linkedin", response_model=IngestResultOut)
async def import_linkedin(
    account_id: str,
    file: UploadFile = File(...),
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_agents)),
) -> IngestResultOut:
    member = await _member(ts, principal)
    acc = await _owned_account(ts, member, account_id)
    content = await file.read()
    try:
        identities = parse_linkedin_csv(content)
    except LinkedInCsvError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    from nexus.network.connectors.base import NetworkSyncBatch
    from nexus.network.service import ingest_batch

    res = await ingest_batch(ts, acc, NetworkSyncBatch(identities=identities))
    return IngestResultOut(**res)
```

Note: `NetworkSourceAccount` and `IngestResultOut` and `enqueue_sync_network_account` are already imported in this router from Task 12 of the core plan — keep those imports. The callback's owner-side write uses the worker's `tenant_session` (it sets `app.current_tenant` for RLS), consistent with how jobs write.

- [ ] **Step 4: Run test to verify it passes**

Run: `py -3.10 -m pytest tests/test_network_oauth_api.py tests/test_network_api.py -v`
Expected: PASS (new oauth/linkedin tests + the existing network API tests still green)

- [ ] **Step 5: Commit**

```bash
git add nexus/network/schemas.py nexus/api/routers/network.py tests/test_network_oauth_api.py
git commit -m "feat(network): OAuth start/callback + LinkedIn import; gate OAuth providers"
```

---

## Task 11: Frontend — real OAuth + LinkedIn upload, remove mocks

**Files:** Modify `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts`, `frontend/src/pages/NetworkPage.tsx`

- [ ] **Step 1: Add types + API methods**

In `frontend/src/lib/types.ts`, add near the other network types:

```typescript
export interface NetworkOAuthStart {
  authorize_url: string;
}
```

In `frontend/src/lib/api.ts`, add to the `// ---- network (relationship graph) ----` section and import `NetworkOAuthStart`:

```typescript
  networkOAuthStart(provider: "google" | "microsoft", signal?: AbortSignal) {
    return this.request<NetworkOAuthStart>(`/network/oauth/${provider}/start`, { signal });
  }
  importLinkedInCsv(accountId: string, file: File, signal?: AbortSignal) {
    const form = new FormData();
    form.set("file", file);
    return this.requestForm<NetworkIngestResult>(
      `/network/accounts/${accountId}/import-linkedin`, form, signal);
  }
```

(Remove `importNetworkBatch` from `api.ts` — it backed the demo seeder and is no longer used by the UI. Keep `connectNetworkAccount` for LinkedIn; it now only accepts non-OAuth providers server-side.)

- [ ] **Step 2: Rewrite `NetworkPage.tsx` — remove the mock, wire real flows**

Replace `frontend/src/pages/NetworkPage.tsx` with the production version. Key changes from the current file:
- **Delete** `SAMPLE_IDENTITIES`, `SAMPLE_TOUCHPOINTS`, `DAYS_AGO`, and the "Import sample" button + `importSample`.
- **Providers:** Google + Microsoft connect via OAuth redirect; LinkedIn via CSV upload; **no "Demo network"**.
- On mount, read `?connected=` / `?error=` from the URL (the OAuth callback redirect) → toast + refetch + clean the query string.
- Source rows: show real `status`, `last_synced_at`, a **Sync** button, and a **Reconnect** button when `status === "error"`.

```tsx
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge, Button, Card, CardHeader, EmptyState, ErrorState, Icons, Input, Modal, Skeleton,
  Spinner, useToast,
} from "@/components/ui";
import { useApi } from "@/hooks/useApi";
import { useApiClient } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import { strengthMeta } from "@/lib/display";
import type {
  Member, NetworkAccount, NetworkIntroPath, NetworkPersonSummary, NetworkSearchHit,
} from "@/lib/types";
import styles from "./NetworkPage.module.css";

const EXAMPLE_QUERIES = [
  "CTO at a healthcare startup",
  "VP of Procurement in retail",
  "CFO at a fintech",
  "Head of Talent",
];
const RELATION_LABEL: Record<string, string> = {
  email: "email thread", calendar: "met in person", linkedin_1st: "1st-degree connection",
  follower: "follows them", contact: "in contacts",
};

export function NetworkPage() {
  const api = useApiClient();
  const toast = useToast();

  const sources = useApi<NetworkAccount[]>((s) => api.listNetworkAccounts(s), []);
  const members = useApi<Member[]>((s) => api.listMembers(s), []);
  const memberName = (id: string) =>
    members.data?.find((m) => m.membership_id === id)?.full_name ?? "a teammate";

  // OAuth callback bounce: /network?connected=google or ?error=...
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    const connected = p.get("connected");
    const error = p.get("error");
    if (connected) {
      toast.success("Account connected", `Syncing your ${connected} network now.`);
      sources.refetch();
    } else if (error) {
      toast.error("Couldn't connect", `OAuth failed (${error}). Please try again.`);
    }
    if (connected || error) {
      window.history.replaceState({}, "", "/network");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // search
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [results, setResults] = useState<NetworkSearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  async function runSearch(raw: string) {
    const term = raw.trim();
    if (!term) return;
    setQuery(term); setSubmitted(term); setSearching(true); setSearchError(null);
    try {
      setResults(await api.searchNetwork(term, 25));
    } catch (err) {
      setResults(null);
      setSearchError(err instanceof ApiError ? err.detail : "Search failed. Try again.");
    } finally {
      setSearching(false);
    }
  }

  // connect / sync
  const [connecting, setConnecting] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  async function connectOAuth(provider: "google" | "microsoft") {
    setConnecting(provider);
    try {
      const { authorize_url } = await api.networkOAuthStart(provider);
      window.location.assign(authorize_url); // leave the SPA for the consent screen
    } catch (err) {
      setConnecting(null);
      const msg = err instanceof ApiError ? err.detail : "Try again.";
      toast.error(`Can't connect ${provider}`, msg);
    }
  }

  const [linkedInAccountId, setLinkedInAccountId] = useState<string | null>(null);
  async function ensureLinkedInSource(): Promise<string> {
    const existing = (sources.data ?? []).find((a) => a.provider === "linkedin");
    if (existing) return existing.id;
    const acc = await api.connectNetworkAccount({
      provider: "linkedin", external_account_id: "linkedin-export",
      display_email: "LinkedIn (export)",
    });
    sources.refetch();
    return acc.id;
  }
  async function onLinkedInFile(file: File) {
    setConnecting("linkedin");
    try {
      const id = linkedInAccountId ?? (await ensureLinkedInSource());
      const res = await api.importLinkedInCsv(id, file);
      toast.success("LinkedIn import complete", `Added ${res.new_persons} connections.`);
      sources.refetch();
      if (submitted) runSearch(submitted);
    } catch (err) {
      toast.error("Import failed", err instanceof ApiError ? err.detail : "Check the CSV and retry.");
    } finally {
      setConnecting(null);
    }
  }

  async function togglePooling(acc: NetworkAccount) {
    const next = !acc.pooling_enabled;
    sources.setData((prev) => (prev ?? []).map((a) => (a.id === acc.id ? { ...a, pooling_enabled: next } : a)));
    try {
      await api.patchNetworkAccount(acc.id, { pooling_enabled: next });
    } catch (err) {
      sources.setData((prev) => (prev ?? []).map((a) => (a.id === acc.id ? { ...a, pooling_enabled: !next } : a)));
      toast.error("Couldn't update sharing", err instanceof ApiError ? err.detail : "Try again.");
    }
  }

  async function sync(acc: NetworkAccount) {
    setBusyId(acc.id);
    try {
      await api.syncNetworkAccount(acc.id);
      toast.success("Sync started", "We'll refresh this network in the background.");
    } catch (err) {
      toast.error("Couldn't start sync", err instanceof ApiError ? err.detail : "Try again.");
    } finally {
      setBusyId(null);
    }
  }

  // intro drawer
  const [activePerson, setActivePerson] = useState<NetworkPersonSummary | null>(null);
  const [paths, setPaths] = useState<NetworkIntroPath[] | null>(null);
  const [pathsLoading, setPathsLoading] = useState(false);
  async function openPerson(person: NetworkPersonSummary) {
    setActivePerson(person); setPaths(null); setPathsLoading(true);
    try {
      setPaths(await api.networkIntroPaths(person.id));
    } catch (err) {
      toast.error("Couldn't load intro paths", err instanceof ApiError ? err.detail : "Try again.");
      setActivePerson(null);
    } finally {
      setPathsLoading(false);
    }
  }

  const srcRows = sources.data ?? [];
  const hasSources = srcRows.length > 0;
  void linkedInAccountId; void setLinkedInAccountId;

  return (
    <div>
      <PageHeader
        title="Network"
        description="Search the people your team already knows, and find the warmest path to any buyer."
      />

      <div className={styles.searchCard}>
        <form className={styles.searchForm} onSubmit={(e) => { e.preventDefault(); runSearch(query); }}>
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Who do we know who's a CTO at a healthcare startup?"
            iconLeft={<Icons.SearchIcon />}
            aria-label="Search your network in plain language"
          />
          <Button type="submit" loading={searching} iconLeft={<Icons.SparklesIcon />}>Search</Button>
        </form>
        <div className={styles.examples}>
          <span className={styles.examplesLabel}>Try:</span>
          {EXAMPLE_QUERIES.map((q) => (
            <button key={q} type="button" className={styles.chip} onClick={() => runSearch(q)}>{q}</button>
          ))}
        </div>
      </div>

      <div className={styles.layout}>
        <section aria-label="Search results">
          <h2 className={styles.sectionTitle}>{submitted ? `Results for "${submitted}"` : "Results"}</h2>
          {searching ? (
            <div className={styles.results}>{[0, 1, 2].map((i) => <Skeleton key={i} width="100%" height={64} />)}</div>
          ) : searchError ? (
            <ErrorState title="Search failed" message={searchError} onRetry={() => submitted && runSearch(submitted)} />
          ) : !submitted ? (
            <EmptyState icon={<Icons.SearchIcon />} title="Ask in plain language"
              description="Describe who you're trying to reach. We rank the people your team already knows by match and relationship strength." />
          ) : results && results.length > 0 ? (
            <div className={styles.results}>
              {results.map((hit, i) => {
                const meta = strengthMeta(hit.best_strength / 100);
                const sub = [hit.person.title, hit.person.company].filter(Boolean).join(" · ");
                return (
                  <button key={hit.person.id} type="button"
                    className={`${styles.resultRow} ${styles.rise}`}
                    style={{ animationDelay: `${Math.min(i, 8) * 28}ms` }}
                    onClick={() => openPerson(hit.person)}
                    aria-label={`See intro paths to ${hit.person.full_name}`}>
                    <span className={styles.resultMain}>
                      <span className={styles.name}>{hit.person.full_name}</span>
                      {sub && <span className={styles.sub}>{sub}</span>}
                      {hit.person.primary_email && <span className={styles.email}>{hit.person.primary_email}</span>}
                    </span>
                    <span className={styles.resultMeta}>
                      <Badge tone={meta.tone} dot>{meta.label} {hit.best_strength}</Badge>
                      {hit.broker_member_ids.length > 0 && (
                        <span className={styles.brokers}>
                          {hit.broker_member_ids.slice(0, 2).map((id) => (
                            <span key={id} className={styles.brokerChip}><Icons.UserCheckIcon /> via {memberName(id)}</span>
                          ))}
                          {hit.broker_member_ids.length > 2 && (
                            <span className={styles.brokerMore}>+{hit.broker_member_ids.length - 2}</span>
                          )}
                        </span>
                      )}
                    </span>
                  </button>
                );
              })}
            </div>
          ) : (
            <EmptyState icon={<Icons.UsersIcon />} title="No one in your network matches yet"
              description={hasSources
                ? "Try broader terms, or connect another source so your team's graph is denser."
                : "Connect a source on the right to build your network graph."} />
          )}
        </section>

        <aside aria-label="Your network sources">
          <Card>
            <CardHeader title="Your sources" subtitle="Private by default. Share to pool with your team." />
            {sources.error && !sources.data ? (
              <ErrorState title="Couldn't load sources" message={sources.error.detail} onRetry={sources.refetch} />
            ) : sources.loading && !sources.data ? (
              <div className={styles.sourceList}>{[0, 1].map((i) => <Skeleton key={i} width="100%" height={84} />)}</div>
            ) : hasSources ? (
              <div className={styles.sourceList}>
                {srcRows.map((acc) => (
                  <div key={acc.id} className={styles.sourceRow}>
                    <div className={styles.sourceTop}>
                      <span className={styles.sourceId}>
                        <Icons.PlugIcon className={styles.sourceIcon} />
                        <span className={styles.sourceEmail} title={acc.display_email}>{acc.display_email || acc.provider}</span>
                      </span>
                      <Badge tone={acc.status === "connected" ? "success" : acc.status === "error" ? "danger" : "neutral"} dot>{acc.status}</Badge>
                    </div>
                    <div className={styles.sourceActions}>
                      <span className={styles.switchRow}>
                        <button type="button" role="switch" aria-checked={acc.pooling_enabled}
                          aria-label={`Share ${acc.display_email || acc.provider} with the team`}
                          className={styles.switch} onClick={() => togglePooling(acc)}>
                          <span className={styles.switchKnob} />
                        </button>
                        {acc.pooling_enabled ? "Shared" : "Private"}
                      </span>
                      {acc.status === "error" && acc.provider !== "linkedin" ? (
                        <Button size="sm" variant="secondary" iconLeft={<Icons.RefreshIcon />}
                          onClick={() => connectOAuth(acc.provider as "google" | "microsoft")}>Reconnect</Button>
                      ) : acc.provider !== "linkedin" ? (
                        <Button size="sm" variant="ghost" loading={busyId === acc.id}
                          iconLeft={<Icons.RefreshIcon />} onClick={() => sync(acc)}>Sync</Button>
                      ) : null}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState compact icon={<Icons.PlugIcon />} title="No sources yet"
                description="Connect Google, Microsoft, or upload your LinkedIn export to begin." />
            )}

            <div className={styles.connectRow} style={{ marginTop: "var(--space-4)" }}>
              <Button size="sm" variant="primary" loading={connecting === "google"} onClick={() => connectOAuth("google")}>Connect Google</Button>
              <Button size="sm" variant="ghost" loading={connecting === "microsoft"} onClick={() => connectOAuth("microsoft")}>Connect Microsoft</Button>
              <label className={styles.uploadBtn}>
                <input type="file" accept=".csv" hidden
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) onLinkedInFile(f); e.currentTarget.value = ""; }} />
                {connecting === "linkedin" ? "Importing…" : "Upload LinkedIn CSV"}
              </label>
            </div>
            <p className={styles.asideHint}>
              Google/Microsoft sync your contacts and meetings over OAuth. For LinkedIn, upload your
              own export (Settings → Get a copy of your data → Connections).
            </p>
          </Card>
        </aside>
      </div>

      <Modal open={!!activePerson} onClose={() => setActivePerson(null)}
        title={activePerson?.full_name ?? "Intro paths"}
        description={activePerson ? [activePerson.title, activePerson.company].filter(Boolean).join(" · ") || undefined : undefined}>
        {pathsLoading ? (
          <div className={styles.inlineLoading}><Spinner size={16} /> Finding the warmest paths…</div>
        ) : paths && paths.length > 0 ? (
          <>
            <p className={styles.drawerLead}>
              {paths.length === 1 ? "One teammate can broker this introduction."
                : `${paths.length} teammates can broker this introduction, strongest first.`}
            </p>
            <ul className={styles.pathList}>
              {paths.map((p, i) => {
                const meta = strengthMeta(p.strength / 100);
                const how = RELATION_LABEL[p.relation] ?? p.relation;
                return (
                  <li key={`${p.broker_member_id}-${i}`} className={styles.pathRow}>
                    <span className={styles.pathBroker}>
                      <span className={styles.pathName}>{memberName(p.broker_member_id)}</span>
                      <span className={styles.pathMeta}>{how}</span>
                    </span>
                    <Badge tone={meta.tone} dot>{meta.label} {p.strength}</Badge>
                  </li>
                );
              })}
            </ul>
          </>
        ) : (
          <EmptyState compact icon={<Icons.UsersIcon />} title="No visible path yet"
            description="No one on your team has a shared relationship with this person." />
        )}
      </Modal>
    </div>
  );
}

export default NetworkPage;
```

- [ ] **Step 3: Add the upload-button style**

Append to `frontend/src/pages/NetworkPage.module.css`:

```css
.uploadBtn {
  display: inline-flex;
  align-items: center;
  height: 32px;
  padding: 0 var(--space-3);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--text);
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease-out);
}
.uploadBtn:hover { background: var(--surface-3); }
.uploadBtn:focus-within { outline: 2px solid var(--ring); outline-offset: 2px; }
```

- [ ] **Step 4: Typecheck + build**

Run: `cd frontend && npm run build`
Expected: `tsc -b` passes (no unused-symbol errors — the demo constants are gone) and Vite emits the bundle. If `tsc` flags an unused import, remove it.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/types.ts frontend/src/lib/api.ts frontend/src/pages/NetworkPage.tsx frontend/src/pages/NetworkPage.module.css
git commit -m "feat(network): real OAuth connect + LinkedIn upload UI; remove demo mocks"
```

---

## Task 12: Full regression + lint

**Files:** none (verification only)

- [ ] **Step 1: Run the network suite + lint**

Run: `py -3.10 -m pytest tests/ -k network -q`
Expected: PASS (all network tests, old + new).

Run: `py -3.10 -m pytest -q`
Expected: the pre-existing suite stays green (the only known non-network failure is the stale-`dist` metrics test, an environment artifact — confirm it passes when `nexus/web/dist` is absent).

Run: `py -3.10 -m ruff check nexus/network nexus/api/routers/network.py nexus/workers/tasks.py`
Expected: no F/E9 violations (unused imports, undefined names). Fix any.

- [ ] **Step 2: Commit (only if a fix was needed)**

```bash
git add -A
git commit -m "fix(network): resolve lint/regression after real-connectors"
```

---

## Self-review

**Spec coverage:**
- Settings (§2) → Task 1. Token encryption (§4, §6) → Task 2. OAuth state+PKCE (§4, §6) → Task 3.
- LinkedIn CSV (§4) → Task 4. OAuth base (§4) → Task 5. Google (§5) → Task 6. Microsoft (§4) → Task 7.
- Registry/inert-when-unconfigured (§2, §4) → Task 8. Token refresh + error capture (§5, §6) → Task 9.
- OAuth routes + LinkedIn import + gate POST /accounts (§3, §8) → Task 10. Frontend, remove mocks (§1, §8) → Task 11.
- Production checklist (§6): encryption (T2), state+PKCE (T3), least scopes (T6/T7), refresh (T9), retry/backoff (T5), incremental sync via cursor (T6), error surfacing (T9/T11), no fake fallback (T8/T10) → covered.
- Testing (§7): crypto/state/csv/connector(MockTransport)/oauth-api tests → T2,T3,T4,T6,T7,T10. Regression → T12.
- Deferred (correctly out of scope): Gmail, live LinkedIn API, A5 profiling, stats, projection cache.

**Placeholder scan:** No `TBD`/`TODO` in implementation code. The one stray note in Task 10 (the illustrative `create_access_token` import) is explicitly flagged "do NOT import — drop it." Connector `transport=None` is a real test seam, not a placeholder.

**Type consistency:**
- `OAuthConnector.__init__(*, client_id, client_secret, redirect_uri, transport=None)` (T5); Microsoft adds `tenant` (T7); registry builds both with those exact kwargs (T8). Connector tests construct with the same kwargs.
- `seal_tokens(dict)->dict{"enc"}` / `unseal_tokens(dict|None)->dict` (T2), used in T9 (worker) and T10 (callback).
- `sign_state(*, member_id, tenant_id, provider, verifier, ttl_s=600)->str` / `verify_state(str)->dict|None` with claims `mid/tid/prov/pkce/typ` (T3), used in T10.
- `provider_configured(str)->bool` / `get_network_connector(str)` (T8) used in T10.
- `parse_linkedin_csv(bytes)->list[RawIdentity]` + `LinkedInCsvError` (T4) used in T10.
- `authorize_url_for(*, state, code_challenge)`, `exchange_code(*, code, code_verifier)`, `refresh(refresh_token)` (T5) used in T6/T7/T9/T10.
- Frontend: `networkOAuthStart(provider)->{authorize_url}`, `importLinkedInCsv(accountId, file)->NetworkIngestResult` (T11) match the T10 routes (`/oauth/{provider}/start`, `/accounts/{id}/import-linkedin`).
