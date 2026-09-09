# tests/test_alert_webhook_ssrf.py
"""A customer-supplied alert webhook URL must not reach the internal network.

Found by attacking the running deployment. An ordinary customer with `manage_workspace` could
`PUT /alert-connections/slack` with a `url` pointing at the cloud metadata endpoint, an internal
service, or the admin API, then `POST .../test` to force the request. The only validation was
`startswith("https://")`. The delivery error was a verbatim oracle distinguishing open ports,
closed ports and unresolvable hosts — a working internal port scanner.

The guard reuses `sources.safety._is_blocked_host`, the one this codebase already had for the DSN
form of the same primitive. These tests pin: the guard refuses the hosts that mattered, the paid
public webhook still passes, `allow_private` is honoured for local dev, and — the structural part —
the guard is wired into the delivery transport, not only the connect endpoint, because DNS can
rebind after a URL is stored and rows predate the check.
"""
from __future__ import annotations

import inspect
import re

import pytest

from nexus.alerts.url_guard import WebhookURLRejected, validate_webhook_url


BLOCKED = [
    "https://169.254.169.254/latest/meta-data/iam/security-credentials/",  # AWS metadata (link-local)
    "https://metadata.google.internal/computeMetadata/v1/",                # GCP metadata by name
    "https://127.0.0.1/api/admin/billing/overview",                        # loopback -> our own admin API
    "https://localhost:8000/metrics",                                      # loopback by name
    "http://hooks.slack.com/services/T/B/x",                               # not https
    "https://10.0.0.5/",                                                   # private range
    "https://192.168.1.1/",                                                # private range
    "not-a-url",
    "",
]


@pytest.mark.parametrize("url", BLOCKED)
def test_internal_and_malformed_urls_are_refused(url):
    with pytest.raises(WebhookURLRejected):
        validate_webhook_url(url, allow_private=False)


def test_a_real_public_webhook_still_passes():
    # A genuine Slack incoming webhook must not be collateral damage.
    ok = validate_webhook_url("https://hooks.slack.com/services/T00/B00/xxxxxxxx", allow_private=False)
    assert ok.startswith("https://hooks.slack.com/")


def test_allow_private_is_a_local_dev_escape_hatch():
    # With the setting on, a localhost mock receiver is reachable — but https is still required.
    assert validate_webhook_url("https://localhost:9000/hook", allow_private=True)
    with pytest.raises(WebhookURLRejected):
        validate_webhook_url("http://localhost:9000/hook", allow_private=True)


def test_the_guard_is_wired_into_delivery_not_only_connect():
    """The load-bearing call site. If only the connect endpoint validated, a rebind after storage —
    or any row stored before the guard existed — would still deliver to an internal host."""
    from nexus.alerts import channels

    src = inspect.getsource(channels._httpx_post)
    assert "validate_webhook_url" in src, (
        "the real HTTP poster does not run the SSRF guard; connect-time validation alone is "
        "bypassable by DNS rebinding and does not cover already-stored URLs"
    )


def test_the_allow_private_toggle_cannot_be_flipped_from_the_runtime_panel():
    """An SSRF guard toggle exposed in the runtime catalog could be switched off through the very
    surface it protects, the same rule that keeps `source_db_allow_private` out of it."""
    from nexus.runtime_config.catalog import CATALOG, FORBIDDEN

    assert "alert_webhook_allow_private" in FORBIDDEN
    assert "alert_webhook_allow_private" not in CATALOG


def test_it_refuses_to_boot_with_the_guard_off_in_production():
    from nexus.core.config import Settings

    with pytest.raises(Exception):
        Settings(env="prod", alert_webhook_allow_private=True, secret_key="x" * 40)
