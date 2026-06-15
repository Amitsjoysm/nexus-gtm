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
import logging
from dataclasses import dataclass, field

import httpx

from nexus.core.config import get_settings

logger = logging.getLogger("nexus.agents.llm")


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
        if purpose == "clarify_question":
            slot = v.get("slot", "")
            prompts = {
                "industries": "Which industries should I focus on? "
                "(e.g. SaaS, Fintech, Healthcare)",
                "geo": "Which countries or regions are you targeting? "
                "(e.g. United States, United Kingdom)",
                "company_size": "What company size range should I target? "
                "(e.g. 200–5000 employees)",
                "titles": "Which job titles or personas should I look for? "
                "(e.g. VP Sales, CRO)",
            }
            return prompts.get(slot, f"Could you tell me more about {slot or 'your ICP'}?")
        if purpose == "chat_summary":
            icp = v.get("icp_state", {}) or {}
            cap_chars = max(40, get_settings().orch_chat_summary_token_cap * 4)
            parts: list[str] = []
            if icp.get("industries"):
                parts.append("industries: " + ", ".join(map(str, icp["industries"])))
            if icp.get("icp_description"):
                parts.append("desc: " + str(icp["icp_description"]))
            if icp.get("geo"):
                parts.append("geo: " + ", ".join(map(str, icp["geo"])))
            size = icp.get("company_size") or {}
            if size.get("min") or size.get("max"):
                parts.append(f"size: {size.get('min', 0)}–{size.get('max', '∞')}")
            if icp.get("titles"):
                parts.append("titles: " + ", ".join(map(str, icp["titles"])))
            body = "; ".join(parts) if parts else "no ICP details captured yet"
            return (f"Target {v.get('target', 'companies')}; {body}.")[:cap_chars]
        if purpose == "intake_understanding":
            # Deterministic stub: no opinion. Returning empty JSON makes the LLM understanding pass a
            # guaranteed no-op offline, so the regex/keyword intake layer is fully authoritative in CI.
            return "{}"
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


class GroqLLMProvider(OpenAICompatProvider):
    """Groq exposes an OpenAI-compatible /chat/completions endpoint, so it's just preset URL+model."""

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.groq.com/openai/v1"):
        super().__init__(base_url=base_url, api_key=api_key, model=model)


class AnthropicLLMProvider(LLMProvider):
    """Anthropic's native /v1/messages API (not OpenAI-shaped): system is a top-level field and
    only user/assistant turns go in ``messages``."""

    ENDPOINT = "https://api.anthropic.com/v1/messages"
    VERSION = "2023-06-01"

    def __init__(self, api_key: str, model: str):
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
        system = "\n\n".join(m.content for m in messages if m.role == "system") or None
        turns = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ] or [{"role": "user", "content": ""}]
        payload: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": turns,
        }
        if system:
            payload["system"] = system
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.VERSION,
            "content-type": "application/json",
        }
        resp = await self._http().post(self.ENDPOINT, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        )
        tokens = sum(data.get("usage", {}).get(k, 0) for k in ("input_tokens", "output_tokens"))
        return LLMResponse(text=text, tokens=tokens)


class FallbackLLMProvider(LLMProvider):
    """Try providers in order; on any error fall through to the next. The last provider should be
    the stub so completion never fails — a transient Anthropic/Groq outage degrades to deterministic
    output instead of breaking the agent pipeline (and the rep workflow on top of it)."""

    def __init__(self, providers: list[LLMProvider]):
        if not providers:
            raise ValueError("FallbackLLMProvider needs at least one provider")
        self.providers = providers

    async def complete(self, messages, *, temperature=0.2, max_tokens=800,
                       purpose=None, variables=None) -> LLMResponse:
        last_exc: Exception | None = None
        for i, provider in enumerate(self.providers):
            try:
                return await provider.complete(
                    messages, temperature=temperature, max_tokens=max_tokens,
                    purpose=purpose, variables=variables,
                )
            except Exception as exc:  # noqa: BLE001 — fall through to the next provider
                last_exc = exc
                logger.warning("LLM provider %s failed (%r); falling back", type(provider).__name__, exc)
        raise last_exc  # only reached if every provider raised (i.e. no stub at the tail)


def _build_llm_chain(s) -> LLMProvider:
    """Assemble the provider (chain). ``auto`` prefers Anthropic, then Groq, then any configured
    OpenAI-compatible endpoint, always tailed by the stub so completion can't hard-fail."""
    chain: list[LLMProvider] = []
    if s.anthropic_api_key:
        chain.append(AnthropicLLMProvider(s.anthropic_api_key, s.anthropic_model))
    if s.groq_api_key:
        chain.append(GroqLLMProvider(s.groq_api_key, s.groq_model, s.groq_base_url))
    if s.llm_api_key:
        chain.append(OpenAICompatProvider(s.llm_base_url, s.llm_api_key, s.llm_model))
    chain.append(StubLLMProvider())
    return chain[0] if len(chain) == 1 else FallbackLLMProvider(chain)


_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        s = get_settings()
        if s.llm_provider == "auto":
            _provider = _build_llm_chain(s)
        elif s.llm_provider == "anthropic" and s.anthropic_api_key:
            _provider = FallbackLLMProvider(
                [AnthropicLLMProvider(s.anthropic_api_key, s.anthropic_model), StubLLMProvider()]
            )
        elif s.llm_provider == "groq" and s.groq_api_key:
            _provider = FallbackLLMProvider(
                [GroqLLMProvider(s.groq_api_key, s.groq_model, s.groq_base_url), StubLLMProvider()]
            )
        elif s.llm_provider == "openai_compat" and s.llm_api_key:
            _provider = OpenAICompatProvider(s.llm_base_url, s.llm_api_key, s.llm_model)
        else:
            _provider = StubLLMProvider()
    return _provider


def set_llm_provider(provider: LLMProvider) -> None:
    """Test/runtime override."""
    global _provider
    _provider = provider
