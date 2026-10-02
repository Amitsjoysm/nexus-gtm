"""A mailbox that cannot be read says so, on the screen that owns it.

`sync_mailbox` caught an expired grant and a provider limit and let every other provider refusal
escape. The worker's sweep catches it per mailbox and logs it, and the tenant session rolls back on
the way out, so whatever the failure had written was lost with it: a misconfigured mailbox app
(`tokens.fresh_access_token` marks the mailbox `error` and says why) and a refused read (a Google
project without the Gmail API enabled answers 403) both left the mailbox reading "Connected", with
no error, while no reply was ever picked up. The sweep then tried again every five minutes, forever.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.core.config import get_settings
from nexus.core.db import utcnow
from tests.conftest import tenant_session
from tests.test_engagement_sequences import _world

REFUSAL = ("Gmail API has not been used in project 1234 before or it is disabled. Enable it by "
           "visiting https://console.developers.google.com/apis/api/gmail.googleapis.com")


class Refusing:
    """A provider whose reads are refused until told otherwise."""

    provider = "google"

    def __init__(self):
        self.refuse = True

    async def fetch_changes(self, cursor):
        from nexus.engagement.mailboxes.provider import ChangeBatch, ProviderError

        if self.refuse:
            raise ProviderError(REFUSAL, status=403)
        return ChangeBatch(message_ids=[], next_cursor="1")


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


async def _mailbox(tid, mailbox_id):
    from nexus.models.engagement import MailboxConnection

    async with tenant_session(tid) as ts:
        return await ts.get(MailboxConnection, mailbox_id)


async def test_a_refused_read_is_kept_on_the_mailbox_and_cleared_once_it_reads(engine_on):
    from nexus.engagement.mailboxes import registry
    from nexus.workers.tasks import handle_sync_mailboxes

    tid, _user, mailbox_id, _contacts = await _world("refusedread")
    box = Refusing()
    registry.set_provider_factory(lambda _connection: box)
    try:
        await handle_sync_mailboxes({})
        mailbox = await _mailbox(tid, mailbox_id)
        assert mailbox.last_error and "Gmail API has not been used" in mailbox.last_error
        assert mailbox.last_error.startswith("Replies could not be read")
        # Still connected: a refused read is not a revoked grant, and reconnecting would not help.
        assert mailbox.status == "connected" and mailbox.last_synced_at is None

        box.refuse = False
        await handle_sync_mailboxes({})
        mailbox = await _mailbox(tid, mailbox_id)
        assert mailbox.last_error is None and mailbox.last_synced_at is not None
    finally:
        registry.set_provider_factory(None)


async def test_a_misconfigured_mailbox_app_is_reported_not_rolled_back(engine_on, monkeypatch):
    from nexus.engagement.mailboxes import oauth
    from nexus.engagement.mailboxes.provider import ProviderError
    from nexus.engagement.mailboxes.tokens import seal
    from nexus.models.engagement import MailboxConnection
    from nexus.workers.tasks import handle_sync_mailboxes

    async def invalid_client(app, refresh_token):
        raise ProviderError("invalid_client: The OAuth client was not found.", status=400)

    monkeypatch.setattr(oauth, "refresh", invalid_client)
    tid, _user, mailbox_id, _contacts = await _world("badapp")
    async with tenant_session(tid) as ts:
        mailbox = await ts.get(MailboxConnection, mailbox_id)
        mailbox.tokens = seal({"access_token": "", "refresh_token": "refresh-1"})
        # A live push subscription, so renewal (which catches this itself) is not what reports it:
        # the sync is.
        mailbox.notifications_expire_at = utcnow() + timedelta(days=5)

    await handle_sync_mailboxes({})
    mailbox = await _mailbox(tid, mailbox_id)
    assert mailbox.status == "error"
    assert mailbox.last_error.startswith("The mailbox app is misconfigured")
