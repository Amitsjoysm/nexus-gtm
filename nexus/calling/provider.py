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


def build_call_provider(name: str) -> CallProvider:
    """Construct the configured provider. Unknown/blank keys fail safe to the offline stub."""
    key = (name or "").strip().lower()
    # Tier 2/3 providers (twilio, voice agent) register here later. Until then everything is stub.
    if key in ("", "stub", "none"):
        return StubCallProvider()
    return StubCallProvider()


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
