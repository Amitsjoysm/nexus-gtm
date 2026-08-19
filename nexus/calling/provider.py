"""Telephony provider seam.

v1 ships only :class:`StubCallProvider` — no network, no real calls. Dialing is click-to-dial
(a ``tel:`` URL the SDR's phone/softphone handles) and outcomes are logged manually. Tier 2 drops
in a ``TwilioCallProvider`` (place_call/recording/transcript over the wire) and tier 3 a voice-agent
variant — both behind the same interface, selected by ``NEXUS_TELEPHONY_PROVIDER``. This mirrors
``build_email_verifier`` / ``build_crm_connector_from_settings``: activation is one env line and no
caller code changes.
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
KNOWN_PROVIDERS = ("", "stub", "none")


class TelephonyNotImplemented(RuntimeError):
    """A provider was configured that has no implementation here."""


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
    """
    key = (name or "").strip().lower()
    if key in KNOWN_PROVIDERS:
        return StubCallProvider()
    raise TelephonyNotImplemented(
        f"NEXUS_TELEPHONY_PROVIDER={name!r} has no implementation. "
        f"Only {', '.join(p or '(blank)' for p in KNOWN_PROVIDERS)} are available; a real "
        f"provider (Twilio) is not built yet. Leave it unset for click-to-dial."
    )


_provider: CallProvider | None = None


def get_call_provider() -> CallProvider:
    global _provider
    if _provider is None:
        from nexus.core.config import get_settings

        _provider = build_call_provider(get_settings().telephony_provider)
    return _provider


def set_call_provider(provider: CallProvider) -> None:
    """Test/runtime override."""
    global _provider
    _provider = provider
