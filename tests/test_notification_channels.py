"""Telegram + SMTP email alert channels. Offline (injected transport / send seam)."""
from __future__ import annotations

from nexus.alerts.channels import EmailChannel, TelegramChannel
from nexus.models.alerts import Alert


def _alert(**kw):
    a = Alert(
        tenant_id="t", title="Acme raised $10M", body="Nice", severity="critical",
        channel="telegram", source="play",
        meta={"suggested_action": "Reach out now", "source_url": "https://x.com/a"},
    )
    a.id = "al1"
    for k, v in kw.items():
        setattr(a, k, v)
    return a


async def test_telegram_skips_when_unconfigured():
    res = await TelegramChannel("", "").deliver(_alert())
    assert res.ok is False
    assert res.detail == "not configured"


async def test_telegram_posts_expected_payload():
    captured: dict = {}

    async def poster(url, payload):
        captured["url"], captured["payload"] = url, payload
        return 200

    res = await TelegramChannel("TOKEN", "CHAT", poster=poster).deliver(_alert())
    assert res.ok
    assert "botTOKEN/sendMessage" in captured["url"]
    assert captured["payload"]["chat_id"] == "CHAT"
    text = captured["payload"]["text"]
    assert "Acme raised $10M" in text
    assert "Reach out now" in text                 # meta suggested_action appended
    assert "https://x.com/a" in text               # source url appended


async def test_email_is_stub_without_smtp_host():
    res = await EmailChannel("from@x.com").deliver(_alert(channel="email"))
    assert res.ok and res.detail == "stub-logged"


async def test_email_sends_via_seam_when_configured():
    sent: dict = {}
    ch = EmailChannel("from@x.com", host="smtp.example.com", to="rep@x.com",
                      send_seam=lambda m: sent.update(m))
    res = await ch.deliver(_alert(channel="email"))
    assert res.ok and res.detail == "sent"
    assert sent["to"] == "rep@x.com"
    assert "Acme raised $10M" in sent["subject"]
    assert "Reach out now" in sent["body"]         # enriched action line included
