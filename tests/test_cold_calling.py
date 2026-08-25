"""Cold calling: AI script agent, call queue, dispositions. Offline (stub LLM)."""
from __future__ import annotations

from nexus.agents.runtime import get_agent_runtime
from nexus.calling.provider import StubCallProvider
from nexus.calling.service import get_call_queue_service
from nexus.models.account import Account, Contact
from nexus.models.calling import (
    CALL_DONE,
    CALL_OPEN,
    DISP_MEETING_BOOKED,
    DISP_NO_ANSWER,
    CallActivity,
)
from tests.conftest import make_tenant, tenant_session


async def _account_with_contact(ts, tid):
    acc = Account(tenant_id=tid, name="Acme", domain="acme.co")
    ts.add(acc)
    await ts.flush()
    c = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe", title="VP Sales",
                phone="+15551234567")
    ts.add(c)
    await ts.flush()
    return acc, c


async def test_call_script_agent_returns_structured_script():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc, c = await _account_with_contact(ts, tid)
        result = await get_agent_runtime().run(
            "call_script", ts, account_id=acc.id, contact_id=c.id, persist=False
        )
    assert result.status == "completed"
    script = result.output["script"]
    # All sections present and usable offline (deterministic stub).
    for key in ("opener", "hook", "value_prop", "discovery_questions", "objections", "cta", "voicemail"):
        assert key in script
    assert isinstance(script["discovery_questions"], list) and script["discovery_questions"]
    assert isinstance(script["objections"], list) and "response" in script["objections"][0]


async def test_enqueue_is_idempotent_per_contact():
    tid = await make_tenant()
    svc = get_call_queue_service()
    async with tenant_session(tid) as ts:
        acc, c = await _account_with_contact(ts, tid)
        t1 = await svc.enqueue(ts, account_id=acc.id, contact_id=c.id, reason="hot signal")
        t2 = await svc.enqueue(ts, account_id=acc.id, contact_id=c.id, reason="again")
        assert t1.id == t2.id  # one OPEN task per contact
        assert len(await svc.list_queue(ts)) == 1


async def test_list_queue_orders_by_priority_desc():
    tid = await make_tenant()
    svc = get_call_queue_service()
    async with tenant_session(tid) as ts:
        acc, c = await _account_with_contact(ts, tid)
        c2 = Contact(tenant_id=tid, account_id=acc.id, full_name="Bob Roe")
        ts.add(c2)
        await ts.flush()
        await svc.enqueue(ts, account_id=acc.id, contact_id=c.id, priority=20)
        await svc.enqueue(ts, account_id=acc.id, contact_id=c2.id, priority=90)
        names = [t.contact_id for t in await svc.list_queue(ts)]
        assert names == [c2.id, c.id]  # 90 before 20


async def test_generate_script_caches_on_task():
    tid = await make_tenant()
    svc = get_call_queue_service()
    async with tenant_session(tid) as ts:
        acc, c = await _account_with_contact(ts, tid)
        task = await svc.enqueue(ts, account_id=acc.id, contact_id=c.id)
        script = await svc.generate_script(ts, task)
        assert script.get("opener")
        assert task.script_cache == script  # cached on the task


async def test_disposition_terminal_closes_requeue_keeps_open():
    tid = await make_tenant()
    svc = get_call_queue_service()
    async with tenant_session(tid) as ts:
        acc, c = await _account_with_contact(ts, tid)
        task = await svc.enqueue(ts, account_id=acc.id, contact_id=c.id)

        # no_answer -> re-queue: task stays open, activity logged.
        act1 = await svc.log_disposition(ts, task.id, disposition=DISP_NO_ANSWER, notes="vm full")
        await ts.session.refresh(task)
        assert isinstance(act1, CallActivity) and task.status == CALL_OPEN

        # meeting_booked -> terminal: task closes.
        await svc.log_disposition(ts, task.id, disposition=DISP_MEETING_BOOKED, next_step="demo Tue")
        await ts.session.refresh(task)
        assert task.status == CALL_DONE
        assert len(await svc.list_activities(ts, contact_id=c.id)) == 2


def test_setup_cadence_goal_is_registered():
    from nexus.orchestration.planner import available_goals

    assert "setup_cadence" in available_goals()


