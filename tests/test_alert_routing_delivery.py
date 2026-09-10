"""Alerts reach Slack / Teams / Telegram when somebody asked for them to.

The bug these pin, found on the live deployment 2026-09-10: `routing.route()` — the function that
turns "send funding alerts to Teams, straight away" into a delivery decision — had NO production
caller. Every signal alert was created with the default `channel="in_app"` and delivered only there:
315 of 315 on the live database, while a superadmin's saved `funding -> teams, immediate` sat unread
for six days. The guided setup screen told people "Alerts are ready ... the next matching signal is
the first one you will get", and nothing ever arrived.

It survived because `route()` was tested as a pure function, in isolation, and nothing asserted that
the path which actually runs on ingestion consults it. So every test here goes through
`raise_alerts_for` — the function ingestion calls — and asserts on what a channel RECEIVED.

The combined model these pin (decided with the product owner):

* **Workspace rules** — a manager says "this channel receives funding alerts". Posts for every
  account, and nobody's personal setting can mute it.
* **Personal routes** — any member routes a category to a connected channel, scoped to "all alerts"
  or "only accounts I own". Their own `off` and quiet hours apply to their own routes only.
* **Once per channel** — an alert posts to a channel at most once, however many rules and people
  point it there. The channel is shared; three reps routing funding to Slack is one post, not three.
"""
from __future__ import annotations

import pytest

from nexus.alerts.channels import AlertChannel, AlertDelivery
from tests.conftest import make_tenant, tenant_session


class _Recorder(AlertChannel):
    """A channel that records what it was asked to deliver, and never opens a socket."""

    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.sent: list[str] = []
        self._fail = fail

    async def deliver(self, alert) -> AlertDelivery:
        if self._fail:
            # Shaped like the real failure: httpx puts the full webhook URL — which IS the
            # credential — into the exception message.
            raise RuntimeError(
                "POST https://hooks.slack.com/services/T0/B0/SECRETSECRET failed: 500"
            )
        self.sent.append(alert.id)
        return AlertDelivery(ok=True, channel=self.name, detail="http 200")


@pytest.fixture
def channels():
    from nexus.alerts.channels import AlertChannelRegistry, InAppChannel, set_alert_channels

    rec = {k: _Recorder(k) for k in ("slack", "teams", "telegram", "email")}
    set_alert_channels(AlertChannelRegistry([InAppChannel(), *rec.values()]))
    yield rec
    set_alert_channels(None)


# ---- fixtures -------------------------------------------------------------------------------------

async def _user(tid: str, email: str, role: str = "rep") -> str:
    from nexus.models.identity import Membership, User

    async with tenant_session(tid) as ts:
        user = User(email=email, full_name=email.split("@")[0], password_hash="x")
        ts.session.add(user)
        await ts.flush()
        ts.add(Membership(tenant_id=tid, user_id=user.id, role=role))
        await ts.flush()
        return user.id


async def _account(tid: str, *, owner: str | None = None) -> str:
    from nexus.models.account import Account

    async with tenant_session(tid) as ts:
        acct = Account(tenant_id=tid, name="Acme", domain="acme.com", owner_user_id=owner)
        ts.add(acct)
        await ts.flush()
        return acct.id


async def _pref(tid: str, user_id: str, *, category="funding", channel="slack",
                mode="immediate", scope="all", quiet: tuple[int, int] | None = None) -> None:
    from nexus.models.notification_preference import NotificationPreference

    async with tenant_session(tid) as ts:
        ts.add(NotificationPreference(
            tenant_id=tid, user_id=user_id, category=category, channel=channel, mode=mode,
            scope=scope,
            quiet_from_min=quiet[0] if quiet else None,
            quiet_to_min=quiet[1] if quiet else None,
        ))
        await ts.flush()


async def _rule(tid: str, *, channel="slack", category="funding") -> None:
    from nexus.models.alerts import AlertChannelRule

    async with tenant_session(tid) as ts:
        ts.add(AlertChannelRule(tenant_id=tid, channel=channel, category=category))
        await ts.flush()


async def _signal(tid: str, account_id: str, *, kind="funding", strength=0.9) -> str:
    from nexus.core.db import utcnow
    from nexus.models.signal import SignalEvent

    async with tenant_session(tid) as ts:
        sig = SignalEvent(
            tenant_id=tid, account_id=account_id, kind=kind, source="test",
            title=f"Acme {kind}", strength=strength, dedupe_key=f"k:{kind}:{account_id}",
            occurred_at=utcnow(),
        )
        ts.add(sig)
        await ts.flush()
        return sig.id


