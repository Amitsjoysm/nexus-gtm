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
            # The envelope discriminator, kept stable. `reason` is the field that has always
            # carried the specific cause (`quota_exhausted` | `credits_exhausted` | `disabled` |
            # `dependency` | `feature_switch`), so a client that needs the distinction already has
            # it; renaming `error` would break every consumer matching on it for nothing new.
            "error": "quota_exceeded",
            "capability": self.capability_id,
            "reason": self.reason,
            "used": self.used,
            "quota": self.quota,
            "plan": self.plan_id,
        }
        if self.switch_state is None:
            # Only when the customer could actually buy their way in. A PLATFORM SWITCH IS NOT AN
            # UPSELL — no plan re-enables it — so offering a checkout link invites them to pay to
            # fix our own maintenance window. Omitted here rather than filtered in the client,
            # because our own UI is not the only consumer: a customer's integration reading
            # `upgrade_url` would make exactly that mistake, and a comment is not a guarantee.
            payload["upgrade_url"] = "/settings/billing"
        else:
            payload["switch_state"] = self.switch_state
            payload["switch_message"] = self.switch_message
        return payload


class BillingThrottled(BillingError):
    """A burst/rate/cooldown limit was hit (HTTP 429)."""

    def __init__(self, capability_id: str, *, retry_after_s: int):
        self.capability_id = capability_id
        self.retry_after_s = retry_after_s
        super().__init__(f"{capability_id}: throttled")
