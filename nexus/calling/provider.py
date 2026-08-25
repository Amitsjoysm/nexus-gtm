"""Telephony provider seam.

Two providers ship. :class:`StubCallProvider` is the **default** and is not a placeholder:
dialing is click-to-dial (a ``tel:`` URL the SDR's phone/softphone handles) and outcomes are
logged manually — a complete workflow that needs no telephony account. ``twilio`` places real,
billable calls over the Twilio REST API (``nexus/calling/twilio.py``) and attaches the
provider's own recording/transcript to the logged outcome. A tier-3 voice-agent variant would
sit behind this same interface.

Selection is one env line (``NEXUS_TELEPHONY_PROVIDER``) with no caller code changes, mirroring
``build_email_verifier`` / ``build_crm_connector_from_settings``. Unlike those seams, a name we
cannot build raises here rather than degrading — see :func:`build_call_provider`.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(slots=True)
class CallHandle:
    """Result of initiating a call. In v1 ``mode='manual'`` and ``dial_url`` is a tel: link; a real
    provider returns ``mode='live'`` with a ``provider_call_id`` to fetch recording/transcript."""

    mode: str = "manual"                      # "manual" | "live"
    dial_url: str | None = None               # e.g. "tel:+15551234567"
    provider_call_id: str | None = None


class CallProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def place_call(self, *, to: str, from_: str, context: dict | None = None) -> CallHandle: ...

    # Outcome enrichment. Best-effort by contract: these return ``None`` rather than raising, so a
    # provider hiccup can never stop a rep from logging what happened on the call. ``place_call``
    # is the opposite and must raise — see :class:`CallProviderError`.
    async def get_call_status(self, provider_call_id: str) -> dict | None:
        return None

    async def get_recording(self, provider_call_id: str) -> str | None:
        return None

    async def get_transcript(self, provider_call_id: str) -> str | None:
        return None


class StubCallProvider(CallProvider):
    """Offline default: no real call. Returns a click-to-dial ``tel:`` URL for the SDR's phone."""

    name = "stub"

    async def place_call(self, *, to: str, from_: str, context: dict | None = None) -> CallHandle:
        digits = "".join(c for c in (to or "") if c.isdigit() or c == "+")
        return CallHandle(mode="manual", dial_url=f"tel:{digits}" if digits else None)


# Providers this module can actually construct. Anything else is a name with no implementation
# behind it, and must not be silently honoured — see below.
KNOWN_PROVIDERS = ("", "stub", "none", "twilio")

# The subset of the above that means "no telephony": click-to-dial via the stub.
_STUB_KEYS = ("", "stub", "none")


class TelephonyError(RuntimeError):
    """Base for every telephony failure, so a caller can catch one type."""


class TelephonyNotImplemented(TelephonyError):
    """A provider was configured that has no implementation here."""


class TelephonyNotConfigured(TelephonyError):
    """A real provider was selected but its credentials are missing.

    Distinct from :class:`TelephonyNotImplemented` on purpose: "Twilio does not exist here" and
    "Twilio exists but you have not keyed it" need different fixes, and the status code the API
    returns is the same either way only by coincidence.
    """


class CallProviderError(TelephonyError):
    """A call could not be placed, or the provider rejected the request.

    Raised rather than returned. The whole point of this module's contract is that a dial which
    did not happen must never look like one that did — the same rule as ``ApifyNotConfigured``
    raising instead of returning an empty list.
    """


class InvalidPhoneNumber(CallProviderError):
    """A number is not dialable — caught before any request is spent."""


class AgentNumberRequired(CallProviderError):
    """A live bridge needs the rep's own phone for its first leg."""


def build_call_provider(name: str) -> CallProvider:
    """Construct the configured provider.

    Blank or ``stub`` is the offline default: click-to-dial via a ``tel:`` URL, which is a real,
    working workflow rather than a placeholder.

    **A name we cannot build raises.** This used to return the stub for every input, so setting
    ``NEXUS_TELEPHONY_PROVIDER=twilio`` produced click-to-dial with no error anywhere — the
    operator had configured Twilio, no call was ever placed through it, and nothing said so. That
    is the same "configured and doing nothing" failure as the personalization provider that
    returned the stub for every input, and as `demo` signals scoring fabricated events: the
    codebase's rule is that an integration is inert until keyed and *says so*, never a fake
    fallback.

    Raising here rather than at call time is deliberate: `get_call_provider` is resolved on first
    use, so a bad value surfaces immediately instead of on the first rep's first call.

    ``twilio`` is now real. It raises :class:`TelephonyNotConfigured` when unkeyed, for the same
    reason this function raises on an unknown name: an operator who configured Twilio and got
    click-to-dial back would have no way to discover that no call was ever placed.
    """
    key = (name or "").strip().lower()
    if key in _STUB_KEYS:
        return StubCallProvider()
    if key == "twilio":
        from nexus.calling.twilio import build_twilio_provider

        return build_twilio_provider()
    raise TelephonyNotImplemented(
        f"NEXUS_TELEPHONY_PROVIDER={name!r} has no implementation. "
        f"Only {', '.join(p or '(blank)' for p in KNOWN_PROVIDERS)} are available. "
        f"Leave it unset for click-to-dial."
    )


_provider: CallProvider | None = None


def get_call_provider() -> CallProvider:
    global _provider
    if _provider is None:
        from nexus.core.config import get_settings

        _provider = build_call_provider(get_settings().telephony_provider)
    return _provider


def set_call_provider(provider: CallProvider | None) -> None:
    """Test/runtime override. ``None`` clears it, so the next lookup rebuilds from settings."""
    global _provider
    _provider = provider
