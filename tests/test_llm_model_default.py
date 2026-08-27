# tests/test_llm_model_default.py
"""The default model must be one the default provider can actually serve.

Measured on the live deployment 2026-08-27: `llm_model` defaulted to `gpt-4o-mini`, an **OpenAI**
model name, while `llm_provider="auto"` builds a **Groq** chain. Groq answers::

    HTTP 404  {"message": "The model `gpt-4o-mini` does not exist or you do not have access to it.",
               "code": "model_not_found"}

`GroqLLMProvider` swallows that and the caller receives an empty string, so every AI feature
degraded in silence:

* website analysis returned "couldn't analyze that site" on every URL,
* Suggest Titles fell through to ``_DEFAULT_COMMITTEE`` in ``integrations/contact_search.py`` —
  which is *literally* the generic CTO / Head of Sales list a tester reported,
* ``_extract_people`` parsed ``''`` into ``[]``, so contact search found nobody at all,
* personalization had no contacts left to personalize for.

Four separate bug reports, one wrong string. This is the **second** occurrence of the shape — see
CLAUDE.md, 2026-08-21, where `llama-3.3-70b-versatile` was withdrawn and the stub wrote outbound
email to real prospects. Observing it needs a live provider, so a unit test on the default is the
only thing that catches it before a deploy does.
"""
from __future__ import annotations

# Model families each provider serves. Deliberately prefixes rather than exact ids: ids churn
# constantly (`llama-3.3-70b-versatile` was withdrawn under us), and pinning them would make this
# fail on every vendor rename — which trains people to edit the test rather than read it. The
# prefix encodes "this name belongs to that provider's namespace", which is the actual mistake.
_SERVABLE: dict[str, tuple[str, ...]] = {
    "groq": (
        "llama", "meta-llama/", "openai/gpt-oss", "qwen/", "deepseek", "kimi", "mixtral", "gemma",
    ),
    "anthropic": ("claude-",),
}


def _default_chain(provider: str) -> str:
    """Which provider actually serves a completion for this setting.

    ``auto`` is the shipped default and builds ``FallbackLLMProvider([Groq, Stub])`` — so the model
    name is handed to **Groq**, and a name Groq does not know means the stub answers everything.
    """
    provider = (provider or "auto").lstrip("=").strip().lower()
    return "groq" if provider in ("auto", "groq") else provider


def test_the_default_model_is_servable_by_the_default_provider():
    """Reads the DECLARED CLASS DEFAULTS, never `Settings(...)`.

    Instantiating resolves the local `.env`, which sets `llm_provider=stub` for offline
    development — so an instance-based check falls through the `not in _SERVABLE` branch and passes
    without asserting anything. It did exactly that on the first run of this test, against the very
    `gpt-4o-mini` value that was breaking production. What ships to a deployment setting neither
    variable is the class default, so that is the only thing worth pinning here.
    """
    from nexus.core.config import Settings

    fields = Settings.model_fields
    provider_default = fields["llm_provider"].default
    model_default = (fields["llm_model"].default or "").lower()

    # `llm_provider` ships as "stub" so the offline suite needs no credentials, and every real
    # deployment sets NEXUS_LLM_PROVIDER=auto — which is Groq. The stub ignores the model name
    # entirely, so it constrains nothing; Groq is the provider that has to be able to serve it, and
    # production is the only place the mismatch shows up. Hence: pin the model against Groq
    # regardless of what the provider default happens to be.
    assert provider_default in ("stub", "auto", "groq"), (
        f"llm_provider default is {provider_default!r}; this test assumes production runs 'auto' "
        f"(-> Groq). Re-check which namespace the model default must satisfy."
    )

    chain = "groq"
    assert any(model_default.startswith(p) for p in _SERVABLE[chain]), (
        f"llm_model default {fields['llm_model'].default!r} is not in {chain}'s namespace, and "
        f"every real deployment runs NEXUS_LLM_PROVIDER=auto, which is {chain}. {chain} answers "
        f"404 and the provider returns '' rather than raising, so every AI feature degrades with "
        f"nothing reporting a fault. Expected a name starting with one of {_SERVABLE[chain]}."
    )


def test_gpt_4o_mini_is_not_considered_groq_servable():
    """Pins the exact value that took the live deployment's AI features down on 2026-08-27, so a
    later widening of the prefix list cannot quietly readmit it."""
    assert not any("gpt-4o-mini".startswith(p) for p in _SERVABLE["groq"]), (
        "gpt-4o-mini must never read as Groq-servable — it is an OpenAI name, and it is what "
        "caused four separate user-visible failures on the live site"
    )


def test_the_auto_provider_resolves_to_groq():
    """The premise the first test rests on. If `auto` ever stops meaning Groq, that test would go
    on passing while checking the wrong provider's namespace."""
    assert _default_chain("auto") == "groq"
    assert _default_chain("=auto") == "groq", "deploy/.env carries a stray '=' on this value"
