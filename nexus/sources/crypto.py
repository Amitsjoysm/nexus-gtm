# nexus/sources/crypto.py
"""Sealing of registered source-database connection strings at rest.

A DSN is a live credential to somebody else's database — usually a *customer's*, since the whole
point of this feature is amortising one vendor licence across every tenant. It is the single most
sensitive string this subsystem stores, so it never sits in a column in the clear and is never
serialised back to a client, not even to the superadmin who typed it.

Its own key (``source_db_dsn_enc_key``) rather than a shared one, for the reason stated in
``nexus/core/crypto.py``: rotating the key that protects third-party database credentials must not
orphan MFA seeds or network OAuth tokens. Unset, it derives from ``secret_key``, so a DSN is always
encrypted with no extra required configuration and there is no silent plaintext fallback.

Note the asymmetry with ``network/crypto.py``: an unsealable OAuth bundle degrades to "reconnect",
which is a real state a user can fix. An unsealable DSN cannot degrade to an empty string, because
``""`` is what a *deleted* secret looks like and the caller would go on to report "not configured"
— indistinguishable from a source nobody registered. ``unseal_dsn`` raises instead.
"""
from __future__ import annotations

from nexus.core.config import get_settings
from nexus.core.crypto import seal_text, unseal_text


class DsnUnsealable(RuntimeError):
    """A stored DSN could not be decrypted — wrong key, or the row was tampered with.

    Deliberately loud. The alternative (returning "") makes a key-rotation mistake look exactly
    like an unregistered source, and the operator's next move for those two is opposite: restore
    the key versus register the source.
    """


def seal_dsn(dsn: str) -> str:
    return seal_text(dsn, key=get_settings().source_db_dsn_enc_key)


def unseal_dsn(sealed: str) -> str:
    plain = unseal_text(sealed, key=get_settings().source_db_dsn_enc_key)
    if not plain:
        raise DsnUnsealable(
            "stored connection string could not be decrypted "
            "(NEXUS_SOURCE_DB_DSN_ENC_KEY changed, or the row was altered)"
        )
    return plain
