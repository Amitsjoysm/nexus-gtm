"""Alert delivery channels behind one interface (mirrors the SEP connector pattern).

An :class:`AlertChannel` turns a persisted :class:`Alert` into a delivered notification.
``in_app`` needs no transport (reading it back is the delivery); ``webhook`` and ``slack`` POST
to a configured URL; ``email`` is a deterministic stub a real adapter swaps for an everifier-
validated sender.

Transport failures are allowed to raise — the caller (:class:`AlertService`) wraps delivery so
the alert is always persisted even when a channel is down. The HTTP poster is injectable, so the
whole pipeline is exercisable offline with zero network: tests pass a recording poster (or swap in
a recording channel) and assert the payload shape without ever opening a socket.
"""
from __future__ import annotations

import abc
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nexus.models.alerts import Alert

logger = logging.getLogger("nexus.alerts.channels")

# An async HTTP POST seam: (url, json_payload) -> HTTP status code. Injectable so the webhook and
# Slack channels can be driven without network in tests.
HttpPoster = Callable[[str, dict], Awaitable[int]]


async def _httpx_post(url: str, payload: dict) -> int:
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.status_code


@dataclass(slots=True)
class AlertDelivery:
    """The outcome of one delivery attempt. ``ok`` False means handled-but-not-sent (e.g. no URL
    configured); a hard transport failure raises instead and is caught by the caller."""

    ok: bool
    channel: str
    detail: str = ""


class AlertChannel(abc.ABC):
    name: str

    @abc.abstractmethod
    async def deliver(self, alert: Alert) -> AlertDelivery: ...


class InAppChannel(AlertChannel):
    """No transport: the alert row is the delivery (the API/UI reads it back)."""

    name = "in_app"

    async def deliver(self, alert: Alert) -> AlertDelivery:
        return AlertDelivery(ok=True, channel=self.name, detail="persisted")


class _PostingChannel(AlertChannel):
    """Shared base for channels that POST a JSON body to a configured URL."""

    def __init__(self, url: str = "", *, poster: HttpPoster | None = None) -> None:
        self._url = url
        self._post = poster or _httpx_post

    def _payload(self, alert: Alert) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError

    async def deliver(self, alert: Alert) -> AlertDelivery:
        if not self._url:
            logger.info("alert %s: no %s URL configured, skipping", alert.id, self.name)
            return AlertDelivery(ok=False, channel=self.name, detail="no url configured")
        status = await self._post(self._url, self._payload(alert))
        return AlertDelivery(ok=True, channel=self.name, detail=f"http {status}")


class WebhookChannel(_PostingChannel):
    """Generic JSON webhook — POSTs the raw alert fields for a consumer to format."""

    name = "webhook"

    def _payload(self, alert: Alert) -> dict:
        return {
            "id": alert.id,
            "title": alert.title,
            "body": alert.body,
            "severity": alert.severity,
            "account_id": alert.account_id,
            "source": alert.source,
        }


class SlackChannel(_PostingChannel):
    """Slack Incoming Webhook — formats the alert as Block Kit so it reads well in a channel."""

    name = "slack"

    _EMOJI = {"info": ":information_source:", "warning": ":warning:", "critical": ":rotating_light:"}

    def _payload(self, alert: Alert) -> dict:
        emoji = self._EMOJI.get(alert.severity, ":bell:")
        heading = f"{emoji} *{alert.title}*"
        if alert.body:
            heading += f"\n{alert.body}"
        return {
            # ``text`` is the notification fallback (lockscreen / older clients); blocks render in-app.
            "text": f"{emoji} {alert.title}",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": heading}},
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"severity *{alert.severity}* · source {alert.source}",
                        }
                    ],
                },
            ],
        }


class EmailChannel(AlertChannel):
    """Deterministic offline stub. A real adapter sends from an everifier-validated sender."""

    name = "email"

    def __init__(self, sender: str = "") -> None:
        self._sender = sender or "alerts@nexus.local"

    async def deliver(self, alert: Alert) -> AlertDelivery:
        logger.info(
            "[email-stub from %s] alert %s -> %s: %s",
            self._sender,
            alert.id,
            alert.severity,
            alert.title,
        )
        return AlertDelivery(ok=True, channel=self.name, detail="stub-logged")


class AlertChannelRegistry:
    """Routes an alert to the channel named on it, falling back to ``in_app``."""

    def __init__(self, channels: list[AlertChannel]) -> None:
        self._channels: dict[str, AlertChannel] = {c.name: c for c in channels}
        if "in_app" not in self._channels:
            self._channels["in_app"] = InAppChannel()

    def get(self, name: str) -> AlertChannel:
        return self._channels.get(name) or self._channels["in_app"]

    async def deliver(self, alert: Alert) -> AlertDelivery:
        return await self.get(alert.channel).deliver(alert)


def build_alert_channels_from_settings() -> AlertChannelRegistry:
    from nexus.core.config import get_settings

    s = get_settings()
    return AlertChannelRegistry(
        [
            InAppChannel(),
            WebhookChannel(s.alert_webhook_url),
            SlackChannel(s.alert_slack_webhook_url),
            EmailChannel(s.alert_email_sender),
        ]
    )


_registry: AlertChannelRegistry | None = None


def get_alert_channels() -> AlertChannelRegistry:
    global _registry
    if _registry is None:
        _registry = build_alert_channels_from_settings()
    return _registry


def set_alert_channels(registry: AlertChannelRegistry | None) -> None:
    global _registry
    _registry = registry
