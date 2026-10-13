# nexus/integrations/platform_mail.py
"""The email the platform sends itself, from two sending servers (2026-10-13).

* **account**: signup verification codes, password resets and sign-in (MFA) codes.
* **alerts**: alert notifications. Its own server, decided with the product owner, so a noisy alert
  stream can never get the address that sends password resets rate-limited or blocklisted.

Invoices are not here: Stripe emails its own invoices and receipts.

Reported from staging as "signup OTPs are not being sent". Three things made that possible:

* The account mailbox could only be one of three presets (Gmail, Outlook, Office 365): there was no
  host or port setting, so any other mail server (Zoho, SendGrid, Brevo, SES, a company's own)
  resolved to an empty host and `send_email` answered "smtp not configured" without connecting.
* The configuration was copied into three senders (registration, password reset, MFA). Now there is
  one, `mailbox()`.
* Registration ignored the answer and told the visitor a code was on its way. It now says it could
  not be sent (`auth/registration.py`).

Editable in Superadmin on the screens that already existed: the servers in Configuration
(Platform email group, runtime settings, so their validators and 30s reach apply) and each password
in Provider keys (`system_smtp`, `alert_smtp`, sealed, never returned), whose Test signs in and
sends nothing. Everything falls back to the environment, so a deployment that never opens either
screen sends exactly as before.
"""
from __future__ import annotations

import logging
from nexus.integrations.email_sender import SendResult

logger = logging.getLogger("nexus.integrations.platform_mail")

ACCOUNT = "account"
ALERTS = "alerts"


#: The provider-keys id holding each mailbox's sealed password.
PASSWORD_KEY_IDS = {ACCOUNT: "system_smtp", ALERTS: "alert_smtp"}


def _fields(purpose: str) -> dict:
    """The settings each mailbox is made of. Alert email has no provider: its host is typed."""
    from nexus.core.config import get_settings

    s = get_settings()
    if purpose == ACCOUNT:
        return {"provider": s.system_smtp_provider, "host": s.system_smtp_host,
                "port": s.system_smtp_port, "username": s.system_smtp_username,
                "from_email": s.system_smtp_from, "from_name": s.system_smtp_from_name,
                "password": s.system_smtp_password}
    if purpose == ALERTS:
        return {"provider": "", "host": s.alert_smtp_host, "port": s.alert_smtp_port,
                "username": s.alert_smtp_username, "from_email": s.alert_email_sender,
                "from_name": "", "password": s.alert_smtp_password}
    raise ValueError(f"unknown mailbox {purpose!r}")


def clean_password(provider: str, password: str) -> str:
    """An app password as Google displays it, ``abcd efgh ijkl mnop``, is sixteen letters: the
    spaces are only grouping, and pasted with them the sign-in fails. Google's app passwords never
    contain a space, so for Gmail every space goes; any other server keeps the password exactly,
    because a space can be a real character there. Surrounding whitespace always goes."""
    password = (password or "").strip()
    if (provider or "").strip().lower() == "gmail":
        password = "".join(password.split())
    return password


def explain(detail: str) -> str:
    """The server's refusal, with the fix for the two that are about app passwords and SMTP sign-in.

    Both providers answer a 2-step-verified account's real password with a code that says nothing
    about what to do: Google's 534 5.7.9 and 535 5.7.8, Microsoft's 535 5.7.139 and 5.7.3."""
    text = detail or ""
    low = text.lower()
    if "5.7.9" in low or "application-specific password" in low:
        return (f"{text} Google wants an app password here, not the account password: Google "
                "Account > Security > 2-Step Verification > App passwords.")
    if "5.7.8" in low or "username and password not accepted" in low:
        return (f"{text} Google refused the username or password. With 2-Step Verification on, "
                "it takes an app password (Google Account > Security > App passwords).")
    if "5.7.139" in low or "smtpclientauthentication is disabled" in low:
        return (f"{text} Microsoft 365 has SMTP sign-in switched off for this mailbox: turn on "
                "Authenticated SMTP for it in the Microsoft 365 admin center (Users > Active users "
                "> the mailbox > Mail > Manage email apps).")
    if "5.7.3" in low and "authentication unsuccessful" in low:
        return (f"{text} Microsoft refused the sign-in. With multi-factor sign-in on, use an app "
                "password, and Authenticated SMTP must be on for the mailbox.")
    return text


