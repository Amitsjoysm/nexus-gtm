"""Outbound email via the customer's own SMTP mailbox (Gmail / Outlook / generic).

So a workspace can actually *send* its approved cadence emails from its own inbox without a paid
SEP. Uses the standard library (`smtplib` + `email.message`) — no new dependency — run off the
event loop via ``asyncio.to_thread`` so a slow SMTP server never blocks the worker. Credentials
come from the tenant's ``email_settings``; nothing is sent unless the workspace enabled it.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

logger = logging.getLogger("nexus.integrations.email_sender")

# Host/port presets so a user only needs to pick a provider + enter username + app password.
# imap_host + drafts_folder support "save as draft" (write the message to the mailbox's Drafts).
PROVIDER_PRESETS: dict[str, dict] = {
    "gmail": {"host": "smtp.gmail.com", "port": 587, "use_tls": True,
              "imap_host": "imap.gmail.com", "drafts_folder": "[Gmail]/Drafts"},
    "outlook": {"host": "smtp-mail.outlook.com", "port": 587, "use_tls": True,
                "imap_host": "outlook.office365.com", "drafts_folder": "Drafts"},
    "office365": {"host": "smtp.office365.com", "port": 587, "use_tls": True,
                  "imap_host": "outlook.office365.com", "drafts_folder": "Drafts"},
}


@dataclass(slots=True)
class SendResult:
    ok: bool
    detail: str = ""


def resolve_smtp(settings: dict | None) -> dict:
    """Merge the provider preset with explicit overrides into a concrete SMTP config.

    An override always wins over the preset, and that is what makes `provider: "smtp"` usable at
    all: it has no preset, so every field has to come from what the workspace typed.
    """
    s = dict(settings or {})
    preset = PROVIDER_PRESETS.get((s.get("provider") or "").strip().lower(), {})
    username = (s.get("username") or "").strip()
    port = int(s.get("port") or preset.get("port") or 587)
    return {
        "host": (s.get("host") or preset.get("host") or "").strip(),
        "port": port,
        # STARTTLS on a plain connection (587) vs. implicit TLS from the first byte (465) are
        # different protocols, and which one a server speaks is not derivable from the port: a
        # self-hosted box can offer implicit TLS on 8465. It defaults to `port == 465` because that
        # is the rule this module applied when the port was the only thing it could read, so every
        # mailbox stored before the field existed keeps behaving exactly as it did.
        "use_ssl": bool(s.get("use_ssl", port == 465)),
        "use_tls": bool(s.get("use_tls", preset.get("use_tls", True))),
        "username": username,
        "password": s.get("password") or "",
        "from_email": (s.get("from_email") or username).strip(),
        "from_name": (s.get("from_name") or "").strip(),
        "imap_host": (s.get("imap_host") or preset.get("imap_host") or "").strip(),
        "imap_port": int(s.get("imap_port") or preset.get("imap_port") or 993),
        "drafts_folder": (s.get("drafts_folder") or preset.get("drafts_folder") or "Drafts").strip(),
    }


# ---- multi-account model --------------------------------------------------------------
# A workspace can register several sending mailboxes (e.g. an SDR's inbox and the founder's).
# They live as ``email_settings["accounts"]`` — a list of account dicts. For backward
# compatibility, a legacy single-account ``email_settings`` (top-level provider/username/…)
# is surfaced as one implicit account so older workspaces and the existing settings API keep
# working unchanged.


def _normalize_account(a: dict, idx: int) -> dict:
    """Ensure an account dict carries the identity fields the rest of the system relies on."""
    acct = dict(a)
    acct["id"] = acct.get("id") or f"acct-{idx}"
    # from_email defaults to the username, mirroring resolve_smtp, so the list/select UIs
    # always have an address to show even when from_email was never set explicitly.
    acct["from_email"] = (acct.get("from_email") or acct.get("username") or "").strip()
    acct["label"] = acct.get("label") or acct["from_email"] or acct.get("username") or "Mailbox"
    acct["enabled"] = bool(acct.get("enabled", False))
    acct["default"] = bool(acct.get("default", False))
    return acct


def list_accounts(settings: dict | None) -> list[dict]:
    """All sending accounts for a workspace, newest model first, legacy as a fallback.

    Returns an empty list when nothing is configured. Never raises.
    """
    s = dict(settings or {})
    accts = s.get("accounts")
    if isinstance(accts, list) and accts:
        return [_normalize_account(a, i) for i, a in enumerate(accts)]
    # Legacy single-account fallback: only when real fields are present.
    if any(s.get(k) for k in ("username", "host", "from_email")):
        legacy = _normalize_account(
            {**s, "id": s.get("id") or "default", "label": s.get("label") or "Default",
             "enabled": bool(s.get("enabled", False)), "default": True},
            0,
        )
        legacy.pop("accounts", None)
        return [legacy]
    return []


def account_is_configured(account: dict | None) -> bool:
    """True when a single account is enabled and has the credentials to actually send."""
    if not account or not account.get("enabled"):
        return False
    cfg = resolve_smtp(account)
    return bool(cfg["host"] and cfg["from_email"] and cfg["username"] and cfg["password"])


def resolve_account(settings: dict | None, account_id: str | None = None) -> dict | None:
    """Pick the account to send from: the requested id, else the default, else the first."""
    accts = list_accounts(settings)
    if not accts:
        return None
    if account_id:
        match = next((a for a in accts if a.get("id") == account_id), None)
        if match is not None:
            return match
    return next((a for a in accts if a.get("default")), accts[0])


def is_configured(settings: dict | None) -> bool:
    """True when the workspace has at least one enabled, fully-credentialed sending account."""
    return any(account_is_configured(a) for a in list_accounts(settings))


def _build_message(
    cfg: dict, to: str, subject: str, body: str, html: str | None = None
) -> EmailMessage:
    """Build the message. With ``html`` it becomes multipart/alternative.

    The plain-text part is ALWAYS set first and is never optional: corporate mail gateways strip
    HTML, some clients render text only, and screen readers do better with it. `add_alternative`
    appends, so text-then-HTML is also the order the standard requires — a client picks the LAST
    part it can render, and reversing them would show plain text to everyone.
    """
    msg = EmailMessage()
    from_addr = cfg["from_email"]
    msg["From"] = f'{cfg["from_name"]} <{from_addr}>' if cfg["from_name"] else from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body or "")
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def _send_blocking(cfg: dict, to: str, subject: str, body: str, html: str | None = None) -> None:
    msg = _build_message(cfg, to, subject, body, html)
    if cfg["use_ssl"]:  # implicit TLS, conventionally port 465
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=context, timeout=30) as srv:
            if cfg["username"]:
                srv.login(cfg["username"], cfg["password"])
            srv.send_message(msg)
        return
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as srv:  # STARTTLS (587)
        srv.ehlo()
        if cfg["use_tls"]:
            srv.starttls(context=ssl.create_default_context())
            srv.ehlo()
        if cfg["username"]:
            srv.login(cfg["username"], cfg["password"])
        srv.send_message(msg)


async def send_email(
    settings: dict | None, *, to: str, subject: str, body: str, html: str | None = None
) -> SendResult:
    """Send one email through the workspace's SMTP. Never raises — returns a SendResult."""
    cfg = resolve_smtp(settings)
    if not cfg["host"] or not cfg["from_email"] or not to:
        return SendResult(False, "smtp not configured")
    try:
        await asyncio.to_thread(_send_blocking, cfg, to, subject, body, html)
        return SendResult(True, "sent")
    except Exception as exc:  # auth / connection / recipient — surface, don't crash the worker
        logger.warning("SMTP send to %s via %s failed: %r", to, cfg["host"], exc)
        return SendResult(False, f"smtp error: {exc}")


