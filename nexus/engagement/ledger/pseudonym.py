"""Pseudonymous keys, dataset splits and the archive's sealing key (spec §18.3, §18.4, D25).

One secret, ``ledger_pseudonym`` in Provider keys, derives everything here:

* **Keys** — ``HMAC-SHA256(secret, "<kind>:<value>")``. Deterministic, so the same person is the same
  key in every event and every store (joins and erasure work), and not reversible without the
  secret, which never leaves the app.
* **The archive's sealing key** — derived from the same secret with a different label, so an archive
  dump alone reveals nothing.
* **Splits** — ``train``/``val``/``test`` assigned by hashing the WORKSPACE key, so no workspace's
  data appears in two splits and an evaluation never scores a model on a customer it trained on.

Losing the secret breaks linkage and makes the archive unreadable (spec §16); it is backed up with the
database and rotation re-keys from the archive.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet

KINDS = ("person", "company", "workspace", "user", "account", "contact", "mailbox")


def _normal(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def key(secret: str, kind: str, value: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown key kind {kind!r}")
    if not secret:
        raise ValueError("the pseudonymisation secret is not configured")
    digest = hmac.new(secret.encode(), f"{kind}:{_normal(value)}".encode(), hashlib.sha256)
    return digest.hexdigest()[:32]


def person_key(secret: str, email: str) -> str:
    return key(secret, "person", email)


def workspace_key(secret: str, tenant_id: str) -> str:
    return key(secret, "workspace", tenant_id)


def company_key(secret: str, domain: str) -> str:
    return key(secret, "company", domain)


def split_for(workspace: str) -> str:
    """80 / 10 / 10 by workspace. Deterministic, and independent of the secret's rotation because it
    hashes the key the rows already carry."""
    bucket = int(hashlib.sha256(workspace.encode()).hexdigest()[:8], 16) % 100
    if bucket < 80:
        return "train"
    return "val" if bucket < 90 else "test"


def archive_fernet(secret: str) -> Fernet:
    if not secret:
        raise ValueError("the pseudonymisation secret is not configured")
    raw = hashlib.sha256(f"nexus-ledger-archive:{secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))