async def _fire(tid: str, signal_id: str) -> None:
    """Exactly what ingestion does: raise alerts in the transaction that owns the signal."""
    from nexus.alerts.signal_alerts import raise_alerts_for
    from nexus.models.account import Account
    from nexus.models.signal import SignalEvent

    async with tenant_session(tid) as ts:
        signal = await ts.first(SignalEvent, SignalEvent.id == signal_id)
        account = await ts.first(Account, Account.id == signal.account_id)
        await raise_alerts_for(ts, account, [signal])


async def _alerts(tid: str):
    from nexus.models.alerts import Alert

    async with tenant_session(tid) as ts:
        return await ts.list(Alert)


def _quiet_now() -> tuple[int, int]:
    """A quiet window that contains the current UTC minute, whatever time the suite runs."""
    from nexus.alerts.routing import minutes_utc
    from nexus.core.db import utcnow

    now = minutes_utc(utcnow())
    return ((now - 60) % 1440, (now + 60) % 1440)


# ---- personal routes -------------------------------------------------------------------------------

async def test_a_personal_route_posts_the_alert_to_that_channel(channels):
    """The whole bug in one test: somebody routed funding to Slack, and Slack must receive it."""
    tid = await make_tenant(slug="rd1")
    rep = await _user(tid, "rep@rd1.com")
    await _pref(tid, rep, channel="slack")
    acct = await _account(tid)
    await _fire(tid, await _signal(tid, acct))

    assert len(channels["slack"].sent) == 1, "a routed funding alert never reached Slack"
    # The alert still exists in-app too: routing ADDS a destination, it never replaces the inbox.
    assert len(await _alerts(tid)) == 1


async def test_an_alert_is_only_routed_for_its_own_category(channels):
    tid = await make_tenant(slug="rd2")
    rep = await _user(tid, "rep@rd2.com")
    await _pref(tid, rep, category="hiring", channel="slack")
    acct = await _account(tid)
    await _fire(tid, await _signal(tid, acct, kind="funding"))

    assert channels["slack"].sent == []


async def test_off_never_posts(channels):
    """`off` is a real choice — "never send me this" — not the absence of one."""
    tid = await make_tenant(slug="rd3")
    rep = await _user(tid, "rep@rd3.com")
    await _pref(tid, rep, channel="slack", mode="off")
    acct = await _account(tid)
    await _fire(tid, await _signal(tid, acct))

    assert channels["slack"].sent == []


async def test_a_digest_route_is_not_sent_immediately(channels):
    """Digest routes belong to the digest sweep. Sending them now as well would send them twice."""
    tid = await make_tenant(slug="rd4")
    rep = await _user(tid, "rep@rd4.com")
    await _pref(tid, rep, channel="slack", mode="digest")
    acct = await _account(tid)
    await _fire(tid, await _signal(tid, acct))

    assert channels["slack"].sent == []


async def test_quiet_hours_hold_a_personal_route(channels):
    """Proves `route()` is actually consulted: quiet-hours arithmetic lives nowhere else."""
    tid = await make_tenant(slug="rd5")
    rep = await _user(tid, "rep@rd5.com")
    await _pref(tid, rep, category="hiring", channel="slack", quiet=_quiet_now())
    acct = await _account(tid)
    await _fire(tid, await _signal(tid, acct, kind="hiring"))  # severity: warning

    assert channels["slack"].sent == []


async def test_a_critical_alert_overrides_quiet_hours(channels):
    """The user owns that trade and the default is to be woken for a funding round."""
    tid = await make_tenant(slug="rd6")
    rep = await _user(tid, "rep@rd6.com")
    await _pref(tid, rep, category="funding", channel="slack", quiet=_quiet_now())
    acct = await _account(tid)
    await _fire(tid, await _signal(tid, acct, kind="funding"))  # severity: critical

    assert len(channels["slack"].sent) == 1


# ---- "only my accounts" ----------------------------------------------------------------------------

async def test_only_my_accounts_delivers_for_an_account_i_own(channels):
    tid = await make_tenant(slug="rd7")
    rep = await _user(tid, "rep@rd7.com")
    await _pref(tid, rep, channel="telegram", scope="mine")
    acct = await _account(tid, owner=rep)
    await _fire(tid, await _signal(tid, acct))

    assert len(channels["telegram"].sent) == 1


