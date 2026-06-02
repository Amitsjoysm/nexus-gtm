"""LLM provider interface and adapters.

The interface is intentionally tiny. The default :class:`StubLLMProvider` is deterministic and
needs no API key, so the entire agent pipeline runs in tests/CI offline. Swap in
:class:`OpenAICompatProvider` (OpenAI, vLLM, Ollama, any compatible endpoint) by setting
``NEXUS_LLM_PROVIDER=openai_compat``.

Agents pass an optional ``purpose`` and ``variables`` alongside the messages. Real providers
ignore them (the messages already contain everything); the stub uses them to render readable,
deterministic templates so offline output still looks like a real agent's.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field

import httpx

from nexus.core.config import get_settings


@dataclass(slots=True)
class LLMMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(slots=True)
class LLMResponse:
    text: str
    tokens: int = 0


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 800,
        purpose: str | None = None,
        variables: dict | None = None,
    ) -> LLMResponse: ...


class StubLLMProvider(LLMProvider):
    """Deterministic, dependency-free provider for dev/test and zero-key operation."""

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 800,
        purpose: str | None = None,
        variables: dict | None = None,
    ) -> LLMResponse:
        v = variables or {}
        text = self._render(purpose, v, messages)
        return LLMResponse(text=text, tokens=len(text.split()))

    def _render(self, purpose: str | None, v: dict, messages: list[LLMMessage]) -> str:
        account = v.get("account", "the account")
        if purpose == "research_brief":
            facts = v.get("facts", [])
            bullets = "\n".join(f"- {f}" for f in facts) or "- No external facts retrieved."
            return (
                f"Research brief for {account}:\n{bullets}\n"
                f"Why it matters: aligns with our ICP and recent signals."
            )
        if purpose == "scoring_rationale":
            return (
                f"{account} scores {v.get('composite', 0)}/100 overall "
                f"(ICP fit {v.get('icp_fit', 0)}, intent {v.get('intent', 0)}, "
                f"health {v.get('health', 0)}). {v.get('drivers', '')}".strip()
            )
        if purpose == "outreach_message":
            vp = v.get("value_prop", "our platform")
            trigger = v.get("trigger", "your recent initiative")
            contact = v.get("contact", "there")
            return (
                f"Subject: {vp} for {account}\n\n"
                f"Hi {contact}, noticed {trigger}. Teams like {account} use {vp} to "
                f"{v.get('pain', 'hit their goals faster')}. Worth a 15-min look?\n\nBest,\nYour AE"
            )
        if purpose == "contact_rationale":
            return (
                f"{v.get('contact', 'This person')} ({v.get('title', 'n/a')}) is a strong entry "
                f"point: seniority and function map to the buying committee for {account}."
            )
        if purpose == "account_qa":
            default_answer = (
                "Based on the available context, this account aligns with our ICP "
                "and has active signals worth pursuing."
            )
            return f"Answer about {account}: {v.get('answer', default_answer)}"
        # Fallback: echo the last user message compactly.
        last = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return f"[stub] {last[:280]}"


class OpenAICompatProvider(LLMProvider):
    """Calls any OpenAI-compatible /chat/completions endpoint."""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60)
        return self._client

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 800,
        purpose: str | None = None,
        variables: dict | None = None,
    ) -> LLMResponse:
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        resp = await self._http().post(
            f"{self.base_url}/chat/completions", json=payload, headers=headers
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {}).get("total_tokens", 0)
        return LLMResponse(text=text, tokens=tokens)


_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        s = get_settings()
        if s.llm_provider == "openai_compat" and s.llm_api_key:
            _provider = OpenAICompatProvider(s.llm_base_url, s.llm_api_key, s.llm_model)
        else:
            _provider = StubLLMProvider()
    return _provider


def set_llm_provider(provider: LLMProvider) -> None:
    """Test/runtime override."""
    global _provider
    _provider = provider
