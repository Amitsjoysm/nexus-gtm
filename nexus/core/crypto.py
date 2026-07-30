# nexus/core/crypto.py
"""Symmetric encryption of secrets at rest (Fernet).

Extracted from ``nexus/network/crypto.py`` so more than one subsystem can seal a secret without
duplicating the key derivation. Behaviour is unchanged for the network connectors: pass their
``network_token_enc_key`` and they get exactly the Fernet they had before.

The key rule is the same everywhere: when no dedicated key is configured, derive one
deterministically from ``secret_key`` — so a secret is *always* encrypted at rest, with no extra
required configuration and no silent plaintext fallback. Each caller passes its own override so
keys can be rotated independently (rotating the network token key must not orphan MFA seeds).

``cryptography`` ships transitively via ``python-jose[cryptography]`` — no new dependency.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from nexus.core.config import get_settings


def fernet_for(key: str = "") -> Fernet:
    """A Fernet using ``key`` if set, else one derived from the app ``secret_key``."""
    key = (key or "").strip()
    if not key:
        digest = hashlib.sha256(get_settings().secret_key.encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    return Fernet(key.encode())


def seal_text(plaintext: str, *, key: str = "") -> str:
    """Encrypt a string for storage in a text column."""
    return fernet_for(key).encrypt((plaintext or "").encode()).decode()


def unseal_text(sealed: str, *, key: str = "") -> str:
    """Decrypt a sealed string. Returns ``""`` for empty/tampered input rather than raising —
    a corrupt blob should read as 'no usable secret' (re-enrol), not crash the login path."""
    if not sealed:
        return ""
    try:
        return fernet_for(key).decrypt(sealed.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return ""
