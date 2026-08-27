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
from dataclasses import dataclass

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
            # `pain` is a list of PROBLEMS (nouns), so the connective has to take a noun. It used
            # to read "use {vp} to {pain}", which needs a verb, and produced:
            #   "...use Accurate Lead Generation to Stale lists, Duplicate records, ..."
            # See nexus/agents/copy.py. The default here matches that shape too, so a caller that
            # passes nothing still gets a sentence.
            from nexus.agents.copy import DEFAULT_PAINS

            pain = v.get("pain") or DEFAULT_PAINS
            return (
                f"Subject: {vp} for {account}\n\n"
                f"Hi {contact}, noticed {trigger}. Teams like {account} use {vp} to get ahead of "
                f"{pain}. Worth a 15-min look?\n\nBest,\nYour AE"
            )
        if purpose == "call_script":
            import json as _json

            contact = v.get("contact", "there")
            vp = v.get("value_prop", "our platform")
            trigger = v.get("trigger", "your current priorities")
            from nexus.agents.copy import DEFAULT_PAINS

            # Same noun-vs-verb fix as outreach_message. `one_pain` is what the caller passes for
            # the discovery question: naming four problems in a question nobody can answer is how
            # a script stops sounding like a person.
            pain = v.get("pain") or DEFAULT_PAINS
            one_pain = v.get("one_pain") or pain
            return _json.dumps({
                "opener": f"Hi {contact}, this is <your name> from <your company>. "
                          f"Did I catch you at an okay time for 30 seconds?",
                "hook": f"I noticed {trigger} at {account} — that's usually when teams look at {vp}.",
                "value_prop": f"{vp} helps teams get ahead of {pain}.",
                "discovery_questions": [
                    f"How are you handling {one_pain} today?",
                    "What's driving the priority on this right now?",
                    "Who else would be involved in evaluating something like this?",
                ],
                "objections": [
                    {"objection": "We're not looking right now.",
                     "response": f"Totally fair — most teams aren't until {trigger}. "
                                 "Worth 15 minutes to benchmark?"},
                    {"objection": "Send me an email.",
                     "response": "Happy to — what's the best address, and what should I focus it on?"},
                ],
                "cta": "Would you be open to a 15-minute call later this week?",
                "voicemail": f"Hi {contact}, calling about {trigger} at {account}. "
                             "I'll follow up by email — talk soon.",
            })
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

    def __init__(self, base_url: str, api_key: str, model: str, transport=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._transport = transport  # test seam (httpx.MockTransport); None = real network
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60, transport=self._transport)
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


def _prompt_budget() -> int:
    """How many tokens one prompt may spend, from settings when set.

    Runtime-settable, because the ceiling is a property of the ACCOUNT TIER rather than of the
    model — which is exactly how this bug arrived. Switching to `openai/gpt-oss-120b` moved the
    deployment onto an 8,000 TPM tier, and nothing in the codebase knew that number.
    """
    from nexus.agents.token_budget import DEFAULT_MAX_PROMPT_TOKENS

    configured = getattr(get_settings(), "llm_max_prompt_tokens", 0) or 0
    return int(configured) if configured > 0 else DEFAULT_MAX_PROMPT_TOKENS


def _retry_after_seconds(resp) -> float:
    """Seconds the provider asked us to wait. Its number, not a guess of ours.

    Groq returns fractional seconds here (measured: 28-33 on an exhausted minute). Falls back to a
    sane pause rather than zero — a `Retry-After` we cannot parse must not become a busy loop.
    """
    raw = resp.headers.get("retry-after", "")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 5.0


def _error_detail(resp) -> str:
    """The provider's own message. It states the real limit, which is worth more than our constant."""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err)
        return str(err or body)
    except Exception:
        return (getattr(resp, "text", "") or "")[:300]


