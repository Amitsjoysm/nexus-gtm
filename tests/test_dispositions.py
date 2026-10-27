"""The team's dispositions: Category then Disposition, and the safe actions (2026-10-26, D11).

Supplied by the SDR team, one list for calling and email reps, "best UX with easy drop downs for
various categories". Every outcome is recorded; a few take a safe action and none deletes anything.
The eight dispositions that existed before stay valid for the rows that hold them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nexus.calling import dispositions as dispo
from tests.conftest import auth, signup, tenant_session
from tests.test_calling_api import _seed


def test_the_teams_list_exactly():
    labels = {key: [d.label for d in dispo.DISPOSITIONS if d.category == key]
              for key, _ in dispo.CATEGORIES}
    assert labels["positive"] == ["LEAD", "Interested - Send Information (MQL)",
                                  "Reference Shared"]
    assert labels["qualification"] == [
        "Existing Customer", "Decision at Another Location", "Using Competitor - Not Switching",
        "In-House Solution", "Outside ICP", "Duplicate Lead", "Already Engaged with Colleague",
        "Supplier Registration Required"]
    assert len(dispo.DISPOSITIONS) == 28 and len(dispo.CATEGORIES) == 10
    assert all(len(d.code) <= 24 for d in dispo.DISPOSITIONS)   # call_activities.disposition


def test_each_channel_sees_only_its_own_values():
    def codes(channel):
        return {d["code"] for c in dispo.for_channel(channel) for d in c["dispositions"]}

    assert "bounce" not in codes("call") and "bounce" in codes("email")
    assert {"busy_signal", "gatekeeper", "no_answer"} <= codes("call")
    assert not {"busy_signal", "gatekeeper", "no_answer"} & codes("email")


def test_every_reply_category_reads_as_a_disposition():
    from nexus.engagement.replies.classify import CATEGORIES

    assert set(CATEGORIES) <= set(dispo.REPLY_TO_DISPOSITION)
    assert all(code in dispo.BY_CODE for code in dispo.REPLY_TO_DISPOSITION.values())


# ---- through the API ------------------------------------------------------------------------------

async def _task(client, slug):
    token, acc_id, c_id = await _seed(client, slug)
    h = auth(token)
    task_id = (await client.post("/api/calling/tasks", headers=h,
                                 json={"account_id": acc_id, "contact_id": c_id})).json()["id"]
    from nexus.core.security import decode_access_token

    return h, decode_access_token(token)["tid"], acc_id, c_id, task_id


async def _log(client, h, task_id, **body):
    r = await client.post(f"/api/calling/tasks/{task_id}/disposition", headers=h, json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def test_the_dropdowns_come_from_the_server(client):
    token = await signup(client, slug="dsp0", email="o@dsp0.io", company="Dsp0")
    r = await client.get("/api/calling/dispositions", headers=auth(token))

    assert r.status_code == 200
    first = r.json()[0]
    assert first["label"] == "Positive Outcome" and first["dispositions"][0]["label"] == "LEAD"


async def test_dnc_puts_the_person_on_do_not_contact(client):
    from nexus.engagement.suppression.service import active_block
    from nexus.models.account import Contact

    h, tid, _a, c_id, task_id = await _task(client, "dsp1")
    async with tenant_session(tid) as ts:
        contact = await ts.get(Contact, c_id)
        contact.email = "jane@acme.co"
        await ts.flush()

    await _log(client, h, task_id, disposition="dnc", notes="asked not to be called")

    async with tenant_session(tid) as ts:
        contact = await ts.get(Contact, c_id)
        assert contact.custom_fields.get("do_not_call") is True
        assert await active_block(ts, "jane@acme.co") is not None


async def test_a_bad_number_is_marked_not_deleted(client):
    from nexus.models.account import Contact

    h, tid, _a, c_id, task_id = await _task(client, "dsp2")
    await _log(client, h, task_id, disposition="number_disconnected")

    async with tenant_session(tid) as ts:
        contact = await ts.get(Contact, c_id)
        assert contact.phone == "+15551234567"
        assert contact.custom_fields["phone_status"] == "bad"


async def test_a_callback_stays_open_and_is_due_when_asked(client):
    h, _tid, _a, _c, task_id = await _task(client, "dsp3")
    when = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0)

    await _log(client, h, task_id, disposition="callback", follow_up_at=when.isoformat())

    queue = (await client.get("/api/calling/queue", headers=h)).json()
    assert [t["id"] for t in queue] == [task_id]
    assert queue[0]["due_at"].startswith(when.date().isoformat())


async def test_outside_icp_flags_the_account_and_deletes_nothing(client):
    from nexus.models.account import Account

    h, tid, acc_id, _c, task_id = await _task(client, "dsp4")
    await _log(client, h, task_id, disposition="outside_icp", notes="they make furniture")

    async with tenant_session(tid) as ts:
        account = await ts.get(Account, acc_id)
        flag = account.custom_fields["flags"][0]
        assert flag["code"] == "outside_icp" and flag["note"] == "they make furniture"
        assert account.archived_at is None


async def test_wrong_person_asks_for_the_right_contact(client):
    from nexus.models.workflow import InboxTask

    h, tid, acc_id, _c, task_id = await _task(client, "dsp5")
    await _log(client, h, task_id, disposition="wrong_person", notes="ask for Raj in ops")

    async with tenant_session(tid) as ts:
        tasks = await ts.list(InboxTask, InboxTask.account_id == acc_id)
    assert len(tasks) == 1 and "right contact" in tasks[0].title.lower()
    assert "ask for Raj in ops" in tasks[0].reason


@pytest.mark.parametrize("code", ["meeting_booked", "connected"])
async def test_the_old_codes_still_log(client, code):
    h, _tid, _a, _c, task_id = await _task(client, f"dsp-{code.replace('_', '-')}")
    assert (await _log(client, h, task_id, disposition=code))["disposition"] == code


async def test_bounce_is_not_a_call_outcome(client):
    h, _tid, _a, _c, task_id = await _task(client, "dsp6")
    r = await client.post(f"/api/calling/tasks/{task_id}/disposition", headers=h,
                          json={"disposition": "bounce"})
    assert r.status_code == 422


async def test_the_reply_desk_names_each_reply_as_a_disposition():
    from nexus.api.routers.engagement_desk import _row

    class _Names:
        def contact(self, _id):
            return None

        def account(self, _id):
            return None

    class _Reply:
        id = "r1"
        message_id = "m1"
        category = "later"
        corrected_category = None
        confidence = 0.9
        resolved_date = status = decision = assigned_user_id = None
        contact_id = account_id = responded_at = auto_reply = auto_reply_note = None

    row = _row(_Reply(), None, _Names())

    assert row["disposition"] == "callback"
    assert (row["disposition_category"], row["disposition_label"]) == ("Follow-Up", "Callback")


def _src(path):
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "frontend" / "src" / path).read_text("utf-8")


def test_the_console_picks_a_category_then_a_disposition():
    console = _src("components/CallConsole.tsx")
    assert 'label="Category"' in console and 'label="Disposition"' in console
    assert "api.callDispositions(" in console and "CALL_DISPOSITIONS" not in console
