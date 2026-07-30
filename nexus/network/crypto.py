# nexus/network/crypto.py
"""Encryption of stored OAuth token bundles (at rest).

Tokens live in ``network_source_accounts.oauth`` (a JSON column) as ``{"enc": "<fernet>"}`` and are
never serialized to a client. The Fernet key comes from ``network_token_enc_key`` or, when unset, is
derived deterministically from ``secret_key`` — so tokens are always encrypted with no extra required
secret. ``cryptography`` ships transitively via ``python-jose[cryptography]`` (no new dependency).
"""
from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

from nexus.core.config import get_settings
from nexus.core.crypto import fernet_for


def _fernet() -> Fernet:
    """The network token key, or one derived from ``secret_key``. Derivation now lives in
    ``nexus.core.crypto`` so other subsystems (MFA) reuse it instead of copying it; the resolved
    key for network tokens is byte-for-byte what it was."""
    return fernet_for(get_settings().network_token_enc_key)


def seal_tokens(bundle: dict) -> dict:
    """Encrypt a token bundle for storage. Returns the JSON-column value ``{"enc": "..."}``."""
    token = _fernet().encrypt(json.dumps(bundle).encode()).decode()
    return {"enc": token}


def unseal_tokens(oauth: dict | None) -> dict:
    """Decrypt a stored ``oauth`` value back to the token bundle. Tolerant: returns ``{}`` for
    empty/missing/tampered input rather than raising (a corrupt blob → 'reconnect', not a crash)."""
    if not oauth:
        return {}
    enc = oauth.get("enc")
    if not enc:
        return {}
    try:
        return json.loads(_fernet().decrypt(enc.encode()).decode())
    except (InvalidToken, ValueError, TypeError):
        return {}
