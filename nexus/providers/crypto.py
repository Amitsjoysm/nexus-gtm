# nexus/providers/crypto.py
"""Sealing provider API keys at rest.

Same asymmetry as `nexus/sources/crypto.py`, and for the same reason: an unsealable key must NOT
degrade to `""`. An empty string is what a *deleted* key looks like, so the caller would go on to
report "not configured" — indistinguishable from a provider nobody set up. The operator's next move
differs completely between "restore the encryption key" and "add a key", so this raises.
"""
from __future__ import annotations

import hashlib

from nexus.core.crypto import seal_text, unseal_text


class KeyUnsealable(RuntimeError):
    """A stored provider key could not be decrypted — wrong encryption key, or a tampered row."""


def seal_key(plaintext: str) -> str:
    return seal_text(plaintext or "")


def unseal_key(sealed: str) -> str:
    out = unseal_text(sealed or "")
    if not out:
        raise KeyUnsealable(
            "a stored provider key could not be decrypted. The encryption key changed or the row "
            "was altered; the key must be re-entered."
        )
    return out


def key_digest(plaintext: str) -> str:
    """Stable fingerprint, so the same key cannot be registered twice.

    Fernet is randomised, so ciphertext cannot be compared. This can. It is an INDEX, not
    anonymisation — the same caveat as `people.email_hash`.
    """
    return hashlib.sha256((plaintext or "").encode()).hexdigest()


def key_hint(plaintext: str) -> str:
    """The last four characters, so the UI can tell two rows apart without ever holding the key."""
    return (plaintext or "")[-4:]
