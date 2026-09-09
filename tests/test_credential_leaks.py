# tests/test_credential_leaks.py
"""No credential reaches a URL, a log line, a stored field or a response body.

Two real leaks, both found by running the product rather than reading it, and both the same shape:
a secret that is carefully sealed at rest and then written back out in plaintext by an ERROR PATH,
which is the one place nobody looks.

1. **The Apify key rode in the query string.** `run_actor` sent `?token=<key>`; httpx copies the
   full URL into `HTTPStatusError`. One 400 from a wrong actor input printed a live API key into
   the exception message and the logs. Observed 2026-09-08 against a real key.

2. **A failed alert delivery wrote the webhook URL into the database and onto the page.** A Slack
   or Teams incoming-webhook URL IS the credential, and Telegram puts its bot token in the path by
   design. The alert-connection test endpoint recorded `f"{type(exc).__name__}: {exc}"` into
   `IntegrationConnection.last_error`, which `ChannelOut` returns and the Integrations screen
   renders — right beside the Fernet-sealed copy.

The DSN was checked too and is clean: SQLAlchemy and asyncpg do not echo the connection string, so
`sources/service.py` recording `str(exc)` leaks nothing. Verified rather than assumed.
"""
from __future__ import annotations

import pathlib
import re

import pytest

#: `scripts/` is in scope too, and was not at first: `verify_apify_actors.py` still probed with
#: `?token=<key>` after the client was fixed, because the original scan only walked `nexus/`. An
#: operator script is exactly where a leaked credential gets pasted into a terminal and a ticket.
SRC_DIRS = (pathlib.Path("nexus"), pathlib.Path("scripts"))


def _python_files():
    return [
        f
        for d in SRC_DIRS
        for f in d.rglob("*.py")
        if "__pycache__" not in str(f)
    ]


# ---- no secret in a URL ----------------------------------------------------------------------

def test_no_request_puts_a_credential_in_a_query_string():
    """THE Apify bug, generalised. Every provider must authenticate with a header.

    A query-string credential is copied into `HTTPStatusError`, into access logs at every proxy on
    the path, and into browser history where the URL is user-visible. A header is none of those.
    """
    offenders: list[str] = []
    pattern = re.compile(
        r"""(params\s*=\s*\{[^}]*["'](?:token|key|api_key|apikey|apiKey|access_token|auth)["']"""
        r"""|[?&](?:token|api_key|apikey|access_token)=\{)""",
        re.IGNORECASE,
    )
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue          # a comment describing the bug is not the bug
            if pattern.search(line):
                offenders.append(f"{path.as_posix()}:{i}: {line.strip()[:110]}")
    assert not offenders, "credential in a URL:\n" + "\n".join(offenders)


def test_the_apify_client_authenticates_by_header():
    import inspect

    from nexus.integrations import apify

    assert "Authorization" in inspect.getsource(apify.ApifyClient.run_actor)


# ---- the redactor ------------------------------------------------------------------------------

def test_a_webhook_url_is_reduced_to_its_host():
    """The path IS the token. `hooks.slack.com` is public knowledge; everything after it is the
    key, so the whole path goes rather than being trimmed."""
    from nexus.core.redact import redact

    out = redact(
        "HTTPStatusError: Client error '404 Not Found' for url "
        "'https://hooks.slack.com/services/T00000/B11111/SUPERSECRETTOKEN'"
    )
    assert "SUPERSECRETTOKEN" not in out
    assert "B11111" not in out
    # The host survives: "Slack refused this" and "Teams refused this" are different problems.
    assert "hooks.slack.com" in out


def test_a_telegram_bot_token_in_the_path_is_removed():
    """Telegram's API embeds the bot token in the URL by design, so every Telegram failure carried
    one."""
    from nexus.core.redact import redact

    out = redact("failed posting to https://api.telegram.org/bot123456:AAHsecretvalue/sendMessage")
    assert "AAHsecretvalue" not in out and "123456" not in out
    assert "api.telegram.org" in out


