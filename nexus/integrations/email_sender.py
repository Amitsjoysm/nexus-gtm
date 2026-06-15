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
PROVIDER_PRESETS: dict[str, dict] = {
    "gmail": {"host": "smtp.gmail.com", "port": 587, "use_tls": True},
    "outlook": {"host": "smtp-mail.outlook.com", "port": 587, "use_tls": True},
    "office365": {"host": "smtp.office365.com", "port": 587, "use_tls": True},
}


@dataclass(slots=True)
class SendResult:
    ok: bool
    detail: str = ""


def resolve_smtp(settings: dict | None) -> dict:
    """Merge the provider preset with explicit overrides into a concrete SMTP config."""
    s = dict(settings or {})
    preset = PROVIDER_PRESETS.get((s.get("provider") or "").strip().lower(), {})
    username = (s.get("username") or "").strip()
    return {
        "host": (s.get("host") or preset.get("host") or "").strip(),
        "port": int(s.get("port") or preset.get("port") or 587),
        "use_tls": bool(s.get("use_tls", preset.get("use_tls", True))),
        "username": username,
        "password": s.get("password") or "",
        "from_email": (s.get("from_email") or username).strip(),
        "from_name": (s.get("from_name") or "").strip(),
    }


def is_configured(settings: dict | None) -> bool:
    """True when the workspace has enabled SMTP and provided the essentials to send."""
    if not settings or not settings.get("enabled"):
        return False
    cfg = resolve_smtp(settings)
    return bool(cfg["host"] and cfg["from_email"] and cfg["username"] and cfg["password"])


def _build_message(cfg: dict, to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    from_addr = cfg["from_email"]
    msg["From"] = f'{cfg["from_name"]} <{from_addr}>' if cfg["from_name"] else from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body or "")
    return msg


def _send_blocking(cfg: dict, to: str, subject: str, body: str) -> None:
    msg = _build_message(cfg, to, subject, body)
    if cfg["port"] == 465:  # implicit TLS
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


async def send_email(settings: dict | None, *, to: str, subject: str, body: str) -> SendResult:
    """Send one email through the workspace's SMTP. Never raises — returns a SendResult."""
    cfg = resolve_smtp(settings)
    if not cfg["host"] or not cfg["from_email"] or not to:
        return SendResult(False, "smtp not configured")
    try:
        await asyncio.to_thread(_send_blocking, cfg, to, subject, body)
        return SendResult(True, "sent")
    except Exception as exc:  # auth / connection / recipient — surface, don't crash the worker
        logger.warning("SMTP send to %s via %s failed: %r", to, cfg["host"], exc)
        return SendResult(False, f"smtp error: {exc}")
