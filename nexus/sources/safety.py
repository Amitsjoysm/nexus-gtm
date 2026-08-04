# nexus/sources/safety.py
"""What a registered source database is allowed to be, and what it is allowed to be asked.

Two attack surfaces open the moment an admin can type a connection string into a web form.

**1. The DSN is an SSRF primitive.** A form that accepts `postgresql://…@host:5432/db` and reports
whether it connected is a port scanner. Pointed at `localhost`, the container network, or a cloud
metadata endpoint, the connect/refuse distinction leaks internal topology, and successful
introspection turns it into a read oracle over infrastructure the admin was never granted. Being a
platform admin is not the same as being authorised to read the host's own database — and the whole
point of this feature is that the credential comes from a *customer*, so the person typing it is
not necessarily the person who owns what it points at.

**2. Introspection results become query fragments.** The admin maps a discovered column onto an app
field, and we then build a query from it. Anything that reaches a SQL string has to be an
identifier we discovered ourselves and re-validated, never text that arrived from the browser. This
module is where that check lives, so there is one place to audit rather than one per query.

Neither guard is theoretical: the first is the standard finding on any "add your own datasource"
feature, and the second is how a mapping UI becomes SQL injection with extra steps.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

# Identifiers we will put into SQL. Deliberately stricter than Postgres allows: no quotes, no
# dots, no spaces, no unicode. A legitimate table we cannot map is an inconvenience; a mapping that
# smuggles `"; DROP` past a quote is not recoverable.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# Schemes we can actually talk to. An unknown scheme is refused rather than passed to the driver,
# which would happily try `file://` or a socket path.
_ALLOWED_SCHEMES = frozenset({"postgresql", "postgres", "postgresql+asyncpg"})


class SourceRejected(ValueError):
    """The DSN or identifier is not something we will connect to or query."""


def valid_identifier(name: str | None) -> bool:
    """Whether ``name`` is safe to interpolate as a schema/table/column identifier."""
    return bool(name) and bool(_IDENT.match(name))


def require_identifier(name: str | None, *, what: str = "identifier") -> str:
    """Return ``name`` if it is a safe identifier, else raise.

    Called on **every** value that reaches a SQL string, including ones we discovered ourselves —
    a name that came back from introspection is still attacker-controlled if the attacker owns the
    source database, and a table called `x"; DROP TABLE y; --` is a legal Postgres identifier.
    """
    if not valid_identifier(name):
        raise SourceRejected(f"unsafe {what}: {name!r}")
    return name  # type: ignore[return-value]


def _is_blocked_host(host: str, *, allow_private: bool) -> tuple[bool, str]:
    """Whether this host is somewhere we refuse to connect."""
    if not host:
        return True, "no host in DSN"
    lowered = host.lower().strip("[]")

    # Cloud instance-metadata endpoints. These hand out IAM credentials to anything that can reach
    # them, so they are refused by name as well as by address — a DNS name resolving there is the
    # usual bypass.
    if lowered in ("metadata.google.internal", "metadata.goog", "instance-data"):
        return True, "cloud metadata endpoint"

    if allow_private:
        return False, ""

    try:
        infos = socket.getaddrinfo(lowered, None)
    except OSError as exc:
        return True, f"host does not resolve: {exc}"

    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        # Resolve-then-check, so a public name pointing at 127.0.0.1 is caught. Note this is
        # inherently racy (DNS can change between check and connect); it raises the bar rather
        # than closing the hole, and the real boundary is network egress policy.
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            return True, f"resolves to a private or loopback address ({addr})"
    return False, ""


def validate_dsn(dsn: str, *, allow_private: bool = False) -> str:
    """Check a DSN before we ever hand it to a driver. Returns it unchanged, or raises.

    ``allow_private`` exists for local development and tests, where the source database genuinely
    is on localhost. It is a setting, not a request parameter — an admin must never be able to turn
    the guard off from the form that the guard exists to protect.
    """
    raw = (dsn or "").strip()
    if not raw:
        raise SourceRejected("empty connection string")

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SourceRejected(
            f"unsupported scheme {scheme!r}; only PostgreSQL is supported"
        )

    blocked, why = _is_blocked_host(parsed.hostname or "", allow_private=allow_private)
    if blocked:
        raise SourceRejected(f"refusing to connect: {why}")

    if parsed.port is not None and not (1 <= parsed.port <= 65535):
        raise SourceRejected(f"invalid port {parsed.port}")
    return raw


def redact_dsn(dsn: str) -> str:
    """A DSN safe to log or show. Never let a password reach a log line or an audit row."""
    try:
        parsed = urlparse(dsn)
    except Exception:
        return "<unparseable dsn>"
    if not parsed.hostname:
        return "<dsn>"
    user = f"{parsed.username}@" if parsed.username else ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{user}{parsed.hostname}{port}{parsed.path or ''}"
