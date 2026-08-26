# tests/test_runtime_config.py
"""Changing deployment settings from the Control plane, safely.

The mechanism: `get_settings()` is an `lru_cache` over a MUTABLE object, so an override applied with
`setattr` reaches all 142 call sites without any of them changing. The alternative — a resolver each
call site adopts — would have been 142 opportunities to miss one and leave a setting that silently
ignores the panel while reading "off".

The catalog is an allowlist, and its exclusions carry more weight than its inclusions.
"""
from __future__ import annotations

from tests.conftest import auth, signup


async def _superadmin(client, monkeypatch, *, slug: str, email: str) -> str:
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


# ---- what it refuses -----------------------------------------------------------------------------

async def test_the_forbidden_settings_are_never_settable(client, monkeypatch):
    """The exclusions are the point of the catalog.

    `source_db_allow_private` is the SSRF guard on external source databases: an admin must not be
    able to switch off the guard from the form the guard protects. `security_headers_enabled` and
    `auth_rate_limit_enabled` are the same argument aimed at the web surface itself.
    `demo_signals_enabled` would put fabricated signals into a real inbox, which is the failure the
    ingestion subsystem was rebuilt to prevent.
    """
    from nexus.runtime_config.catalog import CATALOG, FORBIDDEN

    token = await _superadmin(client, monkeypatch, slug="rc1", email="boss@rc1.com")
    for key in FORBIDDEN:
        assert key not in CATALOG, f"{key} must never be in the catalog"
        r = await client.put(f"/api/admin/runtime/settings/{key}", headers=auth(token),
                             json={"value": True})
        assert r.status_code == 404, key
        # Told it is withheld on purpose, not that they mistyped — an operator hunting for a
        # setting they know exists deserves the real reason.
        assert "deliberately not changeable" in r.text


async def test_an_unknown_setting_is_a_404(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="rc2", email="boss@rc2.com")
    r = await client.put("/api/admin/runtime/settings/not_a_setting", headers=auth(token),
                         json={"value": 1})
    assert r.status_code == 404


async def test_a_tenant_owner_cannot_reach_it(client):
    """Several of these toggles decide whether the platform spends money unattended."""
    token = await signup(client, slug="rc3", email="o@rc3.com", company="RC3")
    assert (await client.get("/api/admin/runtime/settings",
                             headers=auth(token))).status_code == 403


async def test_a_string_false_does_not_switch_something_on(client, monkeypatch):
    """The coercion trap, and it is not hypothetical.

    The panel posts JSON, so a boolean can arrive as the string "false" — which is truthy in
    Python. Assigned straight through, it would switch a setting ON while the operator watched the
    control read "off".
    """
    from nexus.core.config import get_settings

    token = await _superadmin(client, monkeypatch, slug="rc4", email="boss@rc4.com")
    r = await client.put("/api/admin/runtime/settings/cadence_enabled", headers=auth(token),
                         json={"value": "false"})
    assert r.status_code == 200, r.text
    assert r.json()["value"] is False
    assert get_settings().cadence_enabled is False

    bad = await client.put("/api/admin/runtime/settings/cadence_enabled", headers=auth(token),
                           json={"value": "maybe"})
    assert bad.status_code == 400


async def test_a_number_outside_its_range_is_refused(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="rc5", email="boss@rc5.com")
    r = await client.put("/api/admin/runtime/settings/signal_alert_floor", headers=auth(token),
                         json={"value": 5})
    assert r.status_code == 400
    assert "above" in r.text


async def test_a_value_outside_the_option_list_is_refused(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="rc6", email="boss@rc6.com")
    r = await client.put("/api/admin/runtime/settings/billing_enforcement", headers=auth(token),
                         json={"value": "sometimes"})
    assert r.status_code == 400


# ---- what it does --------------------------------------------------------------------------------

async def test_setting_an_override_changes_the_live_settings_object(client, monkeypatch):
    """The whole mechanism in one assertion: the running application sees the new value."""
    from nexus.core.config import get_settings

    token = await _superadmin(client, monkeypatch, slug="rc7", email="boss@rc7.com")
    monkeypatch.setattr(get_settings(), "cadence_enabled", False)

    await client.put("/api/admin/runtime/settings/cadence_enabled", headers=auth(token),
                     json={"value": True, "note": "pilot customer signed off"})
    assert get_settings().cadence_enabled is True


