"""Hermes adapter.

NousResearch's `hermes-agent` is a standalone agent application (CLI + messaging gateways),
not an embeddable Python LLM SDK. The clean way to use it from NEXUS is to point our
:class:`LLMProvider` at the OpenAI-compatible model gateway Hermes exposes, so NEXUS keeps
owning the agent orchestration while Hermes (and its 200+ model backends) serves inference.

Usage:
    set NEXUS_LLM_PROVIDER=openai_compat and NEXUS_LLM_BASE_URL=<hermes gateway url>
    or construct :class:`HermesProvider` directly and pass it to the AgentRuntime.
"""
from __future__ import annotations

from nexus.agents.llm import OpenAICompatProvider


class HermesProvider(OpenAICompatProvider):
    """Thin alias pointing the OpenAI-compatible client at a Hermes gateway."""

    def __init__(self, gateway_url: str, api_key: str = "hermes", model: str = "hermes"):
        super().__init__(base_url=gateway_url, api_key=api_key, model=model)