async def _run_setup_cadence(ts, goal_input):
    from types import SimpleNamespace

    from nexus.orchestration.tools import SetupCadenceTool, ToolContext

    run = SimpleNamespace(goal_input=goal_input, blackboard={},
                          created_by_user_id=None, account_id=None)
    tc = ToolContext(ts=ts, runtime=None, run=run, inputs={})
    return await SetupCadenceTool().run(tc)


async def test_orchestrator_sets_up_call_cadence_from_steps():
    from nexus.cadences.service import get_cadence_service

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        out = await _run_setup_cadence(ts, {
            "cadence_name": "Cold Call Seq",
            "steps": [{"channel": "email", "delay_days": 0},
                      {"channel": "call", "delay_days": 2}],
        })
        assert out["name"] == "Cold Call Seq"
        steps = await get_cadence_service().list_steps(ts, out["cadence_id"])
        assert [s.channel for s in steps] == ["email", "call"]


async def test_orchestrator_defaults_to_cold_calling_3touch():
    """No steps given -> the orchestrator picks a sensible email -> call -> email sequence."""
    from nexus.cadences.service import get_cadence_service

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        out = await _run_setup_cadence(ts, {})
        steps = await get_cadence_service().list_steps(ts, out["cadence_id"])
        assert [s.channel for s in steps] == ["email", "call", "email"]


def test_stub_call_provider_returns_tel_link():
    import asyncio

    prov = StubCallProvider()
    handle = asyncio.get_event_loop().run_until_complete(
        prov.place_call(to="+1 (555) 123-4567", from_="+15550000000")
    )
    assert handle.mode == "manual"
    assert handle.dial_url == "tel:+15551234567"


# ---- an unimplemented provider must not read as a working one -----------------------------------


def test_the_offline_default_is_click_to_dial():
    from nexus.calling.provider import build_call_provider

    for name in ("", "stub", "none", "STUB"):
        assert build_call_provider(name).name == "stub"


def test_a_provider_with_no_implementation_raises_rather_than_stubbing():
    """`build_call_provider` returned StubCallProvider for EVERY input, so an unbuildable
    provider name behaved exactly like leaving it blank with nothing logged.

    Click-to-dial works, so an operator who set one saw calls "working" and had no way to learn
    that their provider was never involved. That is the same failure as the personalization
    provider that silently returned the stub for every input.

    `twilio` is a real implementation now, so this uses a name that still has none — the
    guarantee under test is about unbuildable names, not about Twilio specifically.
    """
    import pytest

    from nexus.calling.provider import TelephonyNotImplemented, build_call_provider

    with pytest.raises(TelephonyNotImplemented) as exc:
        build_call_provider("vonage")
    # The message has to say what IS available, or it just moves the confusion.
    assert "vonage" in str(exc.value).lower()
    assert "click-to-dial" in str(exc.value)


def test_twilio_without_credentials_raises_rather_than_stubbing(monkeypatch):
    """The same guarantee, one step further in: an implemented provider with no keys must also
    refuse. Building the stub here would recreate the exact silence this suite exists to prevent
    — configured Twilio, click-to-dial behaviour, no error anywhere."""
    import pytest

    from nexus.calling.provider import TelephonyNotConfigured, build_call_provider

    monkeypatch.delenv("NEXUS_TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("NEXUS_TWILIO_AUTH_TOKEN", raising=False)
    with pytest.raises(TelephonyNotConfigured) as exc:
        build_call_provider("twilio")
    assert "NEXUS_TWILIO_ACCOUNT_SID" in str(exc.value)


async def test_the_app_refuses_to_start_on_a_provider_it_cannot_build(monkeypatch):
    """Resolved once at boot in `lifespan`, so the mistake surfaces on deploy rather than on the
    first rep's first call. Both failure modes are caught there: a name with no implementation,
    and a real provider that was never keyed."""
    import pytest

    from nexus.calling import provider as provider_mod
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "telephony_provider", "vonage")
    monkeypatch.setattr(provider_mod, "_provider", None)
    with pytest.raises(provider_mod.TelephonyNotImplemented):
        provider_mod.get_call_provider()

    monkeypatch.delenv("NEXUS_TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("NEXUS_TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(get_settings(), "telephony_provider", "twilio")
    monkeypatch.setattr(provider_mod, "_provider", None)
    with pytest.raises(provider_mod.TelephonyNotConfigured):
        provider_mod.get_call_provider()
    monkeypatch.setattr(provider_mod, "_provider", None)
