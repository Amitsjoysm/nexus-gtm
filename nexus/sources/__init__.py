"""Externally-hosted source databases: register, introspect, map, dry-run, then use.

A superadmin registers a read-only DSN; we discover its tables and columns; the admin maps those
onto the fields this app needs; a dry run proves the mapping before anything consumes it. Only then
does the source become an enrichment provider, tried ahead of the paid APIs.

The staging is the point. A misconfigured source that silently returns wrong rows would attach one
customer's contacts to another company, and this codebase has already shipped six wrong-attribution
bugs by trusting a weaker signal than it should have.

Read `safety.py` first: it holds the SSRF guard on the DSN and the identifier validation that every
generated query passes through.
"""
from nexus.sources.crypto import DsnUnsealable, seal_dsn, unseal_dsn
from nexus.sources.safety import (
    SourceRejected,
    redact_dsn,
    require_identifier,
    valid_identifier,
    validate_dsn,
)

# `engine` and `service` are deliberately NOT re-exported here. Importing this package must stay
# cheap and side-effect-free: `engine` pulls in SQLAlchemy's async engine machinery and `service`
# imports the ORM, and `safety` is imported by things that only want the guards.

__all__ = [
    "DsnUnsealable",
    "SourceRejected",
    "redact_dsn",
    "require_identifier",
    "seal_dsn",
    "unseal_dsn",
    "valid_identifier",
    "validate_dsn",
]
