# nexus/providers/catalog.py
"""The providers whose keys are manageable from the Control plane.

Adding a provider is one entry here. The exclusions matter more than the inclusions:

* ``stripe_secret_key`` — money. A wrong value stops billing *silently* rather than erroring, so it
  needs its own test and its own care.
* ``hubspot_access_token`` — per-tenant, handled elsewhere. Platform-wide and per-tenant are
  different axes and conflating them is not a config change afterwards.
* ``secret_key``, ``network_token_enc_key``, ``mfa_secret_enc_key``, ``source_db_dsn_enc_key`` —
  cryptographic roots, not provider credentials. Changing one invalidates every sealed OAuth token,
  every MFA seed and every encrypted credential simultaneously, with no way back. Managing those is
  a key-ROTATION feature with re-encryption, which this is not.

``test_crypto_roots_are_never_manageable`` pins all of that, because the difference between a
provider credential and an encryption root is not obvious from `config.py`, where they sit
side by side.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    id: str
    label: str
    # The Settings attribute holding the env fallback. Either a `list[str]` property (the four
    # providers with rotation pools) or a plain `str`; `env_pool` normalises both.
    env_attr: str


PROVIDERS: dict[str, ProviderSpec] = {
    "groq": ProviderSpec("groq", "Groq (LLM)", "groq_api_key_list"),
    "anthropic": ProviderSpec("anthropic", "Anthropic (LLM)", "anthropic_api_key"),
    "openai_compat": ProviderSpec("openai_compat", "OpenAI-compatible (LLM)", "llm_api_key"),
    "exa": ProviderSpec("exa", "Exa (search)", "exa_api_key_list"),
    "firecrawl": ProviderSpec("firecrawl", "Firecrawl (search)", "firecrawl_api_key_list"),
    "brave": ProviderSpec("brave", "Brave (search)", "brave_api_key"),
    "serper": ProviderSpec("serper", "Serper (search)", "serper_api_key"),
    "apify": ProviderSpec("apify", "Apify (actors)", "apify_api_key_list"),
    "github": ProviderSpec("github", "GitHub (public API signals)", "github_token"),
}


def env_pool(provider: str) -> list[str]:
    """The env-configured keys for a provider — the floor the database layers over.

    An unknown provider returns ``[]`` rather than raising: this is called from the resolver on a
    hot path, and a typo should cost that provider its keys, not take down the caller.
    """
    from nexus.core.config import get_settings

    spec = PROVIDERS.get(provider)
    if spec is None:
        return []
    value = getattr(get_settings(), spec.env_attr, "")
    if isinstance(value, list):
        return [k for k in value if k]
    return [value] if value else []
