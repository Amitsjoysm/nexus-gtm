# tests/test_alert_setup_ui.py
"""The alert setup screens must not drift from the server that answers them.

Routing an alert and being able to receive it are two different facts, and the old screen showed
only the first: a grid of every category crossed with every channel, all of it settable, none of it
saying whether Slack had ever been connected. Somebody could set "funding -> Slack" and hear nothing
forever. That is the same failure mode this codebase keeps finding — configured and delivering
nothing, indistinguishable from a quiet week.

There is no frontend test runner here, so these read the source, the same approach as
`test_the_dropdown_offers_exactly_the_providers_the_server_accepts` and the nav/route guard tests.
What they pin is only the places where a UI copy of a server list would silently rot.
"""
from __future__ import annotations

import pathlib
import re

import pytest

FRONTEND = pathlib.Path("frontend/src")
VOCAB = FRONTEND / "components/alerts/vocabulary.ts"
FORM = FRONTEND / "components/alerts/ConnectChannelForm.tsx"
PANEL = FRONTEND / "components/alerts/AlertChannelsCard.tsx"
SETUP = FRONTEND / "pages/settings/AlertDelivery.tsx"


def _read(path: pathlib.Path) -> str:
    assert path.exists(), f"{path} is missing — was it moved?"
    return path.read_text(encoding="utf-8")


# ---- the channel list ----------------------------------------------------------------------------

def test_the_ui_offers_a_connect_form_for_exactly_the_channels_the_server_stores():
    """A channel the server can store and the UI never offers to connect is a credential nobody can
    add; one the UI offers and the server rejects is a form that 404s on submit.

    `ALERT_CHANNEL_KINDS` is what `PUT /alert-connections/{kind}` validates against, so it is the
    list that matters.
    """
    from nexus.models.integration import ALERT_CHANNEL_KINDS

    block = re.search(
        r"CONNECTABLE_CHANNELS[^=]*=\s*\[(.*?)\]", _read(VOCAB), re.S
    )
    assert block, "CONNECTABLE_CHANNELS not found — was it renamed?"
    offered = set(re.findall(r'"([a-z_]+)"', block.group(1)))

    assert offered == set(ALERT_CHANNEL_KINDS), (
        f"the UI connects {sorted(offered)}, the server stores {sorted(ALERT_CHANNEL_KINDS)}"
    )


def test_the_webhook_channel_is_not_offered_as_a_connectable_account():
    """`WebhookChannel` is built from `alert_webhook_url`, a deployment setting an operator wires —
    there is no per-tenant row for it and `save_connection` refuses the kind. Offering a connect
    form would promise something the screen cannot deliver."""
    from nexus.models.integration import ALERT_CHANNEL_KINDS

    assert "webhook" not in ALERT_CHANNEL_KINDS
    block = re.search(r"CONNECTABLE_CHANNELS[^=]*=\s*\[(.*?)\]", _read(VOCAB), re.S)
    assert block and "webhook" not in block.group(1)


def test_every_channel_the_ui_can_connect_tells_the_user_where_the_credential_comes_from():
    """"Create a webhook" is not an instruction anybody can follow without knowing where. A connect
    form with no path to the credential is a dead end wearing a text input."""
    src = _read(VOCAB)
    block = re.search(r"CHANNEL_HELP[^=]*=\s*\{(.*?)\n\};", src, re.S)
    assert block, "CHANNEL_HELP not found — was it renamed?"
    documented = set(re.findall(r"^\s*([a-z_]+):", block.group(1), re.M))

    from nexus.models.integration import ALERT_CHANNEL_KINDS

    missing = set(ALERT_CHANNEL_KINDS) - documented
    assert not missing, f"no setup instructions for {sorted(missing)}"


# ---- the fields ------------------------------------------------------------------------------------

def test_the_connect_form_renders_the_fields_the_server_declares():
    """`CHANNEL_FIELDS` in `nexus/alerts/connections.py` is read by the API, the resolver AND the
    connection-state reporter. A form carrying its own copy is how Telegram ends up storing a bot
    token with no chat id: a row that reads connected and delivers nothing, because a sender with no
    destination is not a connection.
    """
    src = _read(FORM)
    assert "channel.fields.map" in src, (
        "the connect form no longer renders the server-declared fields; a hard-coded list here "
        "cannot notice a channel that starts needing a second field"
    )


def test_the_connect_form_carries_no_hard_coded_channel_field_list():
    """The complement of the test above: a `switch (kind)` or a per-channel field map would be the
    second source of truth, and the first thing to drift would be which fields are required."""
    src = _read(FORM)
    for leak in ("bot_token", "chat_id"):
        # These names may appear in the shared FIELD_META (labels for a field name the server sent),
        # but the FORM itself must not decide which channel needs which.
        assert f'"{leak}"' not in src, (
            f"{FORM} names {leak} directly — the field list belongs to the server"
        )


