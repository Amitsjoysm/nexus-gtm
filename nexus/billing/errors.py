# nexus/billing/errors.py
"""Billing exceptions. Routers translate these into 402/429 with upgrade context."""
from __future__ import annotations


class BillingError(Exception):
    """Base for billing enforcement failures."""


class QuotaExceeded(BillingError):
    """The tenant's plan does not permit this action right now (HTTP 402).

    Carries everything the UI needs to render a useful upsell instead of a dead error.
    """

    def __init__(
        self, capability_id: str, *, reason: str, used: float = 0,
        quota: int | None = None, plan_id: str | None = None,
    ):
        self.capability_id = capability_id
        self.reason = reason          # quota_exhausted | disabled | dependency
        self.used = used
        self.quota = quota
        self.plan_id = plan_id
        super().__init__(f"{capability_id}: {reason}")

    def to_payload(self) -> dict:
        return {
            "error": "quota_exceeded",
            "capability": self.capability_id,
            "reason": self.reason,
            "used": self.used,
            "quota": self.quota,
            "plan": self.plan_id,
            "upgrade_url": "/settings/billing",
        }


class BillingThrottled(BillingError):
    """A burst/rate/cooldown limit was hit (HTTP 429)."""

    def __init__(self, capability_id: str, *, retry_after_s: int):
        self.capability_id = capability_id
        self.retry_after_s = retry_after_s
        super().__init__(f"{capability_id}: throttled")
