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
