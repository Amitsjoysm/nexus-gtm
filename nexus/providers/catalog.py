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
    # How the key is typed, when it is not a single opaque token. Served to the Add-a-key form so
    # the format is stated where it is typed, rather than learned from a failed test.
    key_format: str = ""


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
    # Engagement engine (spec §12, §18). One secret each, not rotation pools: the resolver's
    # first key is the one in use. Tested by nexus/engagement/credential_checks.py.
    "google_oauth": ProviderSpec(
        "google_oauth", "Google OAuth client secret (mailboxes)",
        "engagement_google_client_secret",
    ),
    "microsoft_oauth": ProviderSpec(
        "microsoft_oauth", "Microsoft app client secret (mailboxes)",
        "engagement_microsoft_client_secret",
    ),
    "ledger_archive": ProviderSpec(
        "ledger_archive", "Ledger archive store (Postgres connection string)",
        "ledger_archive_dsn",
    ),
    "ledger_training": ProviderSpec(
        "ledger_training", "Ledger training store (Postgres connection string)",
        "ledger_training_dsn",
    ),
    "ledger_insights": ProviderSpec(
        "ledger_insights", "Ledger insights store (Postgres connection string)",
        "ledger_insights_dsn",
    ),
    "ledger_pseudonym": ProviderSpec(
        "ledger_pseudonym", "Ledger pseudonymisation secret", "ledger_pseudonym_secret",
    ),
    # The PLATFORM account every workspace without its own Twilio calls on. Entered as
    # ACCOUNT_SID:AUTH_TOKEN: Twilio authenticates with the pair, so they are stored together.
    # A workspace's own Twilio is per-tenant and lives under Integrations, not here.
    "twilio": ProviderSpec(
        "twilio", "Twilio (calling, platform account)", "twilio_credential",
        key_format="Account SID and Auth Token joined by a colon: AC0123...:your-auth-token",
    ),
}

# The providers that HAVE a model to choose. One definition, read by `testing.list_models`, by the
# `/providers` endpoint and through it by the UI — a second hardcoded list in the frontend would
# drift, which is the reason `/providers` exists at all rather than the UI knowing the ids.
MODEL_PROVIDERS: frozenset[str] = frozenset({"groq", "anthropic", "openai_compat"})


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