def test_every_field_the_server_can_ask_for_has_a_label():
    """A field rendered with its raw key ("bot_token") as the label is a form asking for something
    nobody outside this repo can name."""
    from nexus.alerts.connections import CHANNEL_FIELDS

    src = _read(VOCAB)
    block = re.search(r"FIELD_META[^=]*=\s*\{(.*?)\n\};", src, re.S)
    assert block, "FIELD_META not found — was it renamed?"
    labelled = set(re.findall(r"^\s{2}([a-z_]+):\s*\{", block.group(1), re.M))

    needed = {f for fields in CHANNEL_FIELDS.values() for f in fields}
    missing = needed - labelled
    assert not missing, f"no label for {sorted(missing)}"


# ---- the gate --------------------------------------------------------------------------------------

def test_the_setup_flow_gates_on_the_live_connection_state():
    """THE property of this screen. The step that asks for a credential must appear because the
    SERVER says the channel is unconnected, not because of anything the client decided.

    Without it the flow saves a routing preference for a channel that has never been connected,
    which is the old grid's bug with a nicer layout.
    """
    src = _read(SETUP)
    assert "alertConnections" in src, "the setup flow never reads the connection state"
    assert "needsConnection" in src and "isConnectable" in src, (
        "the connect step is no longer gated on the channel being connectable and unconnected"
    )
    assert "ConnectChannelForm" in src, "there is no way to connect from inside the flow"


def test_a_failed_connections_read_does_not_block_routing():
    """Same bias as the entitlements engine and the alert resolver: a lookup we could not perform
    must not delete somebody's ability to act. The connection state improves the flow; it is not
    what the flow is for.
    """
    src = _read(SETUP)
    gate = re.search(r"const needsConnection\s*=\s*(.*?);", src, re.S)
    assert gate, "needsConnection not found — was it renamed?"
    assert "conn !== null" in gate.group(1), (
        "a null connection (the read failed, or the channel is unknown) must fall through to "
        f"allowing the route, not block it: {gate.group(1).strip()}"
    )


def test_connecting_is_admin_gated_in_the_ui_as_well_as_the_server():
    """`PUT /alert-connections/{kind}` is `manage_workspace`. A rep handed the form gets a 403 on
    submit and no idea who to ask, so the UI shows what is missing and who can fix it instead."""
    from nexus.api.routers import alert_connections as router_mod

    src = _read(pathlib.Path(router_mod.__file__))
    assert "Permission.manage_workspace" in src, "connecting is no longer workspace-admin gated"

    form = _read(FORM)
    assert "canManage" in form and "if (!canManage)" in form, (
        "the connect form no longer has a non-admin branch"
    )


# ---- the vocabulary --------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [SETUP, PANEL])
def test_the_screens_share_one_vocabulary(path: pathlib.Path):
    """Two screens naming one credential two different ways is how "Teams" and "MS Teams" become
    two things in a customer's head. Both import the labels rather than spelling them again."""
    src = _read(path)
    assert "components/alerts/vocabulary" in src or "./vocabulary" in src, (
        f"{path} spells the channel names itself instead of importing them"
    )


def test_every_alert_category_a_user_can_choose_has_a_readable_name():
    """The dropdown is the first place anybody meets these names, and "technographic" means nothing
    on its own. `ALERT_CATEGORIES` is derived from the alert rules, so a category added there
    appears in the dropdown whether or not anybody labelled it."""
    from nexus.alerts.rules import ALERT_CATEGORIES

    src = _read(VOCAB)
    block = re.search(r"CATEGORY_LABEL[^=]*=\s*\{(.*?)\n\};", src, re.S)
    assert block, "CATEGORY_LABEL not found — was it renamed?"
    labelled = set(re.findall(r"^\s{2}([a-z_]+):", block.group(1), re.M))

    missing = set(ALERT_CATEGORIES) - labelled
    assert not missing, f"no label for alert categories {sorted(missing)}"


def test_every_alert_category_says_what_fires_it():
    """A name alone does not tell a rep whether to subscribe. `CATEGORY_BLURB` is what turns the
    dropdown from a list of jargon into a decision somebody can make."""
    from nexus.alerts.rules import ALERT_CATEGORIES

    src = _read(VOCAB)
    block = re.search(r"CATEGORY_BLURB[^=]*=\s*\{(.*?)\n\};", src, re.S)
    assert block, "CATEGORY_BLURB not found — was it renamed?"
    described = set(re.findall(r"^\s{2}([a-z_]+):", block.group(1), re.M))

    missing = set(ALERT_CATEGORIES) - described
    assert not missing, f"no description for alert categories {sorted(missing)}"


def test_the_panel_never_renders_a_secret():
    """`ChannelOut` carries no secret, and the panel must not invent a place to show one. A screen
    that can display a credential leaks it through a screenshot or a support session."""
    src = _read(PANEL)
    for leak in ("secret", "bot_token", "webhook_url"):
        assert leak not in src, f"{PANEL} references {leak}"
