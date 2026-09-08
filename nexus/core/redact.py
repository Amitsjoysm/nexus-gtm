# nexus/core/redact.py
"""Strip credentials out of text that is about to be stored, logged, or shown.

**A URL can BE the credential.** A Slack or Teams incoming-webhook URL is a bearer token wearing a
scheme: anyone holding it can post into that channel, which is why `nexus/alerts/connections.py`
seals it with the same envelope as a CRM token and keeps it out of every response model.

That sealing is undone the moment delivery fails. Measured 2026-09-08:

    HTTPStatusError: Client error '404 Not Found' for url
    'https://hooks.slack.com/services/T00000/B11111/SUPERSECRETTOKEN'

httpx puts the full URL into `raise_for_status`, the alert-connection test endpoint records
`f"{type(exc).__name__}: {exc}"` into `IntegrationConnection.last_error`, and the Integrations
screen renders that field. So one failed "Send test" wrote the sealed secret back out in plaintext
— into the database, into the API response, and onto the page — beside the encrypted copy.

The same shape as the Apify key that rode in a query string. A secret does not stop being a secret
because it is inside an error message, and an error message is the one place nobody thinks to look.

Used at the boundary where text becomes durable or visible, never as a substitute for not having
the secret in hand: redaction is the second line, sealing is the first.
"""
from __future__ import annotations

import re

#: Any URL. The whole path is dropped rather than trimmed, because for a webhook the path IS the
#: token — `hooks.slack.com` alone is public knowledge, `/services/T.../B.../xxxx` is the key.
_URL = re.compile(r"https?://[^\s'\"<>)\]]+", re.IGNORECASE)

#: A bearer/api token appearing as a bare word. Long, high-entropy-looking runs with a known
#: provider prefix — deliberately prefix-anchored rather than "any long string", because an opaque
#: 40-character id in an error message is often the thing an operator needs to read.
_TOKEN = re.compile(
    r"\b("
    r"apify_api_[A-Za-z0-9]{8,}"
    r"|sk-[A-Za-z0-9_\-]{16,}"
    r"|xox[baprs]-[A-Za-z0-9\-]{8,}"
    r"|gsk_[A-Za-z0-9]{16,}"
    # Stripe echoes a masked key in some errors, but not all, and it is the one credential where
    # "money fails silently" is the documented design brief.
    r"|sk_(?:live|test)_[A-Za-z0-9]{8,}"
    r"|rk_(?:live|test)_[A-Za-z0-9]{8,}"
    r"|pat-[A-Za-z0-9\-]{8,}"
    r"|ghp_[A-Za-z0-9]{16,}"
    r")\b",
    re.IGNORECASE,
)


def redact_url(url: str) -> str:
    """``https://hooks.slack.com/services/T00/B11/xxxx`` -> ``https://hooks.slack.com/…``.

    The host survives because it is what makes an error diagnosable — "Slack refused this" and
    "Teams refused this" are different problems — and a hostname is not a secret.
    """
    match = re.match(r"(https?://[^/\s]+)", (url or "").strip(), re.IGNORECASE)
    return f"{match.group(1)}/…" if match else ""


def redact(text: str, *, limit: int = 500) -> str:
    """Text with every URL reduced to its host and every recognisable token masked.

    Never raises and never returns None: this runs on an error path, and a redactor that throws
    would replace a leak with an outage.
    """
    try:
        cleaned = _URL.sub(lambda m: redact_url(m.group(0)), text or "")
        cleaned = _TOKEN.sub("<redacted>", cleaned)
        return cleaned[:limit]
    except Exception:  # pragma: no cover - a redactor must never be the thing that fails
        return "<error text withheld>"