class GroqLLMProvider(OpenAICompatProvider):
    """Groq exposes an OpenAI-compatible /chat/completions endpoint (preset URL+model).

    Supports a pool of API keys: on a 429 (rate limit) it rotates to the next key and retries,
    so bursty load rides out a single key's limit. If every key is rate-limited the request
    raises, letting :class:`FallbackLLMProvider` degrade to the next provider (ultimately the
    stub) — completion never hard-fails.
    """

    def __init__(
        self,
        api_keys: str | list[str],
        model: str,
        base_url: str = "https://api.groq.com/openai/v1",
        transport=None,
    ):
        keys = [api_keys] if isinstance(api_keys, str) else list(api_keys)
        keys = [k.strip() for k in keys if k and k.strip()]
        if not keys:
            raise ValueError("GroqLLMProvider needs at least one API key")
        super().__init__(base_url=base_url, api_key=keys[0], model=model, transport=transport)
        self._keys = keys
        self._idx = 0  # sticky: start each call from the last key that worked

    async def _refresh_keys(self) -> None:
        """Re-read the managed pool so a key added in the Control plane reaches a RUNNING process.

        The worker is a separate container, so nothing the API does can invalidate its memory.
        Uses the MANAGED pool, not `key_pool`: the latter falls back to the environment,
        so refreshing against it would overwrite keys a caller passed explicitly. The
        lookup is TTL-cached, so this is a dict read on all but one call in thirty
        seconds — cheap enough to sit on the hot path, and the only thing that makes "add a key and
        it just works" true without a restart.

        Resets the index to 0 so the operator's PINNED key is tried first after a change; rotation
        is meant to be the failure path, not the resting state.
        """
        try:
            from nexus.providers.resolver import managed_pool, model_for

            pool = await managed_pool("groq")
            # The MODEL is resolved here too, and for the same reason a key is: a withdrawn model
            # is exactly as fatal as a revoked key. Measured 2026-08-21 —
            # `llama-3.3-70b-versatile` was retired, every key 404'd, and every draft came from
            # the stub. Changing it must not require a redeploy either.
            chosen = await model_for("groq")
        except Exception:  # never let key management break the call it is meant to serve
            return
        if pool and pool != self._keys:
            self._keys = pool
            self._idx = 0
        if chosen and chosen != self.model:
            self.model = chosen

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.2,
        max_tokens: int = 800,
        purpose: str | None = None,
        variables: dict | None = None,
    ) -> LLMResponse:
        # BEFORE the payload is built: the refresh can change `self.model`, and a payload
        # assembled first would send the stale one and only pick the new model up next call.
        await self._refresh_keys()

        # Fit the prompt to the account's per-request token ceiling BEFORE sending. Measured
        # 2026-08-26: this account is capped at 8,000 TPM, and a request above it returns 413
        # "Request too large ... on tokens per minute", which no retry and no key rotation can fix.
        # Trimming here is what makes the whole thing work on a small tier; everything below only
        # handles the cases where traffic, not size, is the problem.
        from nexus.agents.token_budget import GATE, fit_messages, parse_limit

        budget = _prompt_budget()
        messages, trimmed = fit_messages(list(messages), budget)
        if trimmed:
            logger.info("prompt trimmed to fit %d tokens for %s", budget, purpose or "completion")

        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        last_resp: httpx.Response | None = None
        # Bounded process-wide. The token budget belongs to the account and is shared by every
        # endpoint — `/enrich`, `/lookalikes` and `/source-contacts` fired in the same second in the
        # reported failure, and each holding its own limiter would have allowed exactly that.
        async with GATE():
            return await self._complete_bounded(payload, messages, budget, max_tokens, purpose)

    async def _complete_bounded(self, payload, messages, budget, max_tokens, purpose):
        import asyncio

        from nexus.agents.token_budget import fit_messages, parse_limit

        last_resp: httpx.Response | None = None
        waited = 0.0
        # One pass per key plus a couple of waits: the keys are separate organizations with separate
        # budgets (verified by reading four different org ids out of four 429s), so rotating is
        # worth doing FIRST — but once they are all exhausted only time helps.
        for attempt in range(len(self._keys) + 2):
            key = self._keys[self._idx]
            resp = await self._http().post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
            )
            if resp.status_code == 429:
                # Rotate FIRST — each key is a separate organization with its own minute budget, so
                # the next one may well be clear. Only once we have been round them all is waiting
                # the answer, and then `Retry-After` says how long (measured: 28-33 seconds).
                #
                # The old code rotated with no backoff at all, so a burst burned every key within a
                # second or two and raised — and the caller fell through to the stub, whose output
                # is emailed to real prospects.
                retry_after = _retry_after_seconds(resp)
                logger.warning(
                    "Groq key #%d rate-limited (429), retry-after=%.0fs; %s",
                    self._idx, retry_after,
                    "rotating" if attempt < len(self._keys) else "waiting",
                )
                self._idx = (self._idx + 1) % len(self._keys)
                last_resp = resp
                if attempt >= len(self._keys):
                    # Cap the wait: a caller blocked for a full minute is worse than a clear
                    # failure it can retry, and the job queue will bring the work back.
                    delay = min(retry_after, 20.0)
                    waited += delay
                    await asyncio.sleep(delay)
                continue

            if resp.status_code == 413:
                # A single request bigger than the whole minute budget. Rotation and waiting are
                # both useless — only a smaller prompt works. Shrink to whatever the provider says
                # its limit actually is and try once more, because its number is current and ours
                # is a constant that the account tier can move under.
                detail = _error_detail(resp)
                stated = parse_limit(detail)
                if stated and stated < budget:
                    budget = int(stated * 0.75)
                else:
                    budget = max(512, budget // 2)
                messages, shrank = fit_messages(messages, budget)
                logger.warning(
                    "Groq refused a request as too large (413); retrying at %d tokens. %s",
                    budget, detail[:160],
                )
                if not shrank:
                    # Already at the floor and still refused: say so plainly rather than looping.
                    raise RuntimeError(
                        f"Groq refused this request as too large even after trimming to {budget} "
                        f"tokens: {detail[:200]}"
                    )
                payload["messages"] = [{"role": m.role, "content": m.content} for m in messages]
                last_resp = resp
                continue
            if resp.status_code in (401, 403):
                # Condemns the KEY, not the request — so rotate past it, exactly as
                # `integrations/apify.py` and the search engines now do. Rotation used to fire on
                # 429 alone, which meant one revoked key raised straight out of here, the
                # FallbackLLMProvider caught it, and EVERY draft came from the stub while four
                # working keys sat unused. The stub's output is emailed to real prospects, so that
                # is not a graceful degradation.
                logger.warning("Groq key #%d rejected (%d); rotating past it",
                               self._idx, resp.status_code)
                # Record it against the managed row so the Control plane shows this without anyone
                # having to press Test. No-op when the key came from the environment pool.
                from nexus.providers.service import record_rejection_from_response

                await record_rejection_from_response("groq", self._keys[self._idx], resp)
                self._idx = (self._idx + 1) % len(self._keys)
                last_resp = resp
                continue
            if resp.status_code == 404:
                # Not a key problem, and rotating cannot help: 404 here means the MODEL does not
                # exist for this account. Measured 2026-08-21: all five configured keys returned
                # 404 for `llama-3.3-70b-versatile`, which Groq had withdrawn — so the chain fell
                # to the stub on every call and nothing said why. Say it once, loudly, and stop.
                detail = ""
                try:
                    detail = str(resp.json().get("error", {}).get("message", ""))[:200]
                except Exception:
                    pass
                raise RuntimeError(
                    f"Groq rejected model {self.model!r} (404): {detail or 'model not found'}. "
                    "Rotating keys cannot fix this — set NEXUS_GROQ_MODEL to a model this account "
                    "can use (GET {base}/models lists them).".format(base=self.base_url)
                )
            resp.raise_for_status()  # other HTTP errors propagate to the fallback chain
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            tokens = data.get("usage", {}).get("total_tokens", 0)
            return LLMResponse(text=text, tokens=tokens)
        # Every key was rate-limited or rejected: raise so the caller can fall back. The message no
        # longer says "rate-limited" unconditionally — a pool of revoked keys and a pool of
        # throttled keys need opposite fixes, and blaming the rate limit sends an operator to wait
        # it out when the credentials are the problem.
        last_resp.raise_for_status()  # type: ignore[union-attr]
        raise RuntimeError(  # pragma: no cover (defensive)
            f"all {len(self._keys)} Groq key(s) were rate-limited or rejected"
        )


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


class CostTrackingProvider(LLMProvider):
    """Delegates to a real provider and reports what the call cost.

    Wrapping here rather than inside each provider means every model call is measured exactly
    once, whichever backend or fallback served it — there is no path to the network that skips
    the meter. It reports COGS only; the billable *action* is metered at the endpoint, because
    one endpoint may make several model calls and no customer recognizes "a token" as a unit.
    """

    def __init__(self, inner: LLMProvider, usd_per_1k_tokens: float):
        self.inner = inner
        self.usd_per_1k_tokens = usd_per_1k_tokens

    async def complete(self, messages, *, temperature=0.2, max_tokens=800,
                       purpose=None, variables=None) -> LLMResponse:
        from nexus.billing.context import report_cost

        resp = await self.inner.complete(
            messages, temperature=temperature, max_tokens=max_tokens,
            purpose=purpose, variables=variables,
        )
        # A no-op when nothing is metering, so background jobs and scripts are unaffected.
        report_cost(
            usd=(resp.tokens or 0) / 1000 * self.usd_per_1k_tokens,
            tokens=resp.tokens or 0,
            source=type(self.inner).__name__,
        )
        return resp


def _build_llm_chain(s) -> LLMProvider:
    """Assemble the provider (chain). ``auto`` prefers Anthropic, then Groq, then any configured
    OpenAI-compatible endpoint, always tailed by the stub so completion can't hard-fail."""
    chain: list[LLMProvider] = []
    if s.anthropic_api_key:
        chain.append(AnthropicLLMProvider(s.anthropic_api_key, s.anthropic_model))
    if s.groq_api_key_list:
        chain.append(GroqLLMProvider(s.groq_api_key_list, s.groq_model, s.groq_base_url))
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
        elif s.llm_provider == "groq" and s.groq_api_key_list:
            _provider = FallbackLLMProvider(
                [GroqLLMProvider(s.groq_api_key_list, s.groq_model, s.groq_base_url), StubLLMProvider()]
            )
        elif s.llm_provider == "openai_compat" and s.llm_api_key:
            _provider = OpenAICompatProvider(s.llm_base_url, s.llm_api_key, s.llm_model)
        else:
            _provider = StubLLMProvider()
        # Measure whatever we ended up with — one wrapper, no bypass. `set_llm_provider` is
        # deliberately left unwrapped so test injection stays exact.
        _provider = CostTrackingProvider(_provider, s.llm_usd_per_1k_tokens)
    return _provider


def set_llm_provider(provider: LLMProvider) -> None:
    """Test/runtime override."""
    global _provider
    _provider = provider
