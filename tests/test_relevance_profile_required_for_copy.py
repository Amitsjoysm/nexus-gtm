"""A workspace with nothing to pitch must not generate outreach.

`get_or_create_profile` gives every new tenant an EMPTY RelevanceProfile, and nothing required it
to be filled before the copy agents ran. `RelevanceContext.to_prompt` then rendered
"PRODUCT CONTEXT: (none provided)" while messaging/call_script fell back to the placeholder value
prop "our platform" — so the model was asked to pitch an unnamed product to a real buyer, with the
triggering signal as the only concrete material in context.

Measured in production 2026-08-31: an account carrying a `hiring` signal produced a JOB
APPLICATION ("opening found, application for position") instead of a sales email. Fluent, plausible
and completely wrong — and everything these agents generate is sent to a real buyer.

These tests pin BOTH directions. The refusal is the new behaviour; the "still works when
configured" half is what stops a future tightening of the guard silently disabling copy for
tenants that are correctly set up.
"""
from __future__ import annotations

from nexus.agents.llm import get_llm_provider
from nexus.agents.runtime import AgentRuntime
from nexus.models.account import Account, Contact
from nexus.models.relevance import RelevanceProfile
from nexus.relevance.engine import RelevanceContext, get_relevance_engine
from tests.conftest import make_tenant, tenant_session

COPY_AGENTS = ("messaging", "call_script")


def _runtime() -> AgentRuntime:
    return AgentRuntime(llm=get_llm_provider(), relevance=get_relevance_engine(), browser=None)


async def _account_with_profile(ts, tenant_id, *, profile: RelevanceProfile | None) -> Account:
    """Seed an account plus a contact, with the given profile (or none at all)."""
    if profile is not None:
        ts.add(profile)
    acc = Account(
        tenant_id=tenant_id, name="Acme", domain="acme.co",
        industry="Software", employee_count=500, country="US",
    )
    ts.add(acc)
    await ts.flush()
    ts.add(Contact(tenant_id=tenant_id, account_id=acc.id, full_name="Jane Doe", title="VP Sales"))
    await ts.flush()
    return acc


# ---------------------------------------------------------------------------------------------
# is_configured — the predicate itself
# ---------------------------------------------------------------------------------------------

def _ctx(*, value_props: list[dict], product_context: str) -> RelevanceContext:
    return RelevanceContext(
        icp_summary="industries Software", value_props=value_props, product_context=product_context
    )


def test_an_empty_profile_is_not_configured():
    assert _ctx(value_props=[], product_context="").is_configured is False


def test_whitespace_product_context_is_not_configured():
    """A field containing only spaces is empty. It reaches the prompt as blank either way."""
    assert _ctx(value_props=[], product_context="   \n  ").is_configured is False


def test_either_field_alone_is_enough():
    """OR, not AND — deliberately.

    `product_context` alone gives the model real material to pitch from even with no structured
    value props; a value prop alone carries a name and the pains it removes. Requiring both would
    refuse copy for tenants who can write perfectly honest outreach.
    """
    assert _ctx(value_props=[], product_context="We sell a GTM platform.").is_configured is True
    assert _ctx(value_props=[{"name": "Faster GTM"}], product_context="").is_configured is True


def test_an_icp_alone_does_not_count():
    """The ICP says WHO to talk to, never WHAT to say.

    This is the exact production state that produced the job application: a complete ICP, and
    nothing to pitch. If this test ever fails, the guard has been widened to accept an ICP and
    the original bug is back.
    """
    ctx = RelevanceContext(
        icp_summary="industries Software; 100-1000 employees", value_props=[], product_context=""
    )
    assert ctx.is_configured is False


# ---------------------------------------------------------------------------------------------
# The agents
# ---------------------------------------------------------------------------------------------

async def test_copy_agents_refuse_on_an_unconfigured_workspace():
    """The refusal must be a returned `error`, never a raise.

    workers/durability.py treats a returned `error` as a NORMAL TERMINAL OUTCOME — not marked
    JOB_FAILED, so not retried and not dead-lettered. Raising would turn every unconfigured
    workspace into a stream of dead letters for a state only a human can fix in the UI.
    """
    for agent in COPY_AGENTS:
        tid = await make_tenant(slug=f"unconf-{agent}")
        async with tenant_session(tid) as ts:
            acc = await _account_with_profile(ts, tid, profile=None)
            result = await _runtime().run(agent, ts, account_id=acc.id)
            assert result.output.get("error") == "relevance_profile_not_configured", (
                f"{agent} generated copy for a workspace with nothing to pitch"
            )
            # Actionable, and it names where to fix it. An error the reader cannot act on sends
            # them to support instead of to the Relevance page.
            assert "Relevance" in result.output.get("detail", "")


async def test_copy_agents_still_run_when_the_profile_is_configured():
    """The half that protects existing, correctly-configured tenants."""
    for agent in COPY_AGENTS:
        tid = await make_tenant(slug=f"conf-{agent}")
        async with tenant_session(tid) as ts:
            acc = await _account_with_profile(ts, tid, profile=RelevanceProfile(
                tenant_id=tid,
                icp={"industries": ["Software"]},
                value_props=[{"name": "Faster GTM", "description": "x", "pains_solved": ["slow onboarding"]}],
                product_context="A GTM intelligence platform.",
            ))
            result = await _runtime().run(agent, ts, account_id=acc.id)
            assert result.status == "completed"
            assert "error" not in result.output, f"{agent} refused a configured workspace"


async def test_product_context_alone_is_enough_to_generate():
    """Pins the OR at the agent boundary, not just on the predicate.

    A tenant that wrote a product description but never filled the structured value-prop table is
    the common half-configured state, and it can still produce honest copy.
    """
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _account_with_profile(ts, tid, profile=RelevanceProfile(
            tenant_id=tid, icp={}, value_props=[],
            product_context="We sell outsourced SDR services to B2B software companies.",
        ))
        result = await _runtime().run("messaging", ts, account_id=acc.id)
        assert result.status == "completed"
        assert "error" not in result.output


async def test_other_agents_are_untouched_by_the_guard():
    """Blast radius check.

    Only messaging and call_script pitch. research, scoring, contact_rec and qa do not sell
    anything to anyone, so an empty profile must not stop them — they are how a rep investigates
    an account *before* deciding what to say.
    """
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _account_with_profile(ts, tid, profile=None)
        for agent in ("research", "scoring", "contact_rec"):
            result = await _runtime().run(agent, ts, account_id=acc.id)
            assert result.output.get("error") != "relevance_profile_not_configured", (
                f"{agent} was caught by a guard meant only for buyer-facing copy"
            )
