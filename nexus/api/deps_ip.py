# nexus/api/deps_ip.py
"""Restrict the Control plane to named origins.

The panel grants power over pricing, provider credentials and other people's workspaces. Origin is
worth checking on top of authentication: a stolen admin token is worth a great deal less if it also
has to arrive from the right network.

Three properties stop this becoming a lockout, and all three are deliberate:

* **Empty means open.** Default-closed would lock every existing deployment out of its own admin
  panel the moment it upgraded. Security that ships as an outage does not get kept.
* **At most two entries.** A policy limit rather than a technical one — three is where an allowlist
  stops being a restriction and starts being a list. Use a CIDR range for an office network.
* **A malformed list is ignored, not enforced.** The only way to fix a bad allowlist is through the
  panel the allowlist would have closed, so a typo must not be able to lock the door behind itself.

The refusal names the address we actually observed. Behind a proxy that is frequently not the one
the operator expects, and without seeing it they cannot fix their own lockout.
"""
from __future__ import annotations

import ipaddress
import logging

logger = logging.getLogger("nexus.api.deps_ip")

MAX_ENTRIES = 2


def parse_allowlist(raw: str) -> list:
    """Parse a comma-separated list of addresses or CIDR ranges. Raises on anything malformed.

    Refuses rather than skipping: silently dropping an unparseable entry turns a typo into an open
    panel, which is the exact opposite of what the operator was trying to do.
    """
    entries = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if not entries:
        return []
    if len(entries) > MAX_ENTRIES:
        raise ValueError(
            f"the admin IP allowlist takes at most {MAX_ENTRIES} entries, got {len(entries)}. "
            f"Use a CIDR range for a network."
        )
    nets = []
    for entry in entries:
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise ValueError(f"'{entry}' is not an IP address or CIDR range") from exc
    return nets


def ip_allowed(client_ip: str, nets: list) -> bool:
    """Whether this address may reach the Control plane. An empty allowlist admits everyone."""
    if not nets:
        return True
    if not client_ip:
        # No observable origin, against a list that says origin matters: refuse. An unknown address
        # must not pass a check whose entire purpose is knowing where the request came from.
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(addr in net for net in nets)


def client_ip_of(request) -> str:
    """The caller's address, honouring one hop of ``X-Forwarded-For``.

    Takes the FIRST entry, which is the original client. Behind our own reverse proxy that is the
    value to trust.

    A deployment that exposes the application directly to the internet must not enable the
    allowlist and rely on this header, because a client can set it freely — there, the allowlist is
    only as good as the proxy in front of it. That is a property of every header-based origin check
    and is why this is one layer rather than the whole defence.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or ""


# ---- where the value lives -------------------------------------------------------------------
# NOT on `Settings`. Pydantic refuses an attribute that is not a declared field, so the runtime
# override mechanism — which works by `setattr` on the cached Settings object — cannot carry a
# setting that `config.py` does not declare. Rather than add a field for it, the allowlist keeps
# its own module-level cache, refreshed from `runtime_settings` on the same TTL as everything else.
#
# The read has to be synchronous: it happens on every Control-plane request, and a database round
# trip per request to decide whether the request is allowed would be a self-inflicted latency tax
# on the panel it protects.
SETTING_KEY = "admin_ip_allowlist"

_raw: str = ""
_nets: list = []


def current_allowlist() -> str:
    """The raw configured value, for display."""
    return _raw


def set_allowlist(raw: str) -> None:
    """Install a new allowlist into this process. Parses eagerly so a bad value is caught here.

    A malformed value leaves the previous list in place rather than clearing it. Clearing would
    turn a typo into an open panel, which is the opposite of the operator's intent.
    """
    global _raw, _nets
    nets = parse_allowlist(raw)          # raises on malformed input
    _raw, _nets = raw or "", nets


async def refresh_allowlist() -> None:
    """Re-read the stored allowlist. Never raises — see the module docstring."""
    try:
        from sqlalchemy import select

        from nexus.core.db import get_platform_sessionmaker
        from nexus.models.runtime_setting import RuntimeSetting

        async with get_platform_sessionmaker()() as s:
            row = (
                await s.scalars(
                    select(RuntimeSetting).where(RuntimeSetting.key == SETTING_KEY)
                )
            ).first()
        set_allowlist(row.value if row is not None else "")
    except Exception:
        # Falls open on a read failure, deliberately. A database blip must not lock an operator out
        # of the panel they would use to diagnose it.
        logger.warning("could not refresh the admin IP allowlist; leaving it as-is", exc_info=True)


def check_admin_origin(request) -> None:
    """Raise 403 when the Control plane is not permitted from this address."""
    from fastapi import HTTPException, status

    if not _nets:
        return

    observed = client_ip_of(request)
    if not ip_allowed(observed, _nets):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"the control plane is not permitted from "
            f"{observed or 'an unknown address'}",
        )
