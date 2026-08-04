# tests/test_source_safety.py
"""The two guards that make "add your own database" safe to expose in a web form.

A form that accepts a DSN and reports whether it connected is a port scanner. A mapping UI that
feeds discovered names into a query is SQL injection with extra steps. Both are the standard
findings on this feature, so both are pinned here rather than trusted to review.
"""
from __future__ import annotations

import pytest

from nexus.sources import SourceRejected, redact_dsn, require_identifier, validate_dsn


# ---- SSRF ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("dsn", [
    "postgresql://u:p@localhost/db",
    "postgresql://u:p@127.0.0.1/db",
    "postgresql://u:p@[::1]/db",
    "postgresql://u:p@10.0.0.5/db",
    "postgresql://u:p@192.168.1.10/db",
    "postgresql://u:p@169.254.169.254/db",       # AWS/Azure instance metadata
    "postgresql://u:p@metadata.google.internal/db",
])
def test_internal_targets_are_refused(dsn):
    """Being a platform admin is not the same as being authorised to read the host's own network.
    The credential comes from a customer, so the person typing it may not own what it points at."""
    with pytest.raises(SourceRejected):
        validate_dsn(dsn)


@pytest.mark.parametrize("dsn", [
    "file:///etc/passwd",
    "http://example.com/",
    "mysql://u:p@db.example.com/x",
    "",
])
def test_unsupported_schemes_never_reach_a_driver(dsn):
    """An unknown scheme handed to the driver would happily try a file or a unix socket."""
    with pytest.raises(SourceRejected):
        validate_dsn(dsn)


def test_a_public_postgres_host_is_accepted(monkeypatch):
    """The resolver is stubbed because the suite is hermetic and offline — no name resolves here.
    Stubbing it is also what keeps this a test of the ALLOW logic rather than of DNS."""
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 5432))],   # a public address
    )
    dsn = "postgresql://user:pw@db.example.com:5432/prod"
    assert validate_dsn(dsn) == dsn


def test_a_public_name_pointing_at_loopback_is_still_refused(monkeypatch):
    """The DNS-rebinding shape: a perfectly ordinary hostname that resolves inward. Checking the
    name alone would let this through, which is why the check resolves first."""
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 5432))],
    )
    with pytest.raises(SourceRejected):
        validate_dsn("postgresql://user:pw@totally-normal.example.com:5432/prod")


def test_localhost_is_allowed_only_when_the_setting_says_so():
    """Local development genuinely does point at localhost. This is a SETTING, never a request
    parameter — an admin must not be able to switch off the guard from the form it protects."""
    with pytest.raises(SourceRejected):
        validate_dsn("postgresql://u:p@localhost/db")
    assert validate_dsn("postgresql://u:p@localhost/db", allow_private=True)


def test_a_host_that_does_not_resolve_is_refused():
    with pytest.raises(SourceRejected):
        validate_dsn("postgresql://u:p@no-such-host.invalid/db")


# ---- identifiers --------------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    'x"; DROP TABLE y; --',
    "users; select 1",
    "a b",
    "a.b",
    "'quoted'",
    "",
    None,
    "1_starts_with_digit",
    "x" * 64,
])
def test_unsafe_identifiers_are_refused(name):
    """Every value reaching a SQL string passes this, INCLUDING names we discovered ourselves —
    a name returned by introspection is still attacker-controlled if the attacker owns the source
    database, and `x"; DROP TABLE y; --` is a legal Postgres identifier."""
    with pytest.raises(SourceRejected):
        require_identifier(name)


@pytest.mark.parametrize("name", ["users", "contact_email", "_private", "Table1", "x" * 63])
def test_ordinary_identifiers_are_accepted(name):
    assert require_identifier(name) == name


# ---- secrets ------------------------------------------------------------------------------------

def test_the_password_never_survives_redaction():
    """A DSN reaches logs and audit rows. The password must not travel with it."""
    out = redact_dsn("postgresql://user:sup3rsecret@db.example.com:5432/prod")
    assert "sup3rsecret" not in out
    assert "db.example.com" in out and "user" in out


def test_redaction_never_raises_on_junk():
    """It is called on error paths; throwing there would replace a useful message with a stack."""
    assert redact_dsn("not a dsn at all")
    assert redact_dsn("")
