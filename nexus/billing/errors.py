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
        switch_state: str | None = None, switch_message: str = "",
    ):
        self.capability_id = capability_id
        # quota_exhausted | credits_exhausted | disabled | dependency | feature_switch
        self.reason = reason
        self.used = used
        self.quota = quota
        self.plan_id = plan_id
        self.switch_state = switch_state
        self.switch_message = switch_message
        super().__init__(f"{capability_id}: {reason}")

    def to_payload(self) -> dict:
        payload = {
            "error": "quota_exceeded",
            "capability": self.capability_id,
            "reason": self.reason,
            "used": self.used,
            "quota": self.quota,
            "plan": self.plan_id,
            "upgrade_url": "/settings/billing",
        }
        if self.switch_state is not None:
            # A PLATFORM SWITCH IS NOT AN UPSELL. Without these two keys the client renders the
            # generic "your plan does not include this — upgrade" for a feature we have taken down
            # ourselves, so the customer is invited to pay to fix our maintenance window. The
            # client drops `upgrade_url` when `switch_state` is present.
            payload["switch_state"] = self.switch_state
            payload["switch_message"] = self.switch_message
        return payload


class BillingThrottled(BillingError):
    """A burst/rate/cooldown limit was hit (HTTP 429)."""

    def __init__(self, capability_id: str, *, retry_after_s: int):
        self.capability_id = capability_id
        self.retry_after_s = retry_after_s
        super().__init__(f"{capability_id}: throttled")
