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


# ---- the table -----------------------------------------------------------------------------------

async def test_a_provider_key_row_stores_no_plaintext():
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.provider_key import ProviderKey
    from nexus.providers.crypto import key_digest, key_hint, seal_key

    # gitleaks:allow - fabricated literal; the assertion below is that it is NOT stored.
    secret = "sk-test-abcdefgh1234"
    async with get_platform_sessionmaker()() as s:
        row = ProviderKey(
            provider="exa", label="primary",
            key_encrypted=seal_key(secret),
            key_hint=key_hint(secret), key_digest=key_digest(secret),
        )
        s.add(row)
        await s.commit()
        assert secret not in row.key_encrypted
        assert row.status == "untested"
        assert row.enabled is True
        assert row.preferred is False


def test_the_table_carries_no_tenant_id():
    """Platform-global, like companies/people/source_databases. A tenant_id would make
    scripts/apply_rls.py enrol it, and every worker read would then return zero rows — silently,
    because RLS misses are not errors."""
    from nexus.models.provider_key import ProviderKey

    assert "tenant_id" not in ProviderKey.__table__.columns


def test_probe_ok_and_verified_are_distinct_statuses():
    """The Groq shape: a key can authenticate while every real call fails. One green state would
    have shown five healthy keys while every draft came from the stub."""
    from nexus.models.provider_key import KEY_STATUSES

    assert "probe_ok" in KEY_STATUSES and "verified" in KEY_STATUSES
    assert KEY_STATUSES.index("probe_ok") < KEY_STATUSES.index("verified")


# ---- the service ---------------------------------------------------------------------------------

async def test_adding_the_same_key_twice_is_refused():
    """Silently accepting a duplicate would double that key's share of the rotation."""
    from nexus.providers.service import DuplicateKey, add_key

    await add_key("exa", "one", "sk-dupe-1111")
    with pytest.raises(DuplicateKey):
        await add_key("exa", "two", "sk-dupe-1111")


async def test_preferring_a_key_unpins_the_previous_one_and_enables_it():
    """At most one pin per provider. Preferring implies enabling, because pinned-but-disabled is a
    state the UI could express and the resolver would have to silently ignore."""
    from nexus.providers.service import add_key, list_keys, prefer_key, set_enabled

    a = await add_key("brave", "a", "sk-a-0001")
    b = await add_key("brave", "b", "sk-b-0002")
    await prefer_key(a.id)
    await set_enabled(b.id, False)
    await prefer_key(b.id)

    rows = {r.id: r for r in await list_keys("brave")}
    assert rows[b.id].preferred is True and rows[b.id].enabled is True
    assert rows[a.id].preferred is False


async def test_disabling_the_pinned_key_clears_the_pin():
    from nexus.providers.service import add_key, list_keys, prefer_key, set_enabled

    k = await add_key("serper", "only", "sk-s-0003")
    await prefer_key(k.id)
    await set_enabled(k.id, False)
    row = (await list_keys("serper"))[0]
    assert row.enabled is False and row.preferred is False


def test_a_caller_cannot_set_status_directly():
    """Only mark_tested/mark_failed write status. An admin who could set `verified` by hand could
    mark a dead key working — the rule nexus/sources/service.py enforces for its ladder."""
    import inspect

    from nexus.providers import service

    for name in ("add_key", "update_label", "set_enabled", "prefer_key"):
        sig = inspect.signature(getattr(service, name))
        assert "status" not in sig.parameters, f"{name} exposes status"


async def test_an_unknown_provider_is_refused():
    from nexus.providers.service import UnknownProvider, add_key

    with pytest.raises(UnknownProvider):
        await add_key("not-a-provider", "x", "sk-x-0000")


async def test_an_empty_key_is_refused():
    """Storing "" would produce a row that resolves to nothing while looking configured."""
    from nexus.providers.service import add_key

    with pytest.raises(ValueError):
        await add_key("exa", "blank", "   ")


# ---- resolution, and the TTL that keeps a separate worker current --------------------------------

async def test_an_empty_table_resolves_to_the_env_pool():
    """The safety property that lets every earlier task ship: until someone adds a key, behaviour
    is byte-identical to before this feature existed."""
    from nexus.providers import resolver
    from nexus.providers.catalog import env_pool

    resolver.invalidate()
    assert await resolver.key_pool("anthropic") == env_pool("anthropic")


async def test_db_keys_replace_the_env_pool_once_any_exist():
    from nexus.providers import resolver
    from nexus.providers.service import add_key

    await add_key("serper", "db", "sk-db-9999")
    resolver.invalidate()
    assert await resolver.key_pool("serper") == ["sk-db-9999"]


async def test_the_pinned_key_comes_first():
    """The operator's selection is the starting point; rotation is the failure path, not the
    normal one."""
    from nexus.providers import resolver
    from nexus.providers.service import add_key, prefer_key

    await add_key("github", "first-added", "ghp-aaaa")
    second = await add_key("github", "second-added", "ghp-bbbb")
    await prefer_key(second.id)
    resolver.invalidate()
    assert (await resolver.key_pool("github"))[0] == "ghp-bbbb"