def _verify_blocking(cfg: dict) -> tuple[bool, str]:
    """Open the SMTP session and log in. Sends nothing.

    "Can this mailbox send?" and "did this message arrive?" are different questions, and only the
    first can be answered without involving somebody's inbox. Returns the server's own words on
    failure: "535 authentication failed" and "connection refused" send an operator to different
    places, and a generic "not connected" sends them nowhere.
    """
    try:
        if cfg["use_ssl"]:  # implicit TLS, conventionally port 465
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=context, timeout=20) as srv:
                srv.login(cfg["username"], cfg["password"])
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=20) as srv:
                srv.ehlo()
                if cfg["use_tls"]:
                    srv.starttls(context=ssl.create_default_context())
                    srv.ehlo()
                srv.login(cfg["username"], cfg["password"])
    except smtplib.SMTPAuthenticationError as exc:
        detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc)
        return False, f"{exc.smtp_code} {detail}".strip()
    except Exception as exc:  # noqa: BLE001 - network, DNS, TLS: the reason is what matters
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"Signed in to {cfg['host']} as {cfg['username']}."


async def verify_smtp(account: dict | None) -> tuple[bool, str]:
    """Whether this mailbox's credentials work right now. Never raises, never sends."""
    cfg = resolve_smtp(account)
    missing = [
        name for name, value in (
            ("host", cfg["host"]), ("username", cfg["username"]), ("password", cfg["password"]),
        ) if not value
    ]
    if missing:
        # Naming the field is the difference between a fix and a guess; "password" is by far the
        # most common one, because it is the only field a rep cannot see after saving.
        return False, f"This mailbox is missing its {', '.join(missing)}."
    return await asyncio.to_thread(_verify_blocking, cfg)


def has_drafts_support(settings: dict | None) -> bool:
    """True when the mailbox can save drafts via IMAP (host + credentials present)."""
    cfg = resolve_smtp(settings)
    return bool(cfg["imap_host"] and cfg["username"] and cfg["password"])


def _save_draft_blocking(cfg: dict, to: str, subject: str, body: str) -> None:
    import imaplib
    import time

    msg = _build_message(cfg, to, subject, body)
    context = ssl.create_default_context()
    imap = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], ssl_context=context)
    try:
        imap.login(cfg["username"], cfg["password"])
        imap.append(
            cfg["drafts_folder"], r"(\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes()
        )
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


async def save_to_drafts(settings: dict | None, *, to: str, subject: str, body: str) -> SendResult:
    """Write the message to the mailbox's Drafts folder via IMAP, for the user to review and send
    by hand. Never raises — returns a SendResult."""
    cfg = resolve_smtp(settings)
    if not cfg["imap_host"] or not cfg["username"] or not cfg["password"]:
        return SendResult(False, "imap not configured for drafts")
    try:
        await asyncio.to_thread(_save_draft_blocking, cfg, to or cfg["from_email"], subject, body)
        return SendResult(True, "saved to drafts")
    except Exception as exc:
        logger.warning("IMAP draft save via %s failed: %r", cfg["imap_host"], exc)
        return SendResult(False, f"imap error: {exc}")
