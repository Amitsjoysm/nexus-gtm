"""Agent runtime: builds relevance-grounded context, runs an agent, records an audit trail."""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field

from nexus.agents.llm import LLMMessage, LLMProvider, get_llm_provider
from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact
from nexus.models.intelligence import AgentRun
from nexus.models.signal import SignalEvent
from nexus.relevance import RelevanceContext, RelevanceEngine, get_relevance_engine
from nexus.relevance.engine import get_profile


@dataclass
class AgentContext:
    ts: TenantSession
    llm: LLMProvider
    relevance: RelevanceEngine
    browser: object | None
    registry: object | None
    account: Account | None
    contacts: list[Contact]
    signals: list[SignalEvent]
    relevance_context: RelevanceContext
    inputs: dict
    tokens: int = 0

    async def complete(
        self, messages: list[LLMMessage], *, purpose=None, variables=None, **kw
    ) -> str:
        resp = await self.llm.complete(messages, purpose=purpose, variables=variables, **kw)
        self.tokens += resp.tokens
        return resp.text

    def system_message(self) -> LLMMessage:
        return LLMMessage(role="system", content=self.relevance_context.to_prompt())


@dataclass(slots=True)
class AgentResult:
    agent: str
    status: str
    output: dict = field(default_factory=dict)
    error: str | None = None
    latency_ms: int = 0
    tokens: int = 0
    run_id: str | None = None


class BaseAgent(abc.ABC):
    name: str

    @abc.abstractmethod
    async def run(self, ctx: AgentContext) -> dict: ...


_REGISTRY: dict[str, BaseAgent] = {}
_loaded = False


def register_agent(agent: BaseAgent) -> None:
    _REGISTRY[agent.name] = agent


def _ensure_agents_loaded() -> None:
    global _loaded
    if _loaded:
        return
    # Import for side-effect registration. Done lazily to avoid an import cycle.
    from nexus.agents import (  # noqa: F401
        call_script,
        contact_rec,
        discovery,
        messaging,
        qa,
        research,
        scoring,
    )

    _loaded = True


def available_agents() -> list[str]:
    _ensure_agents_loaded()
    return sorted(_REGISTRY)


class AgentRuntime:
    def __init__(
        self,
        llm: LLMProvider,
        relevance: RelevanceEngine,
        browser: object | None = None,
        registry: object | None = None,
    ):
        self.llm = llm
        self.relevance = relevance
        self.browser = browser
        self._registry = registry

    @property
    def registry(self):
        """Lazily build the DataSourceRegistry from settings, threading in this runtime's
        browser so injected (test) browsers reach the web-search-backed sources."""
        if self._registry is None:
            from nexus.integrations.registry import build_registry_from_settings

            self._registry = build_registry_from_settings(browser=self.browser)
        return self._registry

    async def _load_context(
        self, ts: TenantSession, account_id: str | None, inputs: dict
    ) -> AgentContext:
        account = await ts.get(Account, account_id) if account_id else None
        contacts: list[Contact] = []
        signals: list[SignalEvent] = []
        if account is not None:
            contacts = await ts.list(Contact, Contact.account_id == account.id)
            stmt = (
                ts.select(SignalEvent, SignalEvent.account_id == account.id)
                .order_by(SignalEvent.occurred_at.desc())
                .limit(50)
            )
            signals = list((await ts.session.scalars(stmt)).all())
        profile = await get_profile(ts)
        rc = self.relevance.build_context(profile, account, contacts, signals)
        return AgentContext(
            ts=ts,
            llm=self.llm,
            relevance=self.relevance,
            browser=self.browser,
            registry=self.registry,
            account=account,
            contacts=contacts,
            signals=signals,
            relevance_context=rc,
            inputs=inputs,
        )

    async def run(
        self,
        agent_name: str,
        ts: TenantSession,
        *,
        account_id: str | None = None,
        persist: bool = True,
        **inputs,
    ) -> AgentResult:
        _ensure_agents_loaded()
        agent = _REGISTRY.get(agent_name)
        if agent is None:
            raise ValueError(f"Unknown agent '{agent_name}'. Available: {available_agents()}")

        ctx = await self._load_context(ts, account_id, inputs)
        started = time.perf_counter()
        status, output, error = "completed", {}, None
        try:
            output = await agent.run(ctx)
        except Exception as exc:  # agents must surface, not crash the request
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.perf_counter() - started) * 1000)

        run = AgentRun(
            tenant_id=ts.tenant_id,
            agent=agent_name,
            account_id=account_id,
            status=status,
            input=inputs,
            output=output,
            error=error,
            tokens=ctx.tokens,
            latency_ms=latency_ms,
        )
        if persist:
            ts.add(run)
            await ts.flush()

        return AgentResult(
            agent=agent_name,
            status=status,
            output=output,
            error=error,
            latency_ms=latency_ms,
            tokens=ctx.tokens,
            run_id=run.id,
        )


_runtime: AgentRuntime | None = None


def get_agent_runtime() -> AgentRuntime:
    global _runtime
    if _runtime is None:
        from nexus.enrichment import get_browser_provider

        _runtime = AgentRuntime(
            llm=get_llm_provider(),
            relevance=get_relevance_engine(),
            browser=get_browser_provider(),
        )
    return _runtime


def reset_agent_runtime() -> None:
    """Test hook to rebuild the runtime (e.g., after swapping the LLM provider)."""
    global _runtime
    _runtime = None
