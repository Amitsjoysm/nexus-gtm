# nexus/ingestion/crm_crypto.py
"""Sealing of per-tenant CRM credentials at rest.

A CRM access token is a live credential to the customer's system of record. It never sits in a
column in the clear and is never serialised back to a client, not even to the admin who typed it.

Mirrors ``nexus/sources/crypto.py``: a thin subsystem module over ``nexus/core/crypto.py`` so the
key derivation is not duplicated. It should have its own key for the reason stated there —
rotating the key protecting CRM tokens must not orphan MFA seeds or network OAuth tokens — but
adding a Settings field is out of scope for this change, so ``_key`` returns "" and the key
derives from ``secret_key``. That is still "always encrypted, no silent plaintext fallback", and
``_key`` is the single place to change when ``crm_token_enc_key`` is added.

Unlike ``sources/crypto.py``, an unsealable value is **tolerated** and reads as ``{}``. This
matches ``network/crypto.py``: an unusable CRM token degrades to "reconnect your CRM", a real
state the admin can fix. A DSN cannot degrade that way because ``""`` is what a *deleted* secret
looks like; a CRM connection row still exists and is reported as needing reconnection.
"""
from __future__ import annotations

import json

from nexus.core.crypto import seal_text, unseal_text


def _key() -> str:
    """The CRM sealing key. Empty derives one from ``secret_key`` (see module docstring)."""
    return ""


def seal_crm_secret(bundle: dict) -> dict:
    """Encrypt a credential bundle. Returns the JSON-column value ``{"enc": "..."}``.

    A dict rather than a bare string so an OAuth token set (access + refresh + expiry) can be
    stored later without a migration.
    """
    return {"enc": seal_text(json.dumps(bundle), key=_key())}


def unseal_crm_secret(blob: dict | None) -> dict:
    """Decrypt a stored value back to the bundle. ``{}`` for empty/missing/tampered input."""
    if not blob:
        return {}
    plain = unseal_text(blob.get("enc") or "", key=_key())
    if not plain:
        return {}
    try:
        loaded = json.loads(plain)
    except (ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
