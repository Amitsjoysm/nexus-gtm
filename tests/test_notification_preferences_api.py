# tests/test_notification_preferences_api.py
"""Alert delivery preferences, reachable at last.

`notification_preferences` has had a table, a model, a unique constraint and a reader in the digest
sweep since migration 0032 — and no endpoint. Built, stored, unreachable: a customer could not
choose where their alerts go, which is the one thing the table exists to record.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def test_it_returns_the_vocabulary_not_invented_defaults(client):
    """A new user has expressed no preference, and the response says so — an empty list plus the
    catalogue of what CAN be chosen.

    Inventing a row per category would turn "I have not decided" into "I decided the default", and
    the model's contract is that an absent row means the workspace setting still applies.
    """
    token = await signup(client, slug="np1", email="o@np1.com", company="NP1")
    body = (await client.get("/api/notifications", headers=auth(token))).json()

    assert body["preferences"] == []
    assert "funding" in body["categories"] and "hiring" in body["categories"]
    assert "in_app" in body["channels"] and "email" in body["channels"]
    assert body["modes"] == ["immediate", "digest", "off"]


async def test_teams_is_offered_as_a_channel(client):
    """The channels come from the live registry, so a channel that exists is offered even when it
    has no URL configured — it reports "no url configured" rather than vanishing from the UI."""
    token = await signup(client, slug="np2", email="o@np2.com", company="NP2")
    body = (await client.get("/api/notifications", headers=auth(token))).json()
    assert "teams" in body["channels"], body["channels"]
    assert "slack" in body["channels"]


async def test_a_preference_round_trips(client):
    token = await signup(client, slug="np3", email="o@np3.com", company="NP3")
    h = auth(token)

    r = await client.put("/api/notifications", headers=h, json={
        "category": "funding", "channel": "slack", "mode": "immediate",
        "quiet_from_min": 1320, "quiet_to_min": 420, "utc_offset_min": 330,
    })
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "immediate"

    got = (await client.get("/api/notifications", headers=h)).json()["preferences"]
    assert len(got) == 1
    assert got[0]["category"] == "funding" and got[0]["channel"] == "slack"
    # 22:00 -> 07:00 in minutes from local midnight, with a +5:30 offset.
    assert got[0]["quiet_from_min"] == 1320 and got[0]["quiet_to_min"] == 420
    assert got[0]["utc_offset_min"] == 330


async def test_saving_twice_leaves_one_row(client):
    """The unique constraint exists to stop a UI that double-saves producing two contradictory
    preferences and making delivery a coin flip. The endpoint must upsert, not insert."""
    token = await signup(client, slug="np4", email="o@np4.com", company="NP4")
    h = auth(token)
    for mode in ("immediate", "digest"):
        r = await client.put("/api/notifications", headers=h,
                             json={"category": "hiring", "channel": "email", "mode": mode})
        assert r.status_code == 200, r.text

    got = (await client.get("/api/notifications", headers=h)).json()["preferences"]
    assert len(got) == 1, got
    assert got[0]["mode"] == "digest", "the second save did not win"


async def test_an_unknown_category_is_refused(client):
    """Categories are DERIVED from the alert rules, so a category nothing can ever emit cannot be
    subscribed to. A preference for an event that never fires is silence the user believes is a
    setting."""
    token = await signup(client, slug="np5", email="o@np5.com", company="NP5")
    r = await client.put("/api/notifications", headers=auth(token),
                         json={"category": "telepathy", "channel": "email"})
    assert r.status_code == 422, r.text


async def test_half_a_quiet_window_is_refused(client):
    """Accepting a start with no end would silently disable quiet hours for someone who believes
    they configured them."""
    token = await signup(client, slug="np6", email="o@np6.com", company="NP6")
    r = await client.put("/api/notifications", headers=auth(token),
                         json={"category": "funding", "channel": "email", "quiet_from_min": 1320})
    assert r.status_code == 422, r.text


async def test_deleting_is_not_the_same_as_off(client):
    """Both are offered on purpose: `off` means "never send me this", no row means "whatever the
    workspace decides". Collapsing them removes the only way back to the default."""
    token = await signup(client, slug="np7", email="o@np7.com", company="NP7")
    h = auth(token)
    await client.put("/api/notifications", headers=h,
                     json={"category": "news", "channel": "in_app", "mode": "off"})
    assert len((await client.get("/api/notifications", headers=h)).json()["preferences"]) == 1

    r = await client.delete("/api/notifications/news/in_app", headers=h)
    assert r.status_code == 204, r.text
    assert (await client.get("/api/notifications", headers=h)).json()["preferences"] == []


async def test_preferences_are_per_user_not_per_workspace(client):
    """These are a person's OWN delivery settings. One member's choices must not appear in
    another's list, or a rep muting funding alerts mutes them for their colleague too."""
    a = auth(await signup(client, slug="np8", email="a@np8.com", company="NP8"))
    await client.put("/api/notifications", headers=a,
                     json={"category": "funding", "channel": "email", "mode": "digest"})

    b = auth(await signup(client, slug="np9", email="b@np9.com", company="NP9"))
    assert (await client.get("/api/notifications", headers=b)).json()["preferences"] == []


# ---- the client surface --------------------------------------------------------------------------

def test_settings_exposes_alert_delivery():
    """The whole point: before this there was no way for a user to choose where alerts go. A
    reachable API with no screen would be the same defect one layer up."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src"
    settings = (root / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")
    assert "AlertDelivery" in settings, "Settings does not render the alert delivery section"

    panel = (root / "pages" / "settings" / "AlertDelivery.tsx").read_text(encoding="utf-8")
    # Teams is why this was built now.
    assert "teams" in panel.lower()
    # "No preference" must be distinguishable from a chosen default, or the only way back to the
    # workspace setting disappears from the UI.
    assert "Workspace default" in panel
    assert "clearNotificationPreference" in panel
    # Quiet hours are stored as minutes so the overnight wrap is arithmetic; the form must convert.
    assert "quiet_from_min" in panel and "toMinutes" in panel


def test_the_panel_does_not_hardcode_the_vocabulary():
    """Categories, channels and modes come from the server. A hard-coded list drifts the moment a
    channel is added, and the first symptom is a channel nobody can select."""
    import pathlib

    panel = (pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages"
             / "settings" / "AlertDelivery.tsx").read_text(encoding="utf-8")
    assert "data.categories.map" in panel
    assert "data.channels.map" in panel
    assert "data.modes.map" in panel
