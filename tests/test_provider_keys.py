# tests/test_provider_keys.py
"""Provider key storage, resolution, and the TTL that keeps a separate worker current.

Platform-wide provider credentials moved off environment variables and into a table an operator can
manage. Two failures measured 2026-08-21 are why:

* All five Groq keys returned 404 — the configured model had been withdrawn — so every LLM call
  fell to the stub and the stub's copy went to real prospects.
* Both Apify accounts 403'd on an approval that must be clicked in their console, and the key that
  worked a fortnight earlier now 401s.

Neither was visible from inside the app. The safety property that makes this shippable is that an
EMPTY table resolves to the environment pool exactly as before, so nothing changes until someone
adds a key.
"""
from __future__ import annotations

import pytest


# ---- sealing -------------------------------------------------------------------------------------

def test_a_sealed_key_round_trips():
    from nexus.providers.crypto import seal_key, unseal_key

    sealed = seal_key("sk-live-abc123")
    assert sealed != "sk-live-abc123", "the key must not be stored in the clear"
    assert unseal_key(sealed) == "sk-live-abc123"


def test_the_same_key_seals_differently_each_time():
    """Fernet is randomised. This is why duplicate detection uses the digest column and never the
    ciphertext — an index over ciphertext would match nothing."""
    from nexus.providers.crypto import seal_key

    assert seal_key("same") != seal_key("same")


def test_an_unsealable_key_raises_rather_than_reading_as_absent():
    """Returning "" would make a key-rotation mistake look exactly like "no key configured", and
    the operator's next move for those two is opposite: restore the encryption key versus add a
    credential. Same asymmetry as nexus/sources/crypto.py."""
    from nexus.providers.crypto import KeyUnsealable, unseal_key

    with pytest.raises(KeyUnsealable):
        unseal_key("not-a-fernet-token")


def test_digest_is_stable_and_hint_shows_only_the_tail():
    from nexus.providers.crypto import key_digest, key_hint

    assert key_digest("abc123") == key_digest("abc123")
    assert key_digest("abc123") != key_digest("abc124")
    assert key_hint("sk-live-verysecret9876") == "9876"
    assert len(key_hint("sk-live-verysecret9876")) == 4


# ---- the catalog ---------------------------------------------------------------------------------

def test_every_catalogued_provider_names_a_real_env_fallback():
    """The env pool is the floor. A provider whose fallback attribute does not exist on Settings
    would silently resolve to an empty pool the moment its DB rows were deleted — turning a
    delete into an outage."""
    from nexus.core.config import get_settings
    from nexus.providers.catalog import PROVIDERS

    settings = get_settings()
    assert len(PROVIDERS) == 9
    for spec in PROVIDERS.values():
        assert hasattr(settings, spec.env_attr), f"{spec.id}: no Settings.{spec.env_attr}"


def test_the_catalog_covers_exactly_the_pooled_providers():
    from nexus.providers.catalog import PROVIDERS

    assert set(PROVIDERS) == {
        "groq", "anthropic", "openai_compat", "exa",
        "firecrawl", "brave", "serper", "apify", "github",
    }


def test_crypto_roots_are_never_manageable():
    """secret_key, network_token_enc_key and mfa_secret_enc_key sit in config.py beside the
    provider keys and look like they belong here. They do not: changing one invalidates every
    sealed OAuth token, every MFA seed and every encrypted credential at once, with no way back.
    Managing those is a key-ROTATION feature with re-encryption, not this one.

    Stripe is excluded because it is money and fails silently; CRM because it is per-tenant.
    """
    from nexus.providers.catalog import PROVIDERS

    attrs = {spec.env_attr for spec in PROVIDERS.values()}
    for forbidden in ("secret_key", "network_token_enc_key", "mfa_secret_enc_key",
                      "stripe_secret_key", "hubspot_access_token", "source_db_dsn_enc_key"):
        assert forbidden not in attrs, f"{forbidden} must never be editable from the UI"


def test_the_env_pool_reads_both_list_and_single_key_settings():
    """Four providers expose a `_list` property; the rest are a single string. Both shapes have to
    resolve, or a provider silently has no floor."""
    from nexus.providers.catalog import env_pool

    assert isinstance(env_pool("exa"), list)       # list-shaped
    assert isinstance(env_pool("brave"), list)     # single-string-shaped, wrapped
    assert env_pool("nope") == []                  # unknown provider is empty, never an error
