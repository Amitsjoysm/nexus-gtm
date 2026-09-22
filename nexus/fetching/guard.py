"""What the fetcher is allowed to request.

An endpoint that takes a URL and reports what came back is a port scanner and a read oracle when it
is pointed at the container network or a metadata endpoint. `nexus/sources/safety.py` refuses
private DSNs for exactly this reason, so this reuses its host rules rather than keeping a second
copy — two copies of an SSRF guard drift, and the one that falls behind is the hole.

Resolve, THEN check: a public hostname can resolve to loopback, and checking the name alone is the
bypass. That check is inherently racy (DNS can change between check and fetch); it raises the bar,
and the real boundary is the service's firewall and the shared secret in front of it.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from nexus.sources.safety import _is_blocked_host

_FETCHABLE_SCHEMES = frozenset({"http", "https"})


class UrlRejected(ValueError):
    """The URL may not be fetched. The message names the reason, for the operator."""


def check_url(url: str) -> str:
    """Return the URL when it is safe to fetch; raise :class:`UrlRejected` when it is not."""
    candidate = (url or "").strip()
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in _FETCHABLE_SCHEMES:
        raise UrlRejected(f"scheme {parts.scheme or '(none)'} is not fetchable")
    # user:pass@host is how an SSRF payload smuggles a different authority past a naive parser.
    if parts.username or parts.password:
        raise UrlRejected("credentials in a URL are not accepted")
    blocked, reason = _is_blocked_host(parts.hostname or "", allow_private=False)
    if blocked:
        raise UrlRejected(f"{parts.hostname or '(no host)'} is not fetchable: {reason}")
    return candidate