async def test_an_override_survives_into_a_fresh_process(client, monkeypatch):
    """`apply_overrides` is what a worker runs on its own TTL. Simulated by clearing the value and
    re-applying from storage, which is exactly what a separate process does at boot."""
    from nexus.core.config import get_settings
    from nexus.runtime_config.service import apply_overrides

    token = await _superadmin(client, monkeypatch, slug="rc8", email="boss@rc8.com")
    await client.put("/api/admin/runtime/settings/cadence_enabled", headers=auth(token),
                     json={"value": True})

    monkeypatch.setattr(get_settings(), "cadence_enabled", False)   # a process that never saw it
    applied = await apply_overrides()
    assert applied.get("cadence_enabled") is True
    assert get_settings().cadence_enabled is True


async def test_clearing_removes_the_override(client, monkeypatch):
    token = await _superadmin(client, monkeypatch, slug="rc9", email="boss@rc9.com")
    await client.put("/api/admin/runtime/settings/cadence_enabled", headers=auth(token),
                     json={"value": True})

    r = await client.delete("/api/admin/runtime/settings/cadence_enabled", headers=auth(token))
    assert r.status_code == 200
    assert r.json()["overridden"] is False

    listed = (await client.get("/api/admin/runtime/settings", headers=auth(token))).json()
    row = next(x for x in listed if x["key"] == "cadence_enabled")
    assert row["overridden"] is False

    again = await client.delete("/api/admin/runtime/settings/cadence_enabled", headers=auth(token))
    assert again.status_code == 404, "clearing what is not overridden is not a success"


async def test_a_failed_read_leaves_the_process_on_its_environment_values(monkeypatch, fresh_db):
    """A configuration read that fails must never take down an application whose settings were
    perfectly fine a second ago."""
    from nexus.runtime_config import service

    async def boom():
        raise RuntimeError("database is having a moment")

    monkeypatch.setattr(service, "stored_overrides", boom)
    assert await service.apply_overrides() == {}


