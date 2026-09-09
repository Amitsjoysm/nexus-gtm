# nexus/alerts/url_guard.py
"""An alert webhook URL is an SSRF primitive, and the same one `nexus/sources/safety.py` guards.

Found by attacking the running deployment: an ordinary customer (`manage_workspace`) could
`PUT /alert-connections/slack` with `url` pointing at `https://169.254.169.254/...` (the cloud
metadata endpoint that hands out IAM credentials), `https://postgres:5432/`, or the internal admin
API, and then `POST /alert-connections/slack/test` to force an outbound request to it. The only
check was `startswith("https://")`. Measured against internal hosts, the delivery error was a
verbatim oracle — `SSL: WRONG_VERSION_NUMBER` (open HTTP port), `ConnectError: ""` (open),
`All connection attempts failed` (closed), `Name or service not known` (no such host) — a working
internal port scanner and service fingerprinter, driven by a customer.

The guard the codebase already had for the DSN case was never applied here. This reuses its core
(`sources.safety._is_blocked_host`) so there is one definition of "somewhere we refuse to connect":
cloud metadata endpoints by name, and any host resolving to a loopback / private / link-local /
reserved address (which is what catches `169.254.169.254`). `allow_private` is a SETTING, never a
request parameter — an admin must not switch off an SSRF guard from the form it protects, the same
rule as `source_db_allow_private`, and it is in the runtime `FORBIDDEN` set for the same reason.

The load-bearing call site is DELIVERY, not connect: DNS can rebind between storing a URL and
sending to it (TOCTOU), and rows stored before this existed never passed a connect-time check. The
connect-time call is fail-fast UX and defence in depth; the delivery-time call is the boundary.
"""
from __future__ import annotations

from urllib.parse import urlparse

from nexus.sources.safety import _is_blocked_host


class WebhookURLRejected(ValueError):
    """A webhook URL points somewhere we refuse to send to."""


def validate_webhook_url(url: str, *, allow_private: bool = False) -> str:
    """Return the URL unchanged, or raise `WebhookURLRejected`.

    `allow_private` exists for local development, where a webhook genuinely points at localhost
    (a mock receiver). It is a setting, never a request field.
    """
    raw = (url or "").strip()
    if not raw:
        raise WebhookURLRejected("empty webhook URL")

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        # http:// would also carry the credential in cleartext; only https is a webhook URL.
        raise WebhookURLRejected("the webhook URL must be an https:// address")

    host = parsed.hostname or ""
    blocked, why = _is_blocked_host(host, allow_private=allow_private)
    if blocked:
        # Deliberately does not echo the resolved address back to the caller; the boolean outcome
        # is enough for them and the detail is the oracle we are closing.
        raise WebhookURLRejected(f"refusing to send to this host: {why}")

    if parsed.port is not None and not (1 <= parsed.port <= 65535):
        raise WebhookURLRejected(f"invalid port {parsed.port}")
    return raw