async def test_only_my_accounts_skips_an_account_someone_else_owns(channels):
    tid = await make_tenant(slug="rd8")
    rep = await _user(tid, "rep@rd8.com")
    other = await _user(tid, "other@rd8.com")
    await _pref(tid, rep, channel="telegram", scope="mine")
    acct = await _account(tid, owner=other)
    await _fire(tid, await _signal(tid, acct))

    assert channels["telegram"].sent == []


async def test_only_my_accounts_skips_an_unowned_account(channels):
    """Unowned is not "mine". Workspace rules are how unowned accounts reach a channel."""
    tid = await make_tenant(slug="rd9")
    rep = await _user(tid, "rep@rd9.com")
    await _pref(tid, rep, channel="telegram", scope="mine")
    acct = await _account(tid, owner=None)
    await _fire(tid, await _signal(tid, acct))

    assert channels["telegram"].sent == []


# ---- workspace rules ---------------------------------------------------------------------------------

async def test_a_workspace_rule_posts_for_everyone(channels):
    """No personal routes at all: the rule alone must be enough."""
    tid = await make_tenant(slug="rd10")
    await _rule(tid, channel="teams", category="funding")
    acct = await _account(tid)
    await _fire(tid, await _signal(tid, acct))

    assert len(channels["teams"].sent) == 1


async def test_my_off_does_not_silence_a_workspace_rule(channels):
    """A rep muting funding for themselves must not mute the team's shared channel."""
    tid = await make_tenant(slug="rd11")
    rep = await _user(tid, "rep@rd11.com")
    await _rule(tid, channel="slack", category="funding")
    await _pref(tid, rep, channel="slack", mode="off")
    acct = await _account(tid)
    await _fire(tid, await _signal(tid, acct))

    assert len(channels["slack"].sent) == 1


# ---- once per channel, and failure isolation -----------------------------------------------------------

async def test_an_alert_posts_to_a_channel_once_however_many_routes_point_there(channels):
    """Slack is shared per workspace. A rule plus two reps routing funding there is ONE post."""
    tid = await make_tenant(slug="rd12")
    a = await _user(tid, "a@rd12.com")
    b = await _user(tid, "b@rd12.com")
    await _rule(tid, channel="slack", category="funding")
    await _pref(tid, a, channel="slack")
    await _pref(tid, b, channel="slack")
    acct = await _account(tid)
    await _fire(tid, await _signal(tid, acct))

    assert len(channels["slack"].sent) == 1


async def test_one_alert_can_reach_several_channels(channels):
    tid = await make_tenant(slug="rd13")
    rep = await _user(tid, "rep@rd13.com")
    await _rule(tid, channel="teams", category="funding")
    await _pref(tid, rep, channel="telegram")
    acct = await _account(tid)
    await _fire(tid, await _signal(tid, acct))

    assert len(channels["teams"].sent) == 1
    assert len(channels["telegram"].sent) == 1


async def test_a_failing_channel_never_loses_the_alert(channels):
    """A channel being down must cost that channel's post, never the alert or the other channels.

    And the recorded failure is REDACTED: a Slack webhook URL is the credential, and httpx writes the
    full URL into the exception.
    """
    from nexus.alerts.channels import AlertChannelRegistry, InAppChannel, set_alert_channels

    broken, teams = _Recorder("slack", fail=True), _Recorder("teams")
    set_alert_channels(AlertChannelRegistry([InAppChannel(), broken, teams]))

    tid = await make_tenant(slug="rd14")
    await _rule(tid, channel="slack", category="funding")
    await _rule(tid, channel="teams", category="funding")
    acct = await _account(tid)
    await _fire(tid, await _signal(tid, acct))

    alerts = await _alerts(tid)
    assert len(alerts) == 1, "a channel failure lost the alert"
    assert len(teams.sent) == 1, "one broken channel stopped delivery to the next"
    deliveries = alerts[0].meta.get("deliveries") or {}
    assert deliveries["slack"]["ok"] is False
    assert deliveries["teams"]["ok"] is True
    assert "SECRETSECRET" not in repr(alerts[0].meta), "the webhook credential was stored in meta"


async def test_nothing_routed_means_nothing_posted_externally(channels):
    """The default is unchanged: with no rule and no route, an alert is in-app only."""
    tid = await make_tenant(slug="rd15")
    acct = await _account(tid)
    await _fire(tid, await _signal(tid, acct))

    assert all(rec.sent == [] for rec in channels.values())
    assert len(await _alerts(tid)) == 1
