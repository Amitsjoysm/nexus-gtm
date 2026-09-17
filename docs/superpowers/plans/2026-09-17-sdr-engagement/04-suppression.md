# Phase 04: Do-Not-Contact and Unsubscribe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A workspace do-not-contact list keyed by address, signed one-click unsubscribe links that satisfy RFC 8058 without letting link scanners unsubscribe people, audited lifting by managers, and a "Do not contact" badge wherever an SDR sees an address.

**Architecture:** `nexus/engagement/suppression/` holds signed tokens (`tokens.py`) and the list rules (`service.py`: idempotent block, unsubscribe permanent, lift with note and audit). A public router serves `GET`/`POST /api/u/{token}` as plain HTML (GET shows a button, POST unsubscribes). A tenant router lists, adds, checks and lifts. The frontend adds a Do-not-contact page and a badge hook used on the Contacts list. Phase 07 puts the link in every email and checks the list before every send; phase 08 stops enrollments when an address is blocked; phase 09 blocks on a clear "no".

**Tech Stack:** FastAPI, HMAC-SHA256, React + TypeScript.

**Roadmap:** [00-roadmap.md](00-roadmap.md). **Spec:** §4 (`do_not_contact`), §5 (pre-send check 1, headers), §9, D7, D11. **Depends on:** phase 01 (and phase 02 for `public_base_url`).

**Verified:** run in the CI image on top of phases 01–03: `tests/test_engagement_suppression.py`, `test_rls_binding_guard.py`, `test_plan_gated_nav.py`, `test_admin_routes_are_not_discoverable.py` (45 passed), `ruff check nexus tests`, and `npm run typecheck` (clean).

---

## Files

| Action | Path | Responsibility |
|---|---|---|
| Create | `nexus/engagement/suppression/__init__.py` | package |
| Create | `nexus/engagement/suppression/tokens.py` | signed unsubscribe tokens and URL |
| Create | `nexus/engagement/suppression/service.py` | block, lift, check, list |
| Create | `nexus/api/routers/unsubscribe.py` | public one-click endpoint |
| Create | `nexus/api/routers/engagement_suppression.py` | tenant list API |
| Modify | `nexus/api/routers/__init__.py` | register both |
| Create | `frontend/src/components/engagement/DncBadge.tsx` | badge + `useDoNotContact` |
| Create | `frontend/src/pages/engagement/DoNotContactPage.tsx` (+ `.module.css`) | the list page |
| Modify | `frontend/src/App.tsx`, `frontend/src/pages/ContactsPage.tsx`, `frontend/src/pages/engagement/MailboxesPage.tsx` (+ `.module.css`), `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts` | route, badge, link, client |
| Create | `tests/test_engagement_suppression.py` | tokens, rules, public endpoint, API, UI |

---

### Task 1: Signed unsubscribe tokens

**Files:**
- Create: `nexus/engagement/suppression/__init__.py`, `nexus/engagement/suppression/tokens.py`
- Test: `tests/test_engagement_suppression.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_engagement_suppression.py` with the module docstring, imports, `_workspace`, `_contact` and `test_a_token_identifies_its_contact_and_refuses_any_change` from the final file (Task 5 Step 1).

- [ ] **Step 2: Run to see it fail**

Run: `pytest tests/test_engagement_suppression.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.suppression'`

- [ ] **Step 3: Implement**

`nexus/engagement/suppression/__init__.py`:

```python
"""Do-not-contact list and unsubscribe links (spec §3 suppression/)."""
```

`nexus/engagement/suppression/tokens.py`:

```python
"""Signed unsubscribe links (D11, RFC 8058).

A link must keep working for as long as someone might click it in an old email, so it carries no
expiry. It carries the workspace and the contact, and an HMAC over both keyed from ``secret_key``;
nothing in it is guessable, and changing either id breaks the signature. It does NOT carry the email
address: links are logged by proxies and mail scanners, and the address is found from the contact.
"""
from __future__ import annotations

import hashlib
import hmac
import re

from nexus.core.config import get_settings

_VERSION = "v1"
_SHAPE = re.compile(r"v1\.([0-9a-f]{32})\.([0-9a-f]{32})\.([0-9a-f]{32})")


def _signature(tenant_id: str, contact_id: str) -> str:
    key = hashlib.sha256(f"engagement:unsubscribe:{get_settings().secret_key}".encode()).digest()
    message = f"{_VERSION}|{tenant_id}|{contact_id}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def make_token(tenant_id: str, contact_id: str) -> str:
    return f"{_VERSION}.{tenant_id}.{contact_id}.{_signature(tenant_id, contact_id)}"


def read_token(token: str) -> tuple[str, str] | None:
    """``(tenant_id, contact_id)`` for a genuine token, else ``None``."""
    match = _SHAPE.fullmatch((token or "").strip())
    if match is None:
        return None
    tenant_id, contact_id, signature = match.groups()
    if not hmac.compare_digest(signature, _signature(tenant_id, contact_id)):
        return None
    return tenant_id, contact_id


def unsubscribe_url(tenant_id: str, contact_id: str) -> str:
    from nexus.engagement.config import public_base_url

    return f"{public_base_url()}/api/u/{make_token(tenant_id, contact_id)}"
```

- [ ] **Step 4: Run to see it pass**

