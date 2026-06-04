"""Pluggable alert delivery channels (Tier-2 workflow stickiness).

All offline: the webhook/Slack channels take an injectable HTTP poster, so the payload shape and
the service's durability guarantee are provable with zero network. The service always persists the
alert; it only stamps ``delivered_at`` when a channel actually handled the send.
"""
from __future__ import annotations

import pytest

from nexus.alerts.channels import (
    AlertChannel,
    AlertChannelRegistry,
    AlertDelivery,
    EmailChannel,
    InAppChannel,
    SlackChannel,
    WebhookChannel,
    build_alert_channels_from_settings,
    set_alert_channels,
)
from nexus.alerts.service import get_alert_service
from nexus.models.alerts import Alert
from tests.conftest import make_tenant, tenant_session


@pytest.fixture(autouse=True)
def _restore_channels():
    """Keep the module-global channel registry from leaking between tests."""
    yield
    set_alert_channels(build_alert_channels_from_settings())


class RecordingPoster:
    """Captures (url, payload) instead of opening a socket; returns HTTP 200."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, url: str, payload: dict) -> int:
        self.calls.append((url, payload))
        return 200


class RecordingChannel(AlertChannel):
    name = "slack"

    def __init__(self) -> None:
        self.delivered: list[Alert] = []

    async def deliver(self, alert: Alert) -> AlertDelivery:
        self.delivered.append(alert)
        return AlertDelivery(ok=True, channel=self.name, detail="recorded")


class BoomChannel(AlertChannel):
    name = "slack"

    async def deliver(self, alert: Alert) -> AlertDelivery:
        raise RuntimeError("channel down")


def _alert(**kw) -> Alert:
    base = dict(tenant_id="t1", title="Funding round", body="Acme raised $20M",
                severity="critical", source="play")
    base.update(kw)
    return Alert(**base)


# -- channel payloads (pure, offline) ------------------------------------------------


@pytest.mark.asyncio
async def test_slack_channel_formats_block_kit_offline():
    poster = RecordingPoster()
    channel = SlackChannel("https://hooks.slack.test/abc", poster=poster)

    res = await channel.deliver(_alert())

    assert res.ok and res.channel == "slack"
    assert len(poster.calls) == 1
    url, payload = poster.calls[0]
    assert url == "https://hooks.slack.test/abc"
    # Notification fallback text carries the title; blocks carry the formatted body + context.
    assert "Funding round" in payload["text"]
    section = payload["blocks"][0]["text"]["text"]
    assert "Funding round" in section and "Acme raised $20M" in section
    assert "critical" in payload["blocks"][1]["elements"][0]["text"]


@pytest.mark.asyncio
async def test_webhook_channel_posts_raw_fields_offline():
    poster = RecordingPoster()
    channel = WebhookChannel("https://hooks.test/wh", poster=poster)

    await channel.deliver(_alert(title="Hot account"))

    _, payload = poster.calls[0]
    assert payload["title"] == "Hot account"
    assert payload["severity"] == "critical"
    assert payload["source"] == "play"


@pytest.mark.asyncio
async def test_posting_channel_skips_without_url():
    poster = RecordingPoster()
    channel = WebhookChannel("", poster=poster)

    res = await channel.deliver(_alert())

    assert res.ok is False and "no url" in res.detail
    assert poster.calls == []  # nothing sent


@pytest.mark.asyncio
async def test_email_channel_is_offline_stub():
    res = await EmailChannel("alerts@acme.co").deliver(_alert())
    assert res.ok and res.channel == "email"


# -- registry routing ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_falls_back_to_in_app_for_unknown_channel():
    reg = AlertChannelRegistry([InAppChannel()])
    assert reg.get("nope") is reg.get("in_app")
    res = await reg.deliver(_alert(channel="nope"))
    assert res.ok and res.channel == "in_app"


# -- service durability (offline) ----------------------------------------------------


@pytest.mark.asyncio
async def test_service_routes_to_named_channel_and_marks_delivered():
    rec = RecordingChannel()
    set_alert_channels(AlertChannelRegistry([InAppChannel(), rec]))

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        alert = await get_alert_service().create(
            ts, title="Big news", severity="warning", channel="slack"
        )
        assert alert.delivered_at is not None
        assert [a.title for a in rec.delivered] == ["Big news"]


@pytest.mark.asyncio
async def test_delivery_failure_never_loses_the_alert():
    set_alert_channels(AlertChannelRegistry([InAppChannel(), BoomChannel()]))

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        alert = await get_alert_service().create(
            ts, title="Will fail to send", channel="slack"
        )
        # Persisted despite the channel raising — but not marked delivered.
        assert alert.delivered_at is None
        assert [a.title for a in await get_alert_service().list(ts)] == ["Will fail to send"]
