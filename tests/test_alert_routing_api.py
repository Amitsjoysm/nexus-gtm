"""Who may connect a shared alert channel, who may say what it receives, and "only my accounts".

Found 2026-09-10 when an SDR could not get alerts on Slack, Teams or Telegram at all. Decided with
the product owner:

* connecting a channel is MANAGER and up (was owner/admin). A workspace has one Slack, so a rep
  replacing the webhook would redirect the whole team's alerts; a team lead doing it is normal.
* a manager decides which alert types a channel receives for everyone — a workspace rule;
* any member routes their own alerts to a connected channel, scoped to all alerts or only accounts
  they own.

Telegram is used for connecting throughout because it has no URL: the webhook channels resolve DNS
through the SSRF guard, and these tests must not depend on the network.
"""
from __future__ import annotations

from tests.conftest import auth, signup

TELEGRAM = {"bot_token": "123456789:AAtesttesttesttesttesttesttest0000", "chat_id": "42"}


async def _member(client, owner_h: dict, slug: str, role: str) -> dict:
    email = f"{role}@{slug}.com"
    invite = await client.post("/api/workspace/members", headers=owner_h, json={
        "email": email, "full_name": role.title(), "role": role, "password": "password123"})
    assert invite.status_code in (200, 201), invite.text
    login = await client.post("/api/auth/login", json={"email": email, "password": "password123"})
    return auth(login.json()["access_token"])


def _channel(body: dict, kind: str) -> dict:
    return next(c for c in body["channels"] if c["kind"] == kind)


# ---- connecting: manager and up ------------------------------------------------------------------

async def test_a_manager_can_connect_a_channel(client):
    owner = auth(await signup(client, slug="ar1", email="o@ar1.com", company="AR1"))
    manager = await _member(client, owner, "ar1", "manager")

    r = await client.put("/api/alert-connections/telegram", headers=manager, json=TELEGRAM)
    assert r.status_code == 200, r.text
    assert r.json()["connected"] is True


async def test_a_manager_can_test_and_disconnect_a_channel(client):
    owner = auth(await signup(client, slug="ar2", email="o@ar2.com", company="AR2"))
    manager = await _member(client, owner, "ar2", "manager")
    await client.put("/api/alert-connections/telegram", headers=manager, json=TELEGRAM)

    assert (await client.post("/api/alert-connections/telegram/test",
                              headers=manager)).status_code == 200
    assert (await client.delete("/api/alert-connections/telegram",
                                headers=manager)).status_code == 204


async def test_a_rep_still_cannot_connect_a_channel(client):
    """One Slack per workspace: a rep replacing it would redirect every teammate's alerts."""
    owner = auth(await signup(client, slug="ar3", email="o@ar3.com", company="AR3"))
    rep = await _member(client, owner, "ar3", "rep")

    assert (await client.put("/api/alert-connections/telegram", headers=rep,
                             json=TELEGRAM)).status_code == 403
    assert (await client.delete("/api/alert-connections/telegram",
                                headers=rep)).status_code == 403
    # ...but can always SEE what is connected, because routing to a channel is meaningless without
    # knowing whether it exists.
    assert (await client.get("/api/alert-connections", headers=rep)).status_code == 200


# ---- workspace rules: which alert types a channel receives ------------------------------------------

async def test_a_manager_sets_which_alert_types_a_channel_receives(client):
    owner = auth(await signup(client, slug="ar4", email="o@ar4.com", company="AR4"))
    manager = await _member(client, owner, "ar4", "manager")

    r = await client.put("/api/alert-connections/slack/rules", headers=manager,
                         json={"categories": ["funding", "hiring"]})
    assert r.status_code == 200, r.text
    assert sorted(r.json()["categories"]) == ["funding", "hiring"]

    listed = (await client.get("/api/alert-connections", headers=manager)).json()
    assert sorted(_channel(listed, "slack")["categories"]) == ["funding", "hiring"]
    assert _channel(listed, "teams")["categories"] == []


async def test_saving_rules_replaces_the_set(client):
    """The form posts the whole set. Saving twice must not accumulate, and unticking must remove."""
    owner = auth(await signup(client, slug="ar5", email="o@ar5.com", company="AR5"))
    await client.put("/api/alert-connections/slack/rules", headers=owner,
                     json={"categories": ["funding", "hiring"]})
    r = await client.put("/api/alert-connections/slack/rules", headers=owner,
                         json={"categories": ["hiring"]})
    assert r.json()["categories"] == ["hiring"]


async def test_a_rule_for_a_category_nothing_emits_is_refused(client):
    """A rule for an alert type that never fires is silence a manager believes is a setting."""
    owner = auth(await signup(client, slug="ar6", email="o@ar6.com", company="AR6"))
    r = await client.put("/api/alert-connections/slack/rules", headers=owner,
                         json={"categories": ["not_a_real_category"]})
    assert r.status_code == 422


async def test_a_rep_can_read_but_not_set_workspace_rules(client):
    owner = auth(await signup(client, slug="ar7", email="o@ar7.com", company="AR7"))
    rep = await _member(client, owner, "ar7", "rep")
    await client.put("/api/alert-connections/slack/rules", headers=owner,
                     json={"categories": ["funding"]})

    assert (await client.put("/api/alert-connections/slack/rules", headers=rep,
                             json={"categories": []})).status_code == 403
    listed = (await client.get("/api/alert-connections", headers=rep)).json()
    assert _channel(listed, "slack")["categories"] == ["funding"]


# ---- personal routes: scope --------------------------------------------------------------------------

async def test_a_route_can_be_scoped_to_my_accounts(client):
    owner = auth(await signup(client, slug="ar8", email="o@ar8.com", company="AR8"))
    rep = await _member(client, owner, "ar8", "rep")

    r = await client.put("/api/notifications", headers=rep, json={
        "category": "funding", "channel": "slack", "mode": "immediate", "scope": "mine"})
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "mine"
    prefs = (await client.get("/api/notifications", headers=rep)).json()["preferences"]
    assert prefs[0]["scope"] == "mine"


async def test_a_new_route_defaults_to_all_alerts(client):
    owner = auth(await signup(client, slug="ar9", email="o@ar9.com", company="AR9"))
    r = await client.put("/api/notifications", headers=owner, json={
        "category": "funding", "channel": "slack", "mode": "immediate"})
    assert r.json()["scope"] == "all"


async def test_an_unknown_scope_is_refused(client):
    owner = auth(await signup(client, slug="ar10", email="o@ar10.com", company="AR10"))
    r = await client.put("/api/notifications", headers=owner, json={
        "category": "funding", "channel": "slack", "mode": "immediate", "scope": "everyone"})
    assert r.status_code == 422


async def test_changing_a_routes_timing_keeps_its_scope(client):
    """An edit that omits scope must not silently widen "only my accounts" back to everything —
    which is what a client written before scope existed would otherwise do on every save."""
    owner = auth(await signup(client, slug="ar11", email="o@ar11.com", company="AR11"))
    await client.put("/api/notifications", headers=owner, json={
        "category": "funding", "channel": "slack", "mode": "immediate", "scope": "mine"})
    r = await client.put("/api/notifications", headers=owner, json={
        "category": "funding", "channel": "slack", "mode": "digest"})
    assert r.json()["mode"] == "digest"
    assert r.json()["scope"] == "mine"