Run: `pytest tests/test_engagement_suppression.py -n0 -q`
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/suppression tests/test_engagement_suppression.py
git commit -m "feat(engagement): signed, non-expiring unsubscribe tokens"
```

---

### Task 2: The list rules

**Files:**
- Create: `nexus/engagement/suppression/service.py`
- Test: `tests/test_engagement_suppression.py`

- [ ] **Step 1: Write the failing tests**

Add `test_blocking_is_idempotent_and_an_unsubscribe_makes_a_no_permanent` and `test_a_lift_needs_a_note_and_the_address_can_be_blocked_again` from the final file.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_suppression.py -n0 -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.engagement.suppression.service'`

- [ ] **Step 3: Implement**

`nexus/engagement/suppression/service.py`:

```python
"""The do-not-contact list (D7, D11, spec §4, §9).

**Keyed by the normalised address, not by the contact.** The same person can sit under two accounts
or come back in next quarter's import; a block on one contact row would miss the others.

Four reasons, and the two that matter behave differently:

* ``unsubscribed`` — permanent. Nobody lifts it: a person who asked to stop receiving email must not
  be emailed again because a colleague disagreed (CAN-SPAM, and the one-click promise in the
  header).
* ``declined`` — a clear "no" (D7). Blocks everywhere until an SDR or manager lifts it with a note,
  which is audited, so "why did we email her again?" has an answer.
* ``bounced`` and ``manual`` — liftable the same way.

``suppress`` is idempotent: blocking an address that is already blocked returns the existing row, and
an ``unsubscribed`` request upgrades an existing ``declined`` block so it becomes permanent.

Stopping the enrollments of a newly blocked address is added in phase 08, where enrollments exist.
"""
from __future__ import annotations

from nexus.core.audit import record_audit
from nexus.core.db import utcnow
from nexus.core.tenancy import TenantSession
from nexus.models.engagement import DNC_REASONS, DoNotContact

PERMANENT = frozenset({"unsubscribed"})


class PermanentBlock(ValueError):
    """An unsubscribe cannot be lifted."""


def normalize_email(address: str | None) -> str:
    return (address or "").strip().lower()


async def active_block(ts: TenantSession, email: str) -> DoNotContact | None:
    address = normalize_email(email)
    if not address:
        return None
    return await ts.first(DoNotContact, DoNotContact.email == address,
                          DoNotContact.lifted_at.is_(None))


async def active_reasons(ts: TenantSession, emails: list[str]) -> dict[str, str]:
    """``{address: reason}`` for every blocked address among ``emails`` (one query)."""
    addresses = sorted({normalize_email(e) for e in emails if normalize_email(e)})
    if not addresses:
        return {}
    rows = await ts.list(DoNotContact, DoNotContact.email.in_(addresses),
                         DoNotContact.lifted_at.is_(None))
    return {row.email: row.reason for row in rows}


async def suppress(
    ts: TenantSession, *, email: str, reason: str, contact_id: str | None = None,
    source_message_id: str | None = None, created_by_user_id: str | None = None,
) -> DoNotContact:
    if reason not in DNC_REASONS:
        raise ValueError(f"unknown do-not-contact reason {reason!r}")
    address = normalize_email(email)
    if "@" not in address:
        raise ValueError("a do-not-contact entry needs an email address")
    existing = await active_block(ts, address)
    if existing is not None:
        if reason in PERMANENT and existing.reason not in PERMANENT:
            existing.reason = reason
            existing.source_message_id = source_message_id or existing.source_message_id
            await ts.flush()
        return existing
    block = DoNotContact(email=address, reason=reason, contact_id=contact_id,
                         source_message_id=source_message_id,
                         created_by_user_id=created_by_user_id)
    ts.add(block)
    await ts.flush()
    await record_audit(ts, "engagement.dnc.add", actor_user_id=created_by_user_id,
                       target_type="do_not_contact", target_id=block.id,
                       meta={"reason": reason})
    return block


async def lift(ts: TenantSession, block: DoNotContact, *, user_id: str, note: str) -> DoNotContact:
    if block.reason in PERMANENT:
        raise PermanentBlock("An unsubscribe is permanent and cannot be lifted.")
    if block.lifted_at is not None:
        return block
    text = (note or "").strip()
    if len(text) < 5:
        raise ValueError("Say why you are lifting this block; the note is kept with the record.")
    block.lifted_at = utcnow()
    block.lifted_by_user_id = user_id
    block.lift_note = text[:2000]
    await ts.flush()
    await record_audit(ts, "engagement.dnc.lift", actor_user_id=user_id,
                       target_type="do_not_contact", target_id=block.id,
                       meta={"reason": block.reason})
    return block


async def list_blocks(
    ts: TenantSession, *, q: str = "", include_lifted: bool = False, limit: int = 100,
    offset: int = 0,
) -> list[DoNotContact]:
    where = [] if include_lifted else [DoNotContact.lifted_at.is_(None)]
    needle = normalize_email(q)
    if needle:
        escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(DoNotContact.email.like(f"%{escaped}%", escape="\\"))
    stmt = (ts.select(DoNotContact, *where).order_by(DoNotContact.created_at.desc())
            .offset(max(0, offset)).limit(max(1, min(limit, 500))))
    return list((await ts.session.scalars(stmt)).all())
```

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_suppression.py -n0 -q`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add nexus/engagement/suppression/service.py tests/test_engagement_suppression.py
git commit -m "feat(engagement): do-not-contact rules - idempotent, unsubscribe permanent, audited lift"
```

---

### Task 3: Public one-click unsubscribe

**Files:**
- Create: `nexus/api/routers/unsubscribe.py`
- Modify: `nexus/api/routers/__init__.py`
- Test: `tests/test_engagement_suppression.py`