async def test_a_disabled_key_is_not_in_the_pool():
    from nexus.providers import resolver
    from nexus.providers.service import add_key, set_enabled

    k = await add_key("exa", "off", "sk-off-1234")
    await add_key("exa", "on", "sk-on-5678")
    await set_enabled(k.id, False)
    resolver.invalidate()
    pool = await resolver.key_pool("exa")
    assert "sk-off-1234" not in pool and "sk-on-5678" in pool


async def test_an_undecryptable_row_is_skipped_not_fatal():
    """One bad row must not disable the rest of the pool."""
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.provider_key import ProviderKey
    from nexus.providers import resolver
    from nexus.providers.crypto import key_digest, seal_key

    async with get_platform_sessionmaker()() as s:
        s.add(ProviderKey(provider="brave", label="corrupt",
                          key_encrypted="not-a-fernet-token",
                          key_hint="xxxx", key_digest=key_digest("corrupt-unique")))
        s.add(ProviderKey(provider="brave", label="fine",
                          key_encrypted=seal_key("sk-brave-good"),
                          key_hint="good", key_digest=key_digest("sk-brave-good")))
        await s.commit()
    resolver.invalidate()
    assert await resolver.key_pool("brave") == ["sk-brave-good"]


async def test_a_second_process_sees_a_new_key_once_the_ttl_lapses(monkeypatch):
    """THE worker requirement.

    The worker runs in its own container, so the API invalidating its cache reaches nothing. Two
    independently-constructed caches stand in for two processes: a test with one cache would pass
    while the worker stayed stale, which is precisely the bug.
    """
    from nexus.providers import resolver
    from nexus.providers.service import add_key

    await add_key("firecrawl", "one", "fc-first-0001")

    worker = resolver.PoolCache()                      # "the worker process"
    assert await worker.key_pool("firecrawl") == ["fc-first-0001"]

    # The API process adds a key. The worker's cache knows nothing about it.
    await add_key("firecrawl", "two", "fc-second-0002")
    assert await worker.key_pool("firecrawl") == ["fc-first-0001"], "should still be cached"

    # ...until the TTL lapses. No restart, no message passing.
    now = [1000.0]
    monkeypatch.setattr(resolver.time, "monotonic", lambda: now[0])
    fresh = resolver.PoolCache()
    await fresh.key_pool("firecrawl")
    now[0] += resolver.POOL_TTL_S + 1
    assert set(await fresh.key_pool("firecrawl")) == {"fc-first-0001", "fc-second-0002"}


# ---- the seam actually reaches the providers -----------------------------------------------------

async def test_a_key_added_in_the_panel_reaches_the_exa_provider():
    """The behaviour the whole feature exists for: no env edit, no redeploy, no restart."""
    from nexus.integrations.search.engines import ExaSearchProvider
    from nexus.providers import resolver
    from nexus.providers.service import add_key

    provider = ExaSearchProvider(api_keys=["env-key-only"])
    assert provider.api_keys == ["env-key-only"]

    await add_key("exa", "from-the-panel", "sk-panel-0001")
    resolver.invalidate()
    await provider._refresh_keys()
    assert provider.api_keys == ["sk-panel-0001"]
    assert provider._key_idx == 0, "a refresh must start from the pinned key"


async def test_refreshing_survives_a_resolver_failure(monkeypatch):
    """Key management must never break the call it exists to serve. If the resolver raises, the
    provider keeps the keys it already had rather than losing them."""
    from nexus.integrations.search.engines import ExaSearchProvider
    from nexus.providers import resolver

    async def boom(_provider):
        raise RuntimeError("database is down")

    monkeypatch.setattr(resolver, "managed_pool", boom)
    provider = ExaSearchProvider(api_keys=["still-here"])
    await provider._refresh_keys()
    assert provider.api_keys == ["still-here"]


async def test_the_apify_client_can_gain_its_first_key_from_the_panel():
    """A client constructed with an empty pool must still pick up the first managed key, which is
    why the refresh runs BEFORE the not-configured check."""
    from nexus.integrations.apify import ApifyClient
    from nexus.providers import resolver
    from nexus.providers.service import add_key

    client = ApifyClient([])
    assert client.configured is False

    await add_key("apify", "first-ever", "apify-first-0002")
    resolver.invalidate()
    await client._refresh_keys()
    assert client.configured is True


async def test_a_refresh_never_overwrites_explicitly_passed_keys():
    """Found by a failing rotation test, not by reasoning.

    `key_pool` falls back to the environment, so refreshing against it would replace keys a caller
    passed deliberately with whatever the environment held. "The database layers over the
    environment" must mean the database wins WHEN IT HAS SOMETHING TO SAY — not that every refresh
    reasserts the environment over its caller.
    """
    from nexus.integrations.search.engines import ExaSearchProvider
    from nexus.providers import resolver

    resolver.invalidate()
    provider = ExaSearchProvider(api_keys=["explicitly-passed"])
    await provider._refresh_keys()          # no managed rows for a provider nobody configured
    assert provider.api_keys == ["explicitly-passed"]


async def test_managed_pool_is_empty_when_nothing_is_registered():
    from nexus.providers import resolver

    resolver.invalidate()
    assert await resolver.managed_pool("anthropic") == []
    # ...while key_pool still offers the environment floor for ordinary callers.
    from nexus.providers.catalog import env_pool

    assert await resolver.key_pool("anthropic") == env_pool("anthropic")
