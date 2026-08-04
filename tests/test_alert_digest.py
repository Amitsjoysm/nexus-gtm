# tests/test_alert_digest.py
"""M21's missing half: actually sending the alerts routing held back.

`routing.py` decided an alert should wait for a digest — because the user chose `mode="digest"`, or
because it arrived inside their quiet hours — and nothing ever acted on that decision. The alert
existed, the routing was correct, and the person was never told. Exactly the shape of the bug M21
was created to fix, where `signal.created` was published to no subscriber.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nexus.alerts.digest import collect_digest, run_digest_sweep
from tests.conftest import make_tenant, tenant_session


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _pref(ts, *, user_id="u1", mode="digest", category="all", last=None):
    from nexus.models.notification_preference import NotificationPreference

    pref = NotificationPreference(
        user_id=user_id, category=category, channel="email", mode=mode, last_digest_at=last,
    )
    ts.add(pref)
    await ts.flush()
    return pref


async def _alert(ts, *, category="funding", status="open", title="Acme raised $40M"):
    from nexus.models.alerts import Alert

    alert = Alert(title=title, status=status, severity="info", meta={"category": category})
    ts.add(alert)
    await ts.flush()
    return alert


# ---- the gap this closes ---------------------------------------------------------------------------

async def test_a_held_alert_is_actually_delivered():
    """The whole point. Routing deferring an alert and nothing sending it is the bug."""
    sent = []

    async def capture(ts, pref, batch):
        sent.append(batch)

    tid = await make_tenant(slug="dg1", name="DG1")
    async with tenant_session(tid) as ts:
        await _pref(ts)
        await _alert(ts)
        result = await run_digest_sweep(ts, send=capture)

    assert result["sent"] == 1
    assert len(sent) == 1 and sent[0].count == 1


async def test_a_quiet_period_sends_nothing():
    """An empty digest every morning is how a channel gets muted — and then the one that matters
    is muted too."""
    sent = []

    async def capture(ts, pref, batch):
        sent.append(batch)

    tid = await make_tenant(slug="dg2", name="DG2")
    async with tenant_session(tid) as ts:
        await _pref(ts)
        result = await run_digest_sweep(ts, send=capture)

    assert sent == []
    assert result["empty"] == 1


async def test_the_watermark_advances_on_an_empty_run():
    """Otherwise the sweep re-scans the same empty window on every heartbeat tick, forever."""
    tid = await make_tenant(slug="dg3", name="DG3")
    async with tenant_session(tid) as ts:
        pref = await _pref(ts)
        await run_digest_sweep(ts)
        assert pref.last_digest_at is not None


async def test_nobody_is_told_the_same_thing_twice():
    sent = []

    async def capture(ts, pref, batch):
        sent.append(batch)

    tid = await make_tenant(slug="dg4", name="DG4")
    async with tenant_session(tid) as ts:
        await _pref(ts)
        await _alert(ts)
        await run_digest_sweep(ts, send=capture)
        await run_digest_sweep(ts, send=capture)

    assert len(sent) == 1, "the second sweep must find nothing new"


async def test_a_failed_delivery_leaves_the_watermark_alone():
    """Advancing it on failure silently swallows the one digest that did not send."""
    async def boom(ts, pref, batch):
        raise RuntimeError("smtp down")

    tid = await make_tenant(slug="dg5", name="DG5")
    async with tenant_session(tid) as ts:
        pref = await _pref(ts)
        await _alert(ts)
        result = await run_digest_sweep(ts, send=boom)

        assert result["sent"] == 0
        assert pref.last_digest_at is None, "the next sweep must retry"


# ---- what belongs in a digest ------------------------------------------------------------------------

async def test_an_immediate_preference_is_not_swept():
    """It was already delivered. Repeating it teaches people the digest is noise."""
    sent = []

    async def capture(ts, pref, batch):
        sent.append(batch)

    tid = await make_tenant(slug="dg6", name="DG6")
    async with tenant_session(tid) as ts:
        await _pref(ts, mode="immediate")
        await _alert(ts)
        await run_digest_sweep(ts, send=capture)

    assert sent == []


async def test_a_resolved_alert_is_not_repeated():
    """An alert the rep already dealt with must not reappear in tomorrow's summary."""
    tid = await make_tenant(slug="dg7", name="DG7")
    async with tenant_session(tid) as ts:
        pref = await _pref(ts)
        await _alert(ts, status="resolved")
        batch = await collect_digest(ts, pref)

    assert batch.count == 0


async def test_a_category_preference_only_collects_its_own_category():
    tid = await make_tenant(slug="dg8", name="DG8")
    async with tenant_session(tid) as ts:
        pref = await _pref(ts, category="funding")
        await _alert(ts, category="funding")
        await _alert(ts, category="hiring", title="Acme hiring 20 engineers")
        batch = await collect_digest(ts, pref)

    assert batch.count == 1
    assert batch.categories == {"funding": 1}


async def test_a_first_ever_digest_does_not_replay_the_whole_history():
    """Enabling digests on a six-month-old workspace must not send six months of alerts."""
    tid = await make_tenant(slug="dg9", name="DG9")
    async with tenant_session(tid) as ts:
        pref = await _pref(ts)
        old = await _alert(ts, title="Ancient news")
        old.created_at = _now() - timedelta(days=30)
        await ts.flush()
        batch = await collect_digest(ts, pref)

    assert batch.count == 0, "bounded to the first-run window"


async def test_the_interval_gates_a_second_run():
    """The heartbeat ticks far more often than a digest is due."""
    sent = []

    async def capture(ts, pref, batch):
        sent.append(batch)

    tid = await make_tenant(slug="dg10", name="DG10")
    async with tenant_session(tid) as ts:
        await _pref(ts, last=_now() - timedelta(minutes=5))
        await _alert(ts)
        result = await run_digest_sweep(ts, send=capture)

    assert result["considered"] == 0
    assert sent == []


def test_the_summary_leads_with_the_number():
    """It is what decides whether the thing is opened."""
    from nexus.alerts.digest import DigestBatch

    batch = DigestBatch(user_id="u1", alert_ids=["a", "b", "c"],
                        categories={"funding": 2, "hiring": 1})
    summary = batch.summary()
    assert summary.startswith("3 alerts:")
    assert "funding" in summary and "hiring" in summary


def test_an_empty_batch_has_no_summary():
    from nexus.alerts.digest import DigestBatch

    assert DigestBatch(user_id="u1").summary() == ""