- [ ] **Step 1: Write the failing tests**

Add `test_a_get_shows_a_button_and_blocks_nobody`, `test_a_one_click_post_unsubscribes_permanently_and_twice_is_harmless` and `test_a_forged_token_gets_a_neutral_page` from the final file.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_suppression.py -n0 -q`
Expected: FAIL — 404 from the SPA fallback for `/api/u/...`.

- [ ] **Step 3: Implement**

`nexus/api/routers/unsubscribe.py`:

```python
# nexus/api/routers/unsubscribe.py
"""The public unsubscribe link in every engagement email (D11, RFC 8058).

``List-Unsubscribe: <https://…/api/u/{token}>`` with ``List-Unsubscribe-Post:
List-Unsubscribe=One-Click`` tells Gmail and Yahoo that a POST to the URL unsubscribes without any
further step, and the same URL is the plain link in the email's opt-out line.

**GET never unsubscribes.** Corporate mail scanners and link previewers fetch every URL in an email
before a human sees it; if a GET unsubscribed, every recipient behind such a scanner would be
unsubscribed on delivery. A GET shows a one-button page; the button POSTs.

No authentication: the signed token is the credential, as in the OAuth callback. An invalid token
gets the same neutral page as an unknown one, so the endpoint cannot be used to probe which
workspaces or contacts exist. Pages are plain server-rendered HTML with escaped text, so the flow
works with scripts disabled and without the SPA.
"""
from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

logger = logging.getLogger("nexus.api.unsubscribe")