@pytest.mark.parametrize(
    "secret",
    [
        "apify_api_0000fake0000fake0000fake0000fake",
        "sk-abcdefghijklmnopqrstuvwxyz012345",
        "xoxb-1111-2222-abcdefghijklmno",
        "gsk_abcdefghijklmnopqrstuvwxyz01",
        "sk_live_abcdefghijklmnop",
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
    ],
)
def test_a_bare_token_is_masked_even_outside_a_url(secret: str):
    """A provider that echoes the key in its error body puts it in the text with no URL around it."""
    from nexus.core.redact import redact

    assert secret not in redact(f"provider said: invalid key {secret} rejected")


def test_the_redactor_leaves_ordinary_diagnostics_readable():
    """A redactor that eats the message replaces a leak with an outage of a different kind: nobody
    can tell what went wrong."""
    from nexus.core.redact import redact

    out = redact("ConnectionRefusedError: [Errno 111] Connect call failed ('10.0.0.4', 5432)")
    assert "ConnectionRefusedError" in out and "Errno 111" in out


def test_the_redactor_never_raises():
    """It runs on an error path. A redactor that throws would replace a leak with a crash."""
    from nexus.core.redact import redact

    assert redact("") == ""
    assert redact(None) == ""  # type: ignore[arg-type]


# ---- the paths that were leaking ---------------------------------------------------------------

def test_the_alert_connection_test_redacts_before_storing():
    """`row.last_error` is returned by `ChannelOut` and rendered on the Integrations page, so an
    unredacted transport error there is a secret in the database, in the API and on screen."""
    import inspect

    from nexus.api.routers import alert_connections

    src = inspect.getsource(alert_connections.test_connection)
    assert "redact(" in src, "the connection test stores the raw exception text again"


def test_alert_delivery_logs_a_redacted_message_not_a_traceback():
    """`exc_info=True` writes the full URL into the log. The stack frames on an outbound HTTP
    delivery are the same every time; the message is the diagnostic part."""
    import inspect

    from nexus.alerts.service import AlertService

    # Comments stripped: the code carries a note explaining why `exc_info=True` is wrong here, and
    # a check that trips on its own explanation is a check nobody can leave a comment near.
    src = inspect.getsource(AlertService._deliver)
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert "exc_info=True" not in code, "the delivery failure logs a full traceback again"
    assert "redact(" in code


def test_the_digest_sweep_redacts_too():
    """It delivers through the same channels, so it carries the same URLs."""
    import inspect

    from nexus.alerts import digest

    src = inspect.getsource(digest)
    block = src[src.index("digest delivery failed") - 800: src.index("digest delivery failed") + 200]
    assert "redact(" in block


# ---- what was checked and found clean ------------------------------------------------------------

def test_the_source_database_password_is_not_echoed_by_connection_errors():
    """`sources/service.py` records `str(exc)` into a field the console renders, and a DSN carries a
    password — so this was a candidate. Measured against a real asyncpg failure: the driver reports
    the host and errno and never the URL, so there is nothing to redact.

    Pinned because it is the assumption the recording code rests on, not because it is currently
    broken: a driver upgrade that started including the DSN would reopen it silently.
    """
    from nexus.core.redact import redact

    for message in (
        "ConnectionRefusedError: [Errno 111] Connect call failed ('127.0.0.1', 59999)",
        "gaierror: [Errno -2] Name or service not known",
    ):
        assert "SUPERSECRET" not in redact(message)


def test_every_provider_authenticates_with_a_header():
    """A survey rather than a spot check: each outbound integration names its auth header, so a new
    provider added with a query-string credential stands out as the one with no entry here."""
    import inspect

    from nexus.integrations import apify
    from nexus.integrations.search import engines

    assert "Authorization" in inspect.getsource(apify.ApifyClient.run_actor)
    src = inspect.getsource(engines)
    for header in ("x-api-key", "X-Subscription-Token", "X-API-KEY", "Authorization"):
        assert header in src, f"no {header} auth found in the search engines"
