"""The signal day window: a superadmin decides whether each user picks one, and what the default is.

Decided with the product owner 2026-09-23. Platform-wide, two runtime settings:

* ``signal_window_user_choice`` ON (default): each user picks a window in the top bar, starting from
  the default. The server honours what the client asks for.
* OFF: no picker, and the SERVER enforces the default on every list — a client that asks for more,
  or for nothing, still gets the superadmin's window. A policy the server does not enforce is a
  preference an old tab or a bookmark can ignore.

The window reaches the Signals list, Inbox and Alerts (and AI drafts, in test_signal_window_ai.py);
an Inbox task or alert with no signal behind it always shows, because the window is about signals.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from nexus.core.config import get_settings
from nexus.core.db import utcnow
from nexus.core.security import decode_access_token
from nexus.models.account import Account
from nexus.models.alerts import Alert
from nexus.models.signal import SignalEvent
from nexus.models.workflow import InboxTask
from tests.conftest import auth, signup, tenant_session


@pytest.fixture
def policy(monkeypatch):
    def set_(*, choice: bool, default: str):
        monkeypatch.setattr(get_settings(), "signal_window_user_choice", choice)
        monkeypatch.setattr(get_settings(), "signal_window_default", default)

    return set_


async def _workspace(client, slug):
    """A workspace with one signal 3 days old and one 60 days old, each with a task and an alert,
    plus a task and an alert that no signal raised."""
    token = await signup(client, slug=slug, email=f"o@{slug}.x", company=slug.upper())
    tid = (decode_access_token(token) or {})["tid"]
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        for label, age in (("recent", 3), ("old", 60)):
            s = SignalEvent(tenant_id=tid, account_id=acc.id, kind="funding", source="rss",
                            title=f"{label} signal", dedupe_key=label, dated="event",
                            occurred_at=utcnow() - timedelta(days=age))
            ts.add(s)
            await ts.flush()
            ts.add(InboxTask(tenant_id=tid, account_id=acc.id, signal_id=s.id,
                             title=f"{label} task", priority=50))
            ts.add(Alert(tenant_id=tid, account_id=acc.id, signal_id=s.id, title=f"{label} alert"))
        ts.add(InboxTask(tenant_id=tid, account_id=acc.id, title="manual task", priority=10))
        ts.add(Alert(tenant_id=tid, account_id=acc.id, title="manual alert"))
    return token


async def _titles(client, token, path):
    r = await client.get(path, headers=auth(token))
    assert r.status_code == 200, r.text
    return sorted(row["title"] for row in r.json())


# ---- the policy the screens read ----------------------------------------------------------------


async def test_the_default_policy_changes_nothing(client, policy):
    # All time, users choose: exactly the behaviour before this existed.
    policy(choice=True, default="all")
    token = await signup(client, slug="sw0", email="o@sw0.x", company="SW0")

    body = (await client.get("/api/signals/window", headers=auth(token))).json()

    assert body["user_choice"] is True
    assert body["default_days"] is None
    assert [o["label"] for o in body["options"]] == [
        "Weekly", "Fortnightly", "Monthly", "Quarterly", "Half-yearly", "Yearly", "All time",
    ]
    assert [o["days"] for o in body["options"]] == [7, 14, 30, 90, 180, 365, None]


async def test_the_screens_learn_the_superadmins_default(client, policy):
    policy(choice=False, default="30")
    token = await signup(client, slug="sw1", email="o@sw1.x", company="SW1")

    body = (await client.get("/api/signals/window", headers=auth(token))).json()

    assert body == {**body, "user_choice": False, "default_days": 30}


# ---- users choose ---------------------------------------------------------------------------------


async def test_when_users_choose_the_server_honours_their_window(client, policy):
    policy(choice=True, default="30")
    token = await _workspace(client, "sw2")

    assert await _titles(client, token, "/api/signals?max_age_days=7") == ["recent signal"]
    # Asking for nothing is "All time" — the user's own choice, not the default.
    assert await _titles(client, token, "/api/signals") == ["old signal", "recent signal"]


# ---- the superadmin decides -----------------------------------------------------------------------


async def test_when_the_choice_is_off_the_default_is_enforced_whatever_the_client_asks(client, policy):
    policy(choice=False, default="14")
    token = await _workspace(client, "sw3")

    for path in ("/api/signals", "/api/signals?max_age_days=90"):
        assert await _titles(client, token, path) == ["recent signal"], path


async def test_the_enforced_window_reaches_the_inbox_and_alerts(client, policy):
    policy(choice=False, default="14")
    token = await _workspace(client, "sw4")

    assert await _titles(client, token, "/api/inbox?status=all") == ["manual task", "recent task"]
    assert await _titles(client, token, "/api/alerts") == ["manual alert", "recent alert"]


async def test_a_chosen_window_reaches_the_inbox_and_alerts(client, policy):
    policy(choice=True, default="all")
    token = await _workspace(client, "sw5")

    assert await _titles(client, token, "/api/inbox?status=all&max_age_days=7") == [
        "manual task", "recent task"]
    assert await _titles(client, token, "/api/alerts?max_age_days=7") == [
        "manual alert", "recent alert"]
    # No window asked for: everything, exactly as before.
    assert await _titles(client, token, "/api/inbox?status=all") == [
        "manual task", "old task", "recent task"]


async def test_all_time_as_the_enforced_default_hides_nothing(client, policy):
    policy(choice=False, default="all")
    token = await _workspace(client, "sw6")

    assert await _titles(client, token, "/api/signals?max_age_days=7") == [
        "old signal", "recent signal"]


# ---- the control plane ----------------------------------------------------------------------------


def test_both_settings_are_in_the_control_plane_under_signals():
    from nexus.runtime_config.catalog import CATALOG, SIGNALS

    choice = CATALOG["signal_window_user_choice"]
    default = CATALOG["signal_window_default"]
    assert choice.group == SIGNALS and choice.kind == "bool"
    assert default.group == SIGNALS and default.kind == "str"
    assert default.options == ("7", "14", "30", "90", "180", "365", "all")
    assert dict(default.option_labels)["all"] == "All time"


@pytest.mark.parametrize("bad", ["15", "60", "forever", ""])
def test_a_window_outside_the_options_is_refused_before_it_is_stored(bad):
    from nexus.runtime_config.service import _VALIDATORS

    with pytest.raises(ValueError):
        _VALIDATORS["signal_window_default"](bad)


def test_an_unreadable_default_fails_open_to_all_time(monkeypatch):
    # Set through the environment rather than the panel, so the validator never ran. A window the
    # reader cannot parse must hide nothing rather than guess.
    from nexus.ingestion.window import default_days

    monkeypatch.setattr(get_settings(), "signal_window_default", "fortnight-ish")
    assert default_days() is None