async def test_a_row_for_a_setting_no_longer_in_the_catalog_is_ignored(fresh_db):
    """Removing a setting from the panel must actually remove its effect — otherwise it keeps
    applying from a row nobody can see any more."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.runtime_setting import RuntimeSetting
    from nexus.runtime_config.service import apply_overrides

    async with get_platform_sessionmaker()() as s:
        s.add(RuntimeSetting(key="retired_setting", value="True"))
        await s.commit()

    assert "retired_setting" not in await apply_overrides()


# ---- what the operator is told --------------------------------------------------------------------

async def test_every_setting_explains_itself(client, monkeypatch):
    """A toggle whose result nobody can state in a sentence is a trap, not a feature. Anything
    that costs money or turns something off must also carry a warning."""
    token = await _superadmin(client, monkeypatch, slug="rc10", email="boss@rc10.com")
    rows = (await client.get("/api/admin/runtime/settings", headers=auth(token))).json()
    assert rows

    for row in rows:
        assert row["effect"].strip(), f"{row['key']} has no stated effect"
        assert row["risk"] in ("low", "medium", "high")
        if row["risk"] in ("medium", "high"):
            assert row["warning"].strip(), f"{row['key']} is {row['risk']} risk with no warning"


async def test_changing_a_setting_is_audited_with_before_and_after(client, monkeypatch):
    """Six months later, "who turned this on and what did they think it did" is the only question
    that matters."""
    from sqlalchemy import select

    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.billing import BillingAuditLog

    token = await _superadmin(client, monkeypatch, slug="rc11", email="boss@rc11.com")
    await client.put("/api/admin/runtime/settings/phone_enrich_auto", headers=auth(token),
                     json={"value": True, "note": "approved by finance for the Q4 push"})

    async with get_platform_sessionmaker()() as s:
        rows = list((await s.scalars(select(BillingAuditLog))).all())
    entry = next(r for r in rows if r.action == "runtime_setting.set")
    assert entry.target == "phone_enrich_auto"
    assert "approved by finance" in (entry.note or "")
    assert (entry.before or {}).get("value") is False
    assert (entry.after or {}).get("value") is True


# ---- the webhook surface --------------------------------------------------------------------------

async def test_the_webhook_page_says_what_to_paste_into_stripe(client, monkeypatch):
    """The URL is not ours to set — it goes in the Stripe dashboard. What the panel can do is stop
    an operator guessing it, and say whether our side is ready to receive."""
    token = await _superadmin(client, monkeypatch, slug="rc12", email="boss@rc12.com")
    r = await client.get("/api/admin/runtime/webhook", headers=auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "/api/billing/webhooks/stripe"
    assert body["instructions"]
    # The events must be listed: Stripe sends everything otherwise, and each unhandled type is a
    # rejected delivery sitting in the customer's dashboard looking like a fault.
    assert "invoice.paid" in body["events_handled"]
    assert "checkout.session.completed" in body["events_handled"]
    assert body["signing_secret_source"] in ("control plane", "environment", "not set")


async def test_the_webhook_test_says_so_when_no_secret_is_configured(client, monkeypatch):
    """Rather than reporting a failure that sends an operator looking at the network."""
    from nexus.core.config import get_settings

    token = await _superadmin(client, monkeypatch, slug="rc13", email="boss@rc13.com")
    monkeypatch.setattr(get_settings(), "stripe_webhook_secret", "")
    r = await client.post("/api/admin/runtime/webhook/test", headers=auth(token), json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "signing secret" in body["detail"].lower()


async def test_a_tenant_owner_cannot_probe_the_webhook(client):
    token = await signup(client, slug="rc14", email="o@rc14.com", company="RC14")
    assert (await client.get("/api/admin/runtime/webhook",
                             headers=auth(token))).status_code == 403


# ---- the wiring ------------------------------------------------------------------------------------

async def test_a_dispatched_job_picks_up_a_changed_setting(fresh_db, monkeypatch):
    """The worker is a separate process with its own `Settings` singleton, so nothing the API does
    reaches it. Every dispatched job refreshes on the TTL — put in `dispatch` because it is the one
    funnel every job goes through.
    """
    from nexus.core.config import get_settings
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.runtime_setting import RuntimeSetting
    from nexus.runtime_config import service
    from nexus.workers.tasks import Job, dispatch

    async with get_platform_sessionmaker()() as s:
        s.add(RuntimeSetting(key="cadence_enabled", value="True"))
        await s.commit()

    # A process that has never seen the override, with the TTL clock reset so the next call sweeps.
    monkeypatch.setattr(get_settings(), "cadence_enabled", False)
    monkeypatch.setattr(service, "_applied_at", 0.0)

    await dispatch(Job(name="definitely_not_a_real_job", payload={}))
    assert get_settings().cadence_enabled is True


async def test_a_broken_config_read_does_not_stop_a_job(fresh_db, monkeypatch):
    """A configuration read that fails must not stop work from running. The job is the thing the
    customer is waiting on; the setting refresh is bookkeeping attached to it."""
    from nexus.runtime_config import service
    from nexus.workers.tasks import Job, dispatch

    async def boom(*a, **k):
        raise RuntimeError("database is having a moment")

    monkeypatch.setattr(service, "refresh_if_stale", boom)
    result = await dispatch(Job(name="definitely_not_a_real_job", payload={}))
    assert result["error"] == "unknown_job"      # reached the handler lookup regardless


# ---- has it actually taken effect? ---------------------------------------------------------------

async def test_a_setting_reports_whether_it_has_taken_effect(client, monkeypatch):
    """"Saved" and "in force" are different facts, and a panel showing only the first is how an
    operator concludes a feature is on when it is not."""
    token = await _superadmin(client, monkeypatch, slug="rc20", email="boss@rc20.com")
    await client.put("/api/admin/runtime/settings/cadence_enabled", headers=auth(token),
                     json={"value": True})

    rows = (await client.get("/api/admin/runtime/settings", headers=auth(token))).json()
    row = next(r for r in rows if r["key"] == "cadence_enabled")
    assert row["overridden"] is True
    assert row["in_effect"] is True, "set on this process, so it is live here"


async def test_a_stored_value_the_process_has_not_picked_up_reads_as_pending(client, monkeypatch):
    """A restart-only setting is stored and pending. Saying it is live would be a lie the operator
    only discovers when the thing they enabled is still missing."""
    from nexus.core.config import get_settings

    token = await _superadmin(client, monkeypatch, slug="rc21", email="boss@rc21.com")
    await client.put("/api/admin/runtime/settings/metrics_enabled", headers=auth(token),
                     json={"value": False})
    # A process that read the value at boot and still holds the old one.
    monkeypatch.setattr(get_settings(), "metrics_enabled", True)

    rows = (await client.get("/api/admin/runtime/settings", headers=auth(token))).json()
    row = next(r for r in rows if r["key"] == "metrics_enabled")
    assert row["overridden"] is True
    assert row["in_effect"] is False


async def test_a_setting_with_no_override_is_always_in_effect(client, monkeypatch):
    """Nothing stored means the deployment's own value is what is running, by definition. Marking
    those pending would put a warning on every untouched row and train people to ignore it."""
    token = await _superadmin(client, monkeypatch, slug="rc22", email="boss@rc22.com")
    rows = (await client.get("/api/admin/runtime/settings", headers=auth(token))).json()
    for row in rows:
        if not row["overridden"]:
            assert row["in_effect"] is True, row["key"]
