# tests/test_runtime_control_plane.py
"""What the Superadmin panel can change, and the guards that keep "saved" meaning "in force".

Reported 2026-09-15: the email verifier URL could not be set from the panel at all, and typing an
IP into the admin allowlist or `apify` into the personalization provider did nothing, because every
string setting without options was drawn as a number box. The fixes add settings, so this file
also pins the two ways an added setting silently lies: nothing reads it, or free text reaches the
table unchecked.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from nexus.core.config import get_settings
from tests.conftest import auth, signup

NEXUS = pathlib.Path("nexus")


@pytest.fixture(autouse=True)
def _restore_settings_and_caches():
    """Same guard as test_runtime_config.py: `set_override` mutates the cached Settings for the
    whole worker process, and monkeypatch cannot undo a mutation made before it snapshots."""
    from nexus.runtime_config import service
    from nexus.runtime_config.catalog import CATALOG

    settings = get_settings()
    before = {k: getattr(settings, k) for k in CATALOG if hasattr(settings, k)}
    service._overridden_here.clear()
    try:
        yield
    finally:
        for key, value in before.items():
            setattr(settings, key, value)
        service._overridden_here.clear()
        service._reset_providers()


async def _superadmin(client, monkeypatch, *, slug: str, email: str) -> str:
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


EMAIL = "Email finding & verification"
CONTACTS = "Contacts & enrichment"
SIGNALS = "Signals & alerts"
AUTOMATION = "Automation & schedules"
OUTREACH = "Outreach & CRM"

# (group, kind, minimum, maximum). Bounds follow what each reader does at the edges; see
# docs/superpowers/specs/2026-09-15-superadmin-control-plane-design.md, section B6.
EXTRAS = {
    "email_finder_max_candidates": (EMAIL, "int", 1, 20),
    "email_reverify_cooldown_days": (EMAIL, "int", 0, 365),
    "campaign_sourced_min_send_confidence": (OUTREACH, "float", 0, 1),
    "personalization_max_posts": ("Personalization", "int", 1, 10),
    "signal_dork_max_queries": (SIGNALS, "int", 0, 10),
    "tenant_daily_source_runs": (SIGNALS, "int", 0, 100000),
    "inbox_min_signal_strength": (SIGNALS, "float", 0, 1),
    "inbox_realert_cooldown_days": (SIGNALS, "int", 0, 90),
    "digest_interval_hours": (SIGNALS, "int", 1, 168),
    "automation_tick_interval_s": (AUTOMATION, "int", 15, 3600),
    "account_refresh_interval_s": (AUTOMATION, "int", 3600, 604800),
    "account_refresh_interval_cold_s": (AUTOMATION, "int", 3600, 2592000),
    "account_hot_signal_window_days": (AUTOMATION, "int", 1, 365),
    "account_refresh_batch_size": (AUTOMATION, "int", 1, 1000),
    "icp_discovery_daily_count": (AUTOMATION, "int", 1, 100),
    "icp_discovery_min_fit": (AUTOMATION, "int", 0, 100),
    "icp_discovery_interval_hours": (AUTOMATION, "int", 1, 168),
    "icp_discovery_enrich_max": (AUTOMATION, "int", 0, 200),
    "lookalike_enrich_max": (CONTACTS, "int", 0, 50),
    "cadence_batch_size": (OUTREACH, "int", 1, 1000),
    "cadence_max_duration_days": (OUTREACH, "int", 1, 365),
    "crm_sync_batch_size": (OUTREACH, "int", 1, 1000),
    "billing_dunning_schedule_days": ("Billing", "str", None, None),
    "email_verify_url": (EMAIL, "str", None, None),
    "email_verify_timeout_s": (EMAIL, "float", 2, 60),
    "phone_lookup_provider": (CONTACTS, "str", None, None),
}


# ---- what the panel offers ----------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(EXTRAS))
def test_each_added_setting_is_in_the_panel_with_its_bounds(key):
    from nexus.runtime_config.catalog import CATALOG

    group, kind, lo, hi = EXTRAS[key]
    spec = CATALOG[key]
    assert (spec.group, spec.kind) == (group, kind)
    assert (spec.minimum, spec.maximum) == (lo, hi)
    assert spec.effect.strip(), f"{key} does not say what changing it does"
    if spec.risk in ("medium", "high"):
        assert spec.warning.strip(), f"{key} is {spec.risk} risk with no warning"
    assert not spec.requires_restart, f"{key} is read per call; it must not claim a restart"


def test_the_personalization_provider_is_a_picker_not_a_text_box():
    from nexus.runtime_config.catalog import CATALOG

    spec = CATALOG["personalization_provider"]
    assert spec.options == ("apify", "stub")
    assert dict(spec.option_labels) == {"apify": "Apify (LinkedIn profile)", "stub": "Off"}


def test_phone_lookup_is_apify_or_off_and_defaults_to_todays_behaviour():
    from nexus.core.config import Settings
    from nexus.runtime_config.catalog import CATALOG

    assert CATALOG["phone_lookup_provider"].options == ("apify", "off")
    assert Settings.model_fields["phone_lookup_provider"].default == "apify"


@pytest.mark.parametrize("key", ["email_verify_auth_header", "billing_support_credit_cap"])
def test_a_credential_and_a_permission_ceiling_stay_out_of_the_panel(key):
    from nexus.runtime_config.catalog import CATALOG, FORBIDDEN
    from nexus.runtime_config.service import UnknownSetting, _spec

    assert key in FORBIDDEN and key not in CATALOG
    with pytest.raises(UnknownSetting, match="deliberately not changeable"):
        _spec(key)


# ---- grouping -----------------------------------------------------------------------------------

def test_every_group_is_one_the_panel_orders_and_none_is_empty():
    from nexus.runtime_config.catalog import CATALOG, GROUP_ORDER

    assert len(set(GROUP_ORDER)) == len(GROUP_ORDER)
    assert {s.group for s in CATALOG.values()} == set(GROUP_ORDER)


async def test_settings_arrive_in_group_order_then_reading_order():
    from nexus.runtime_config.catalog import CATALOG, GROUP_ORDER
    from nexus.runtime_config.service import current_values

    declared = list(CATALOG)
    rows = await current_values()
    assert [r["key"] for r in rows] == sorted(
        declared, key=lambda k: (GROUP_ORDER.index(CATALOG[k].group), declared.index(k))
    )


async def test_the_panel_receives_labels_and_placeholders():
    from nexus.runtime_config.service import current_values

    rows = {r["key"]: r for r in await current_values()}
    assert rows["phone_lookup_provider"]["option_labels"] == {
        "apify": "Apify phone finder", "off": "Off",
    }
    assert rows["email_verify_url"]["placeholder"].startswith("https://")
    assert rows["automation_enabled"]["option_labels"] == {}


# ---- guards against "saved, applied nothing" ----------------------------------------------------

def _source_outside_config() -> str:
    parts = []
    for path in NEXUS.rglob("*.py"):
        rel = path.as_posix()
        if rel.endswith("core/config.py") or "/runtime_config/" in rel:
            continue
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_every_setting_in_the_panel_is_read_by_something():
    """A setting nothing reads saves, reports "in effect" and changes nothing. Two per-task search
    pickers shipped exactly like that and were only found by reading the code."""
    from nexus.runtime_config.catalog import CATALOG
    from nexus.runtime_config.service import _EXTERNAL_SINKS

    source = _source_outside_config()
    config = (NEXUS / "core/config.py").read_text(encoding="utf-8")
    # A derived property on Settings (`contact_search_source_list` over `contact_search_sources`)
    # counts as a reader when something outside config.py reads the property.
    properties: dict[str, list[str]] = {}
    for name, body in re.findall(
        r"@property\s*\n\s*def (\w+)\(self\)[^\n]*:\n((?:[ \t]+[^\n]*\n|\n)+?)(?=\s*(?:@|def |\Z))",
        config,
    ):
        for key in re.findall(r"self\.(\w+)", body):
            properties.setdefault(key, []).append(name)

    def is_read(key: str) -> bool:
        names = [key, *properties.get(key, [])]
        return any(
            re.search(rf"\.{name}\b|getattr\([^\n]*[\"']{name}[\"']", source) for name in names
        )

    unread = [key for key in CATALOG if key not in _EXTERNAL_SINKS and not is_read(key)]
    assert not unread, f"nothing outside config.py reads: {unread}"


def test_a_free_text_setting_is_validated_before_it_is_stored():
    """`coerce` checks only the declared kind, which for a string is no check at all."""
    from nexus.runtime_config.catalog import CATALOG
    from nexus.runtime_config.service import _VALIDATORS

    unchecked = [
        s.key for s in CATALOG.values()
        if s.kind == "str" and not s.options and s.key not in _VALIDATORS
    ]
    assert not unchecked, f"free text with no validator: {unchecked}"


# ---- the verifier URL ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "ftp://verifier.example.com/v0/check_email",
    "https:///v0/check_email",
    "verifier.example.com/v0/check_email",
    "http://169.254.169.254/latest/meta-data",
    "http://metadata.google.internal/v0/check_email",
    "http://127.0.0.1:8080/v0/check_email",
    "http://10.0.0.5:8080/v0/check_email",
    "http://158.69.113.104:99999/v0/check_email",
])
def test_the_verifier_url_refuses_somewhere_we_must_not_send(monkeypatch, url):
    """The verifier POSTs to this URL and the check reports how it answered, which is a port
    scanner if the URL can point inside the network."""
    from nexus.runtime_config.service import _VALIDATORS

    monkeypatch.setattr(get_settings(), "env", "prod")
    with pytest.raises(ValueError):
        _VALIDATORS["email_verify_url"](url)


@pytest.mark.parametrize("url", [
    "http://158.69.113.104:8080/v0/check_email",
    "https://158.69.113.104/v0/check_email",
])
def test_the_verifier_url_accepts_a_public_address(monkeypatch, url):
    from nexus.runtime_config.service import _VALIDATORS

    monkeypatch.setattr(get_settings(), "env", "prod")
    _VALIDATORS["email_verify_url"](url)


def test_a_private_verifier_is_allowed_on_a_local_stack_but_metadata_never_is(monkeypatch):
    from nexus.runtime_config.service import _VALIDATORS

    monkeypatch.setattr(get_settings(), "env", "local")
    _VALIDATORS["email_verify_url"]("http://reacher:8080/v0/check_email")
    with pytest.raises(ValueError):
        _VALIDATORS["email_verify_url"]("http://metadata.google.internal/v0/check_email")


async def test_a_refused_verifier_url_is_never_stored(monkeypatch):
    from nexus.runtime_config.service import set_override, stored_overrides

    monkeypatch.setattr(get_settings(), "env", "prod")
    with pytest.raises(ValueError):
        await set_override("email_verify_url", "http://127.0.0.1:8080/v0/check_email")
    assert "email_verify_url" not in await stored_overrides()


@pytest.mark.parametrize(("key", "value"), [
    ("email_verify_url", "https://158.69.113.104/v0/check_email"),
    ("email_verify_timeout_s", 7.5),
])
async def test_changing_the_verifier_rebuilds_it_without_a_restart(monkeypatch, key, value):
    """Reacher copies its URL and timeout at construction, so a bare setattr would change nothing
    until a restart."""
    from nexus.integrations.registry import get_registry
    from nexus.runtime_config.service import set_override

    monkeypatch.setattr(get_settings(), "env", "prod")
    monkeypatch.setattr(get_settings(), "email_verify_provider", "reacher")
    before = get_registry()
    await set_override(key, value)
    after = get_registry()
    assert after is not before, "the cached verifier survived the change"
    verifier = after.email_verifier
    assert (verifier.url if key == "email_verify_url" else verifier.timeout) == value


# ---- the dunning schedule -----------------------------------------------------------------------

@pytest.mark.parametrize("value", ["1,3,7", " 2, 5 ", "14"])
def test_a_sensible_dunning_schedule_is_accepted(value):
    from nexus.runtime_config.service import _VALIDATORS

    _VALIDATORS["billing_dunning_schedule_days"](value)


@pytest.mark.parametrize("value", ["", "0,3", "1,a", "61", "1,2,3,4,5,6,7,8,9,10,11"])
def test_a_dunning_schedule_the_reader_would_ignore_is_refused(value):
    """`dunning._schedule` quietly falls back to the default on a bad value, so an unchecked typo
    would read as saved while the old schedule kept running."""
    from nexus.runtime_config.service import _VALIDATORS

    with pytest.raises(ValueError):
        _VALIDATORS["billing_dunning_schedule_days"](value)


# ---- Check connection ---------------------------------------------------------------------------

async def test_a_workspace_owner_cannot_run_the_verifier_check(client):
    token = await signup(client, slug="cp1", email="o@cp1.com", company="CP1")
    r = await client.post("/api/admin/runtime/email-verifier/check", headers=auth(token))
    assert r.status_code == 404


async def test_the_verifier_check_reports_what_is_in_force(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="cp2", email="boss@cp2.com")
    monkeypatch.setattr(get_settings(), "email_verify_provider", "dns")
    r = await client.post("/api/admin/runtime/email-verifier/check", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "degraded"
    assert body["provider"] == "dns"
    assert body["detail"]
    assert body["url"] == ""