router = APIRouter(prefix="/u", tags=["engagement-public"])

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<style>
body{{margin:0;font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f6f7f9;color:#1b1f24}}
main{{max-width:28rem;margin:12vh auto;padding:2rem;background:#fff;border:1px solid #d9dde3;border-radius:12px}}
h1{{font-size:1.25rem;margin:0 0 .75rem}}
p{{margin:0 0 1rem}}
button{{font:inherit;padding:.625rem 1.25rem;border-radius:8px;border:0;background:#1b1f24;color:#fff;cursor:pointer;min-height:44px}}
button:focus-visible{{outline:3px solid #4c7dff;outline-offset:2px}}
@media (prefers-color-scheme:dark){{body{{background:#101317;color:#e8eaed}}main{{background:#171b21;border-color:#2a3038}}button{{background:#e8eaed;color:#101317}}}}
</style></head><body><main>{body}</main></body></html>"""


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), body=body), status_code=status_code,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def _invalid() -> HTMLResponse:
    return _page("Link not valid", "<h1>This link is not valid</h1>"
                 "<p>If you want to stop receiving these emails, reply to one with the word "
                 "&ldquo;unsubscribe&rdquo;.</p>", status_code=404)


async def _resolve(token: str):
    """``(tenant name, contact email, tenant id, contact id)`` or ``None``."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.engagement.suppression.tokens import read_token
    from nexus.models.account import Contact
    from nexus.models.identity import Tenant

    parsed = read_token(token)
    if parsed is None:
        return None
    tenant_id, contact_id = parsed
    # Platform session: the request carries no tenant context, and the token's signature is the
    # proof that this workspace issued it (the webhook rule in CLAUDE.md).
    async with get_platform_sessionmaker()() as session:
        tenant = await session.get(Tenant, tenant_id)
        contact = (await session.scalars(select(Contact).where(
            Contact.id == contact_id, Contact.tenant_id == tenant_id))).first()
    if tenant is None or contact is None or not (contact.email or "").strip():
        return None
    return tenant.name, contact.email, tenant_id, contact_id


@router.get("/{token}", response_class=HTMLResponse, include_in_schema=False)
async def confirm_page(token: str) -> HTMLResponse:
    resolved = await _resolve(token)
    if resolved is None:
        return _invalid()
    workspace, _email, _tid, _cid = resolved
    return _page(
        "Unsubscribe",
        f"<h1>Stop emails from {html.escape(workspace)}?</h1>"
        "<p>You will not receive any more outreach emails from this sender.</p>"
        f'<form method="post" action="/api/u/{html.escape(token)}">'
        '<input type="hidden" name="List-Unsubscribe" value="One-Click">'
        "<button type=\"submit\">Unsubscribe</button></form>",
    )


@router.post("/{token}", response_class=HTMLResponse, include_in_schema=False)
async def unsubscribe(token: str, request: Request) -> HTMLResponse:
    from nexus.core.db import get_sessionmaker
    from nexus.core.tenancy import TenantSession, apply_rls
    from nexus.engagement.suppression.service import suppress

    resolved = await _resolve(token)
    if resolved is None:
        return _invalid()
    workspace, email, tenant_id, contact_id = resolved
    async with get_sessionmaker()() as session:
        await apply_rls(session, tenant_id)
        ts = TenantSession(session, tenant_id)
        await suppress(ts, email=email, reason="unsubscribed", contact_id=contact_id)
        await session.commit()
    logger.info("unsubscribed a contact in tenant %s via one-click link", tenant_id)
    return _page(
        "Unsubscribed",
        f"<h1>You are unsubscribed</h1><p>{html.escape(workspace)} will not email you again.</p>",
    )
```

In `nexus/api/routers/__init__.py` add `unsubscribe,` to the imports (after `signals,`) and `unsubscribe.router,` to `all_routers`.

- [ ] **Step 4: Run to see them pass, and the RLS guard**

Run: `pytest tests/test_engagement_suppression.py tests/test_rls_binding_guard.py -n0 -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add nexus/api/routers/unsubscribe.py nexus/api/routers/__init__.py tests/test_engagement_suppression.py
git commit -m "feat(engagement): RFC 8058 one-click unsubscribe; GET never unsubscribes"
```

---

### Task 4: Tenant list API

**Files:**
- Create: `nexus/api/routers/engagement_suppression.py`
- Modify: `nexus/api/routers/__init__.py`
- Test: `tests/test_engagement_suppression.py`

- [ ] **Step 1: Write the failing tests**

Add `test_members_add_and_check_and_only_managers_lift_with_a_note` and `test_an_unsubscribe_cannot_be_lifted_through_the_api` from the final file.

- [ ] **Step 2: Run to see them fail**

Run: `pytest tests/test_engagement_suppression.py -n0 -q`
Expected: FAIL — 404 on `/api/engagement/do-not-contact`.

- [ ] **Step 3: Implement**

`nexus/api/routers/engagement_suppression.py`:

```python
# nexus/api/routers/engagement_suppression.py
"""Do-not-contact list for the workspace (spec §9, D7).

Any member can see the list, add an address and check which addresses on screen are blocked,
because an SDR about to email someone needs to know. Lifting a block needs ``manage_engagement``
and a note, and an unsubscribe is never liftable (409), whoever asks.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, get_tenant_session, require
from nexus.core.rbac import Permission
from nexus.core.tenancy import TenantSession
from nexus.models.engagement import DoNotContact

router = APIRouter(prefix="/engagement/do-not-contact", tags=["engagement"])


class BlockOut(BaseModel):
    id: str
    email: str
    reason: str
    contact_id: str | None
    source_message_id: str | None
    created_by_user_id: str | None
    created_at: datetime
    lifted_at: datetime | None
    lifted_by_user_id: str | None
    lift_note: str
    liftable: bool

    @classmethod
    def of(cls, row: DoNotContact) -> "BlockOut":
        from nexus.engagement.suppression.service import PERMANENT

        return cls(
            id=row.id, email=row.email, reason=row.reason, contact_id=row.contact_id,
            source_message_id=row.source_message_id, created_by_user_id=row.created_by_user_id,
            created_at=row.created_at, lifted_at=row.lifted_at,
            lifted_by_user_id=row.lifted_by_user_id, lift_note=row.lift_note or "",
            liftable=row.lifted_at is None and row.reason not in PERMANENT,
        )


class AddIn(BaseModel):
    model_config = {"extra": "forbid"}

    email: str = Field(min_length=3, max_length=320)


class LiftIn(BaseModel):
    model_config = {"extra": "forbid"}

    note: str = Field(min_length=5, max_length=2000)


class CheckIn(BaseModel):
    model_config = {"extra": "forbid"}

    emails: list[str] = Field(default_factory=list, max_length=500)


@router.get("", response_model=list[BlockOut])
async def list_blocks(
    q: str = "",
    include_lifted: bool = False,
    limit: int = 100,
    offset: int = 0,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_engagement)),
) -> list[BlockOut]:
    from nexus.engagement.suppression.service import list_blocks as _list

    rows = await _list(ts, q=q, include_lifted=include_lifted, limit=limit, offset=offset)
    return [BlockOut.of(r) for r in rows]


@router.post("", response_model=BlockOut, status_code=status.HTTP_201_CREATED)
async def add_block(
    body: AddIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.run_engagement)),
) -> BlockOut:
    from nexus.engagement.suppression.service import suppress

    try:
        row = await suppress(ts, email=body.email, reason="manual",
                             created_by_user_id=principal.user_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return BlockOut.of(row)


@router.post("/check", response_model=dict[str, str])
async def check_blocks(
    body: CheckIn,
    ts: TenantSession = Depends(get_tenant_session),
    _: Principal = Depends(require(Permission.run_engagement)),
) -> dict[str, str]:
    """``{address: reason}`` for the blocked addresses among ``emails``; for badges on lists."""
    from nexus.engagement.suppression.service import active_reasons

    return await active_reasons(ts, body.emails)


@router.post("/{block_id}/lift", response_model=BlockOut)
async def lift_block(
    block_id: str,
    body: LiftIn,
    ts: TenantSession = Depends(get_tenant_session),
    principal: Principal = Depends(require(Permission.manage_engagement)),
) -> BlockOut:
    from nexus.engagement.suppression.service import PermanentBlock, lift

    row = await ts.get(DoNotContact, block_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Block not found")
    try:
        await lift(ts, row, user_id=principal.user_id, note=body.note)
    except PermanentBlock as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return BlockOut.of(row)
```

In `nexus/api/routers/__init__.py` add `engagement_suppression,` after `engagement_mailboxes,` and `engagement_suppression.router,` to `all_routers`.

- [ ] **Step 4: Run to see them pass**

Run: `pytest tests/test_engagement_suppression.py tests/test_admin_routes_are_not_discoverable.py -n0 -q`
Expected: all pass except the UI test (Task 5).

- [ ] **Step 5: Commit**

```bash
git add nexus/api/routers/engagement_suppression.py nexus/api/routers/__init__.py tests/test_engagement_suppression.py
git commit -m "feat(engagement): do-not-contact API - list, add, check, manager lift"
```

---

### Task 5: Do-not-contact page and contact badge

Invoke the `impeccable` skill before this task.

**Files:**
- Create: `frontend/src/components/engagement/DncBadge.tsx`, `frontend/src/pages/engagement/DoNotContactPage.tsx`, `frontend/src/pages/engagement/DoNotContactPage.module.css`
- Modify: `frontend/src/App.tsx`, `frontend/src/pages/ContactsPage.tsx`, `frontend/src/pages/engagement/MailboxesPage.tsx`, `frontend/src/pages/engagement/MailboxesPage.module.css`, `frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`
- Test: `tests/test_engagement_suppression.py` (final version)

- [ ] **Step 1: The final test file**

Replace `tests/test_engagement_suppression.py` with:

```python
"""Do-not-contact and one-click unsubscribe (D7, D11, RFC 8058)."""
from __future__ import annotations

import pathlib

import pytest

from tests.conftest import auth, principal_from_token, signup, tenant_session


async def _workspace(client, slug: str):
    token = await signup(client, slug=slug, email=f"owner@{slug}co.com", company=slug.title())
    return token, principal_from_token(token)


async def _contact(tenant_id: str, email: str = "Jane@Acme.io") -> str:
    from nexus.models.account import Account, Contact

    async with tenant_session(tenant_id) as ts:
        account = Account(name="Acme", domain="acme.io")
        ts.add(account)
        await ts.flush()
        contact = Contact(account_id=account.id, full_name="Jane Buyer", email=email)
        ts.add(contact)
        await ts.flush()
        return contact.id


# ---- tokens -------------------------------------------------------------------------------------

def test_a_token_identifies_its_contact_and_refuses_any_change():
    from nexus.engagement.suppression.tokens import make_token, read_token

    tenant, contact = "a" * 32, "b" * 32
    token = make_token(tenant, contact)
    assert read_token(token) == (tenant, contact)
    assert "@" not in token
    assert read_token(token.replace(contact, "c" * 32)) is None
    assert read_token(token[:-1] + ("0" if token[-1] != "0" else "1")) is None
    assert read_token("garbage") is None


# ---- service ------------------------------------------------------------------------------------

async def test_blocking_is_idempotent_and_an_unsubscribe_makes_a_no_permanent(client):
    from nexus.engagement.suppression.service import PermanentBlock, active_reasons, lift, suppress

    _token, me = await _workspace(client, "idem")
    async with tenant_session(me.tenant_id) as ts:
        first = await suppress(ts, email=" Jane@Acme.io ", reason="declined")
        again = await suppress(ts, email="jane@acme.io", reason="manual")
        assert again.id == first.id and again.reason == "declined"
        upgraded = await suppress(ts, email="jane@acme.io", reason="unsubscribed")
        assert upgraded.id == first.id and upgraded.reason == "unsubscribed"
        with pytest.raises(PermanentBlock):
            await lift(ts, upgraded, user_id=me.user_id, note="they asked on a call")
        assert await active_reasons(ts, ["JANE@acme.io", "other@acme.io"]) == {
            "jane@acme.io": "unsubscribed"}


async def test_a_lift_needs_a_note_and_the_address_can_be_blocked_again(client):
    from nexus.engagement.suppression.service import active_block, lift, suppress
    from nexus.models.audit import AuditLog

    _token, me = await _workspace(client, "lift")
    async with tenant_session(me.tenant_id) as ts:
        block = await suppress(ts, email="sam@acme.io", reason="bounced")
        with pytest.raises(ValueError, match="Say why"):
            await lift(ts, block, user_id=me.user_id, note="ok")
        await lift(ts, block, user_id=me.user_id, note="address was fixed by their IT")
        assert await active_block(ts, "sam@acme.io") is None
        again = await suppress(ts, email="sam@acme.io", reason="manual")
        assert again.id != block.id
        actions = {row.action for row in await ts.list(AuditLog)}
        assert {"engagement.dnc.add", "engagement.dnc.lift"} <= actions


# ---- public one-click endpoint ------------------------------------------------------------------

async def test_a_get_shows_a_button_and_blocks_nobody(client):
    from nexus.engagement.suppression.service import active_block
    from nexus.engagement.suppression.tokens import make_token

    _token, me = await _workspace(client, "getpage")
    contact_id = await _contact(me.tenant_id)
    r = await client.get(f"/api/u/{make_token(me.tenant_id, contact_id)}")
    assert r.status_code == 200
    assert "<form method=\"post\"" in r.text and "Getpage" in r.text
    assert r.headers["cache-control"] == "no-store"
    async with tenant_session(me.tenant_id) as ts:
        assert await active_block(ts, "jane@acme.io") is None


async def test_a_one_click_post_unsubscribes_permanently_and_twice_is_harmless(client):
    from nexus.engagement.suppression.service import active_block
    from nexus.engagement.suppression.tokens import make_token

    _token, me = await _workspace(client, "oneclick")
    contact_id = await _contact(me.tenant_id)
    url = f"/api/u/{make_token(me.tenant_id, contact_id)}"
    for _ in range(2):
        r = await client.post(url, content="List-Unsubscribe=One-Click",
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 200 and "You are unsubscribed" in r.text
    async with tenant_session(me.tenant_id) as ts:
        block = await active_block(ts, "jane@acme.io")
        assert block is not None and block.reason == "unsubscribed"


async def test_a_forged_token_gets_a_neutral_page(client):
    r = await client.post(f"/api/u/v1.{'a' * 32}.{'b' * 32}.{'c' * 32}")
    assert r.status_code == 404 and "not valid" in r.text


# ---- tenant API ---------------------------------------------------------------------------------

async def test_members_add_and_check_and_only_managers_lift_with_a_note(client):
    from nexus.core.security import create_access_token

    owner, me = await _workspace(client, "dncapi")
    rep = create_access_token(user_id="rep-user", tenant_id=me.tenant_id, role="rep")

    added = await client.post("/api/engagement/do-not-contact", json={"email": "Pat@Acme.io"},
                              headers=auth(rep))
    assert added.status_code == 201 and added.json()["reason"] == "manual"
    assert added.json()["liftable"] is True

    check = await client.post("/api/engagement/do-not-contact/check",
                              json={"emails": ["pat@acme.io", "nobody@acme.io"]},
                              headers=auth(rep))
    assert check.json() == {"pat@acme.io": "manual"}

    block_id = added.json()["id"]
    assert (await client.post(f"/api/engagement/do-not-contact/{block_id}/lift",
                              json={"note": "they asked us to follow up"},
                              headers=auth(rep))).status_code == 403
    lifted = await client.post(f"/api/engagement/do-not-contact/{block_id}/lift",
                               json={"note": "they asked us to follow up"}, headers=auth(owner))
    assert lifted.status_code == 200 and lifted.json()["lifted_at"]

    listed = await client.get("/api/engagement/do-not-contact", headers=auth(rep))
    assert listed.json() == []
    history = await client.get("/api/engagement/do-not-contact",
                               params={"include_lifted": True}, headers=auth(rep))
    assert [b["email"] for b in history.json()] == ["pat@acme.io"]


async def test_an_unsubscribe_cannot_be_lifted_through_the_api(client):
    from nexus.engagement.suppression.service import suppress

    owner, me = await _workspace(client, "perm")
    async with tenant_session(me.tenant_id) as ts:
        block = await suppress(ts, email="gone@acme.io", reason="unsubscribed")
        block_id = block.id
    r = await client.post(f"/api/engagement/do-not-contact/{block_id}/lift",
                          json={"note": "please let us email again"}, headers=auth(owner))
    assert r.status_code == 409


# ---- UI -----------------------------------------------------------------------------------------

SRC = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"


def test_the_list_page_and_the_contact_badge_are_wired():
    app = (SRC / "App.tsx").read_text(encoding="utf-8")
    assert 'path="/do-not-contact"' in app
    page = (SRC / "pages/engagement/DoNotContactPage.tsx").read_text(encoding="utf-8")
    assert "api.liftDoNotContact" in page and "b.liftable" in page
    assert "note.trim().length < 5" in page, "a lift must not be sendable without a note"
    contacts = (SRC / "pages/ContactsPage.tsx").read_text(encoding="utf-8")
    assert "useDoNotContact(" in contacts and "<DncBadge" in contacts
    mailboxes = (SRC / "pages/engagement/MailboxesPage.tsx").read_text(encoding="utf-8")
    assert 'to="/do-not-contact"' in mailboxes
```

Run: `pytest tests/test_engagement_suppression.py -n0 -q -k wired`
Expected: FAIL — `App.tsx` has no `/do-not-contact` route.

- [ ] **Step 2: Types and client**

Append to `frontend/src/lib/types.ts`:

```ts
/** One do-not-contact entry (D7). `liftable` is false for unsubscribes and lifted blocks. */
export interface DoNotContactEntry {
  id: string;
  email: string;
  reason: "unsubscribed" | "declined" | "bounced" | "manual";
  contact_id: string | null;
  source_message_id: string | null;
  created_by_user_id: string | null;
  created_at: string;
  lifted_at: string | null;
  lifted_by_user_id: string | null;
  lift_note: string;
  liftable: boolean;
}
```

In `frontend/src/lib/api.ts` add `DoNotContactEntry,` to the type imports after `MailboxProviderState,` and immediately before `// ---- engagement: my mailboxes ----`:

```ts
  // ---- engagement: do not contact ----
  listDoNotContact(params: { q?: string; include_lifted?: boolean } = {}, signal?: AbortSignal) {
    return this.request<DoNotContactEntry[]>("/engagement/do-not-contact", { query: params, signal });
  }
  addDoNotContact(email: string) {
    return this.request<DoNotContactEntry>("/engagement/do-not-contact", {
      method: "POST", body: { email },
    });
  }
  liftDoNotContact(id: string, note: string) {
    return this.request<DoNotContactEntry>(`/engagement/do-not-contact/${id}/lift`, {
      method: "POST", body: { note },
    });
  }
  checkDoNotContact(emails: string[], signal?: AbortSignal) {
    return this.request<Record<string, string>>("/engagement/do-not-contact/check", {
      method: "POST", body: { emails }, signal,
    });
  }
```

- [ ] **Step 3: Badge and page**

`frontend/src/components/engagement/DncBadge.tsx`:

```tsx
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui";
import { useApiClient } from "@/app/AuthContext";

const REASON_LABEL: Record<string, string> = {
  unsubscribed: "Unsubscribed",
  declined: "Said no",
  bounced: "Bounced",
  manual: "Blocked",
};

/** "Do not contact" next to an address, with why (spec §9 contact badge, D7). */
export function DncBadge({ reason }: { reason: string }) {
  return (
    <Badge tone="danger" dot title="No campaign will email this address">
      Do not contact · {REASON_LABEL[reason] ?? reason}
    </Badge>
  );
}

/**
 * `{lowercased address: reason}` for the blocked addresses among `emails`, checked in one request.
 *
 * Fails open to "nothing blocked": a badge that cannot load must not take the list down with it.
 * The server is what actually refuses to send (pre-send check 1, phase 07).
 */
export function useDoNotContact(emails: Array<string | null | undefined>): Record<string, string> {
  const api = useApiClient();
  const [blocked, setBlocked] = useState<Record<string, string>>({});
  const key = Array.from(
    new Set(emails.map((e) => (e ?? "").trim().toLowerCase()).filter(Boolean)),
  ).sort().slice(0, 500).join(",");

  useEffect(() => {
    if (!key) {
      setBlocked({});
      return;
    }
    const controller = new AbortController();
    api.checkDoNotContact(key.split(","), controller.signal)
      .then(setBlocked)
      .catch(() => {
        if (!controller.signal.aborted) setBlocked({});
      });
    return () => controller.abort();
  }, [api, key]);

  return blocked;
}
```

`frontend/src/pages/engagement/DoNotContactPage.tsx`:

```tsx
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  Field,
  Icons,
  Input,
  Modal,
  Skeleton,
  Textarea,
} from "@/components/ui";
import type { Column } from "@/components/ui";
import { DataState } from "@/components/DataState";
import { DncBadge } from "@/components/engagement/DncBadge";
import { useToast } from "@/components/ui/Toast";
import { useApi } from "@/hooks/useApi";
import { useApiClient, useAuth } from "@/app/AuthContext";
import { ApiError } from "@/lib/api";
import type { DoNotContactEntry } from "@/lib/types";
import styles from "./DoNotContactPage.module.css";

/**
 * The workspace's do-not-contact list (spec §9, D7, D11).
 *
 * Every member sees it and can add an address, because an SDR about to email someone needs to know.
 * Lifting a block is for managers and needs a note, kept with the record. An unsubscribe is
 * permanent: it shows no Lift action at all rather than one that fails.
 */
export function DoNotContactPage() {
  const api = useApiClient();
  const toast = useToast();
  const { session } = useAuth();
  const canLift = session?.role === "manager" || session?.role === "admin"
    || session?.role === "owner";
  const [query, setQuery] = useState("");
  const [includeLifted, setIncludeLifted] = useState(false);
  const [address, setAddress] = useState("");
  const [adding, setAdding] = useState(false);
  const [lifting, setLifting] = useState<DoNotContactEntry | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const blocks = useApi<DoNotContactEntry[]>(
    (signal) => api.listDoNotContact({ q: query, include_lifted: includeLifted }, signal),
    [query, includeLifted],
  );

  async function add(event: FormEvent) {
    event.preventDefault();
    setAdding(true);
    try {
      await api.addDoNotContact(address.trim());
      toast.success("Blocked", `${address.trim()} will not be emailed by any campaign.`);
      setAddress("");
      blocks.refetch();
    } catch (err) {
      toast.error("Couldn't block that address", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setAdding(false);
    }
  }

  async function lift() {
    if (!lifting) return;
    setBusy(true);
    try {
      await api.liftDoNotContact(lifting.id, note.trim());
      toast.success("Block lifted", `${lifting.email} can be contacted again.`);
      setLifting(null);
      setNote("");
      blocks.refetch();
    } catch (err) {
      toast.error("Couldn't lift the block", err instanceof ApiError ? err.detail : "Please try again.");
    } finally {
      setBusy(false);
    }
  }

  const columns: Column<DoNotContactEntry>[] = [
    { key: "email", header: "Address", sortValue: (b) => b.email,
      render: (b) => <span className={styles.mono}>{b.email}</span> },
    { key: "reason", header: "Reason", sortValue: (b) => b.reason,
      render: (b) => <DncBadge reason={b.reason} /> },
    { key: "created_at", header: "Since", hideOnMobile: true, sortValue: (b) => b.created_at,
      render: (b) => new Date(b.created_at).toLocaleDateString() },
    { key: "status", header: "Status", hideOnMobile: true, sortValue: (b) => b.lifted_at ?? "",
      render: (b) => b.lifted_at
        ? <Badge tone="neutral" title={b.lift_note}>Lifted {new Date(b.lifted_at).toLocaleDateString()}</Badge>
        : <Badge tone="danger" dot>Active</Badge> },
    { key: "actions", header: "", render: (b) => canLift && b.liftable ? (
        <Button size="sm" variant="ghost" onClick={() => setLifting(b)}>Lift</Button>
      ) : null },
  ];

  return (
    <div>
      <PageHeader
        eyebrow={<Link to="/mailboxes" className={styles.back}><Icons.ChevronLeftIcon /> My mailboxes</Link>}
        title="Do not contact"
        description="Addresses no campaign will email. Unsubscribes are permanent; a clear no, a bounce or a manual block can be lifted by a manager with a note."
      />

      <Card padding="lg" className={styles.tools}>
        <form className={styles.addForm} onSubmit={add}>
          <Field label="Block an address">
            <Input type="email" value={address} onChange={(e) => setAddress(e.target.value)}
              placeholder="name@company.com" required />
          </Field>
          <Button type="submit" loading={adding} disabled={!address.trim()}>Block</Button>
        </form>
        <div className={styles.filters}>
          <Field label="Search" hideLabel>
            <Input type="search" value={query} onChange={(e) => setQuery(e.target.value)}
              placeholder="Search addresses…" />
          </Field>
          <label className={styles.toggle}>
            <input type="checkbox" checked={includeLifted}
              onChange={(e) => setIncludeLifted(e.target.checked)} />
            Show lifted blocks
          </label>
        </div>
      </Card>

      <DataState
        state={blocks}
        errorTitle="Couldn't load the do-not-contact list"
        skeleton={<Skeleton width="100%" height={240} />}
        isEmpty={(rows) => rows.length === 0}
        empty={<EmptyState icon={<Icons.ShieldCheckIcon />} title="Nobody is blocked"
          description="Unsubscribes, clear no replies and bounces appear here automatically." />}
      >
        {(rows) => <DataTable columns={columns} rows={rows} getRowKey={(b) => b.id} caption="Do-not-contact list" />}
      </DataState>

      <Modal
        open={lifting !== null}
        onClose={() => setLifting(null)}
        title={`Lift the block on ${lifting?.email ?? ""}?`}
        description="Campaigns may email this address again. Your note is kept with the record."
        footer={
          <>
            <Button variant="ghost" onClick={() => setLifting(null)}>Cancel</Button>
            <Button variant="danger" onClick={lift} loading={busy} disabled={note.trim().length < 5}>
              Lift block
            </Button>
          </>
        }
      >
        <Field label="Why" hint="For example: they asked to hear from us again on a call.">
          <Textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </Modal>
    </div>
  );
}

export default DoNotContactPage;
```

`frontend/src/pages/engagement/DoNotContactPage.module.css`:

```css
.back {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  color: var(--text-muted);
  text-decoration: none;
  font-size: var(--text-sm);
}

.back:hover {
  color: var(--text);
}

.tools {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-5);
}

.addForm {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--space-3);
  min-width: min(100%, 24rem);
}

.addForm > :first-child {
  flex: 1 1 16rem;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
}

.toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 44px;
  font-size: var(--text-sm);
  color: var(--text);
}

.mono {
  font-family: var(--font-mono);
  overflow-wrap: anywhere;
}
```

- [ ] **Step 4: Route, link and badge**

`frontend/src/App.tsx`: after the `MailboxesPage` lazy import add

```tsx
const DoNotContactPage = lazyPage(
  () => import("@/pages/engagement/DoNotContactPage"), "DoNotContactPage",
);
```

and before the `/cadences` route:

```tsx
                <Route
                  path="/do-not-contact"
                  element={
                    <RequireCapability capability="module.outreach" name="Do not contact">
                      <DoNotContactPage />
                    </RequireCapability>
                  }
                />
```

`frontend/src/pages/engagement/MailboxesPage.tsx`: import `Link` from `react-router-dom` and replace the header `actions` with:

```tsx
        actions={
          <>
            <Link to="/do-not-contact" className={styles.link}>Do-not-contact list</Link>
            {isManager && (
              <Button variant="secondary" size="sm" onClick={() => setTeam((t) => !t)}
                aria-pressed={team}>
                {team ? "Show mine" : "Show team"}
              </Button>
            )}
          </>
        }
```

and append to `MailboxesPage.module.css`:

```css
.link {
  display: inline-flex;
  align-items: center;
  min-height: 44px;
  color: var(--accent);
  font-size: var(--text-sm);
}
```

`frontend/src/pages/ContactsPage.tsx`:
- import `{ DncBadge, useDoNotContact }` from `@/components/engagement/DncBadge`;
- after `const contacts = useApi<WorkspaceContact[]>(...)` add

```tsx
  // Blocked addresses, checked in one request for the loaded rows (spec §9 contact badge).
  const blocked = useDoNotContact((contacts.data ?? []).map((c) => c.email));
```

- in the `email_status` column, make the render start with the badge:

```tsx
        render: (c) =>
          c.email && blocked[c.email.toLowerCase()] ? (
            <DncBadge reason={blocked[c.email.toLowerCase()]} />
          ) : c.email_status ? (
```

- add `blocked` to that `useMemo` dependency list: `[busyId, callingId, similarId, phoneId, blocked]`.

- [ ] **Step 5: Tests and typecheck**

Run: `pytest tests/test_engagement_suppression.py tests/test_plan_gated_nav.py -n0 -q`
Expected: all pass.

Run: `cd frontend && npm run typecheck`
Expected: exits 0.

- [ ] **Step 6: Look at it**

Preview: add an address, see it listed; as a rep there is no Lift button; as the owner, Lift is disabled until the note has 5 characters; the Contacts list shows "Do not contact · Blocked" for that address; open `/api/u/<token>` for a contact and confirm the page renders in light and dark and that loading it blocks nobody.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/engagement frontend/src/pages/engagement frontend/src/App.tsx frontend/src/pages/ContactsPage.tsx frontend/src/lib/api.ts frontend/src/lib/types.ts tests/test_engagement_suppression.py
git commit -m "feat(engagement): do-not-contact page and contact badge"
```

---

### Task 6: Verify the phase

- [ ] **Step 1:** `ruff check nexus tests` → `All checks passed!`
- [ ] **Step 2:** `pytest -n auto -p no:cacheprovider --timeout=120 -q` → whole suite passes.
- [ ] **Step 3:** `cd frontend && npm run typecheck && npm run build` → both exit 0.

---

## Spec coverage for this phase

| Spec item | Task |
|---|---|
| §4 `do_not_contact` keyed by normalised address, one active block | 2 (schema in phase 01) |
| D7 clear no blocks everywhere until lifted; lifting logged | 2, 4 |
| D7 / D11 unsubscribe permanent | 2, 3, 4 |
| D11 one-click header target (`List-Unsubscribe-Post`), link in the opt-out line | 1, 3 (headers added in phase 07) |
| §9 Settings → Do-not-contact: address, reason, source; managers lift with a note (audited) | 4, 5 |
| §9 contact page do-not-contact badge | 5 (Contacts list; account and contact timelines in phase 11) |
| §16 unmatched mail never stored — the unsubscribe link carries no address | 1 |