async def password_source(purpose: str) -> tuple[str, str]:
    """``(password, source)``: the pinned panel key first, else the environment. Never raises: an
    unreadable key store reads as the environment, which is what sent before this existed."""
    try:
        from nexus.providers.resolver import managed_pool

        pool = await managed_pool(PASSWORD_KEY_IDS[purpose])
        if pool:
            return pool[0], "panel"
    except Exception:
        logger.warning("could not read the %s mail password from the panel", purpose, exc_info=True)
    env = str(_fields(purpose)["password"] or "")
    return env, ("env" if env else "")


async def mailbox(purpose: str, *, password: str | None = None) -> dict:
    """The raw mailbox for `email_sender.resolve_smtp`: provider, host, port, sign-in, sender.

    ``password`` replaces the stored one, for checking a password before it is saved."""
    f = _fields(purpose)
    provider = str(f["provider"] or "").strip().lower()
    port = int(f["port"] or 0)
    username = str(f["username"] or "").strip()
    raw_password = password if password is not None else (await password_source(purpose))[0]
    out = {
        "provider": "" if provider == "smtp" else provider,
        "host": str(f["host"] or "").strip(),
        "username": username,
        # Cleaned on the way out too, so a spaced app password set in the environment works.
        "password": clean_password(provider, raw_password),
        "from_email": str(f["from_email"] or "").strip() or username,
        "from_name": str(f["from_name"] or "").strip(),
    }
    if port:
        out["port"] = port
    return out


def problem(raw: dict) -> str:
    """Why this mailbox cannot send, in a sentence an operator can act on, or "" when it can."""
    from nexus.integrations.email_sender import resolve_smtp

    cfg = resolve_smtp(raw)
    if not cfg["host"]:
        return "No mail server is set: choose a provider, or Other SMTP with its host."
    if not cfg["from_email"]:
        return "No sign-in username or From address is set."
    # No username is a relay that takes mail without signing in, which alert email has always
    # allowed; a username without a password is a form someone has not finished.
    if cfg["username"] and not cfg["password"]:
        return "No password is set."
    return ""


async def send(purpose: str, *, to: str, subject: str, body: str,
               html: str | None = None) -> SendResult:
    """Send one platform email. Never raises; a mailbox that cannot send says why."""
    from nexus.integrations.email_sender import send_email

    raw = await mailbox(purpose)
    why = problem(raw)
    if why:
        logger.warning("%s email to %s not sent: %s", purpose, to, why)
        return SendResult(False, why)
    result = await send_email(raw, to=to, subject=subject, body=body, html=html)
    if not result.ok:
        result = SendResult(False, explain(result.detail))
        logger.warning("%s email to %s not sent: %s", purpose, to, result.detail)
    return result


async def check(purpose: str, *, password: str | None = None) -> tuple[bool, str]:
    """Sign in to the mail server and send nothing. ``(ok, the server's own words)``."""
    from nexus.integrations.email_sender import resolve_smtp, verify_smtp

    raw = await mailbox(purpose, password=password)
    why = problem(raw)
    if why:
        return False, why
    cfg = resolve_smtp(raw)
    host_problem = host_refusal(cfg["host"])
    if host_problem:
        return False, host_problem
    if not cfg["username"]:
        return True, (f"No sign-in is set, so {cfg['host']} is used as a relay that takes mail "
                      "without one. Send a test email to prove it delivers.")
    ok, detail = await verify_smtp(raw)
    return ok, (detail if ok else explain(detail))


def host_refusal(host: str) -> str:
    """A mail host the app must not be pointed at: private, loopback, link-local or a cloud
    metadata name, outside a local stack. Check sign-in connects there and reports how it answered,
    which from a web form would otherwise be a port scanner."""
    from nexus.core.config import get_settings
    from nexus.sources.safety import _is_blocked_host

    if not host:
        return ""
    local = get_settings().env in ("local", "test")
    blocked, why = _is_blocked_host(host, allow_private=local)
    return f"refusing this mail host: {why}" if blocked else ""
