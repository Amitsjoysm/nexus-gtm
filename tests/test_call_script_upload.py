"""A campaign's own call script: uploaded, read out as written, and enhanced on request (D10).

The team calls from scripts they wrote; the console only ever showed an AI script made from
scratch. Decided with the owner: shown verbatim with an AI side panel, plus an option to enhance
it, as a suggestion the rep accepts or rejects with the original kept.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from nexus.calling.scripts import UnreadableScript, fill_placeholders, read_document

SCRIPT = ("Hi {{first_name}}, this is {{my_name}} calling from Acme.\n\n"
          "I saw {{trigger}} and wanted to ask how {{company}} handles field scheduling today.")


def _docx(paragraphs, *, extra=b""):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (b'<?xml version="1.0" encoding="UTF-8"?>' + extra +
           b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           b"<w:body>" + body.encode() + b"</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_a_text_file_is_kept_as_written():
    assert read_document("script.txt", SCRIPT.encode()) == SCRIPT
    assert read_document("script.txt", "Café opener".encode("cp1252")) == "Café opener"


def test_a_word_document_reads_paragraph_by_paragraph():
    text = read_document("Script.DOCX", _docx(["Opener: Hi {{first_name}}", "",
                                               "Ask: how do you schedule crews?"]))
    assert text == "Opener: Hi {{first_name}}\n\nAsk: how do you schedule crews?"


@pytest.mark.parametrize("name, data, why", [
    ("script.pdf", b"%PDF-1.4", "PDF is not supported yet"),
    ("script.doc", b"\xd0\xcf\x11\xe0", r"Upload the script as Word \(\.docx\) or text"),
    ("script.docx", b"not a zip", "not a Word document"),
    ("script.txt", b"", "empty"),
])
def test_what_cannot_be_read_says_why(name, data, why):
    with pytest.raises(UnreadableScript, match=why):
        read_document(name, data)


def test_a_document_type_declaration_is_refused_before_parsing():
    bomb = b'<!DOCTYPE lol [<!ENTITY a "aaaa">]>'
    with pytest.raises(UnreadableScript, match="safely"):
        read_document("script.docx", _docx(["x"], extra=bomb))


def test_only_marked_placeholders_are_filled_and_a_gap_stays_visible():
    filled = fill_placeholders(SCRIPT, {"first_name": "Ana", "company": "Acme Ops",
                                        "my_name": "Sam", "trigger": ""})
    assert filled.startswith("Hi Ana, this is Sam calling from Acme.")
    assert "how Acme Ops handles" in filled and "{{trigger}}" in filled
    assert fill_placeholders("{{password}} {{ company }}", {"password": "x", "company": "A"}) == \
        "{{password}} A"


# ---- through the API ------------------------------------------------------------------------------

async def _campaign(client, slug):
    from nexus.core.security import decode_access_token
    from nexus.engagement.sequences.service import create_campaign
    from tests.conftest import auth, signup, tenant_session
    from tests.test_engagement_sequences import STEPS

    token = await signup(client, slug=slug, email=f"o@{slug}.io", company=slug.title())
    tid, uid = decode_access_token(token)["tid"], decode_access_token(token)["sub"]
    from nexus.models.engagement import MailboxConnection

    async with tenant_session(tid) as ts:
        box = MailboxConnection(owner_user_id=uid, provider="google", email=f"o@{slug}.io",
                                status="connected", timezone="UTC")
        ts.add(box)
        await ts.flush()
        campaign = await create_campaign(ts, name="Q4", owner_user_id=uid, mailbox_id=box.id,
                                         steps=STEPS)
        return auth(token), tid, campaign.id


@pytest.fixture
def engine_on(monkeypatch):
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "engagement_campaigns_enabled", True)


async def test_upload_read_and_remove(client, engine_on):
    h, _tid, cid = await _campaign(client, "cs1")
    r = await client.post(f"/api/engagement/campaigns/{cid}/call-script/file", headers=h,
                         files={"file": ("opener.txt", SCRIPT.encode(), "text/plain")})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == SCRIPT and r.json()["filename"] == "opener.txt"

    assert (await client.get(f"/api/engagement/campaigns/{cid}/call-script",
                             headers=h)).json()["text"] == SCRIPT
    bad = await client.post(f"/api/engagement/campaigns/{cid}/call-script/file", headers=h,
                           files={"file": ("s.pdf", b"%PDF-1.4", "application/pdf")})
    assert bad.status_code == 422 and "PDF" in bad.json()["detail"]

    assert (await client.delete(f"/api/engagement/campaigns/{cid}/call-script",
                                headers=h)).status_code == 204
    assert (await client.get(f"/api/engagement/campaigns/{cid}/call-script",
                             headers=h)).json()["text"] is None


async def test_enhance_is_a_suggestion_the_original_is_kept(client, engine_on, monkeypatch):
    from nexus.agents import llm

    class _Model(llm.LLMProvider):
        async def complete(self, messages, **kw):
            assert kw.get("purpose") == "call_script"
            return llm.LLMResponse(text="Hi {{first_name}}, Sam here from Acme. Quick one.",
                                   tokens=5, provider="Test")

    monkeypatch.setattr(llm, "get_llm_provider", lambda: _Model())
    h, _tid, cid = await _campaign(client, "cs2")
    await client.post(f"/api/engagement/campaigns/{cid}/call-script/file", headers=h,
                     files={"file": ("opener.txt", SCRIPT.encode(), "text/plain")})

    s = await client.post(f"/api/engagement/campaigns/{cid}/call-script/enhance", headers=h)
    assert s.status_code == 200, s.text
    assert s.json()["text"] == SCRIPT, "nothing changes until the rep accepts"
    assert s.json()["suggestion"].startswith("Hi {{first_name}}, Sam here")

    a = await client.post(f"/api/engagement/campaigns/{cid}/call-script/suggestion/accept",
                          headers=h)
    assert a.json()["text"].startswith("Hi {{first_name}}, Sam here")
    assert a.json()["original"] == SCRIPT and a.json()["suggestion"] is None


async def test_the_call_console_shows_the_campaigns_script_filled_in(client, engine_on):
    from nexus.models.account import Account, Contact
    from nexus.models.calling import CallTask
    from nexus.models.engagement import EngagementEnrollment
    from tests.conftest import tenant_session

    h, tid, cid = await _campaign(client, "cs3")
    await client.post(f"/api/engagement/campaigns/{cid}/call-script/file", headers=h,
                     files={"file": ("opener.txt", SCRIPT.encode(), "text/plain")})
    async with tenant_session(tid) as ts:
        account = Account(tenant_id=tid, name="Acme Ops", domain="acme-ops.io")
        ts.add(account)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=account.id, full_name="Ana Example",
                          email="ana@acme-ops.io", phone="+15550001111")
        ts.add(contact)
        await ts.flush()
        enrollment = EngagementEnrollment(campaign_id=cid, contact_id=contact.id,
                                          account_id=account.id, status="active",
                                          current_step_index=0)
        ts.add(enrollment)
        await ts.flush()
        task = CallTask(tenant_id=tid, account_id=account.id, contact_id=contact.id,
                        engagement_enrollment_id=enrollment.id)
        ts.add(task)
        await ts.flush()
        task_id = task.id

    r = await client.get(f"/api/calling/tasks/{task_id}/campaign-script", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["text"].startswith("Hi Ana, this is ")
    assert "how Acme Ops handles" in r.json()["text"] and r.json()["campaign_name"] == "Q4"


# ---- the SDR's own script, from the call console (2026-11-04) -------------------------------------

async def _call(tid):
    from nexus.models.account import Account, Contact
    from nexus.models.calling import CallTask
    from tests.conftest import tenant_session

    async with tenant_session(tid) as ts:
        account = Account(tenant_id=tid, name="Acme Ops", domain="acme-ops.io")
        ts.add(account)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=account.id, full_name="Ana Example",
                          email="ana@acme-ops.io", phone="+15550001111")
        ts.add(contact)
        await ts.flush()
        task = CallTask(tenant_id=tid, account_id=account.id, contact_id=contact.id)
        ts.add(task)
        await ts.flush()
        return task.id


async def test_an_sdr_uploads_their_own_script_and_sees_it_on_every_call(client):
    from nexus.core.security import decode_access_token
    from tests.conftest import auth, signup

    token = await signup(client, slug="mys1", email="o@mys1.io", company="Mys1")
    h, tid = auth(token), decode_access_token(token)["tid"]
    assert (await client.get("/api/calling/my-script", headers=h)).status_code == 404

    r = await client.post("/api/calling/my-script/file", headers=h,
                          files={"file": ("mine.txt", SCRIPT.encode(), "text/plain")})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == SCRIPT and r.json()["filename"] == "mine.txt"
    bad = await client.post("/api/calling/my-script/file", headers=h,
                            files={"file": ("s.pdf", b"%PDF-1.4", "application/pdf")})
    assert bad.status_code == 422 and "PDF" in bad.json()["detail"]
    assert (await client.get("/api/calling/my-script", headers=h)).json()["text"] == SCRIPT

    # A call outside any campaign: their script, filled for this person.
    task_id = await _call(tid)
    call = await client.get(f"/api/calling/tasks/{task_id}/my-script", headers=h)
    assert call.status_code == 200, call.text
    assert call.json()["text"].startswith("Hi Ana, this is")
    assert "how Acme Ops handles" in call.json()["text"]

    # Replacing it keeps one row; removing it brings the console back to what it showed before.
    again = await client.post("/api/calling/my-script/file", headers=h,
                              files={"file": ("v2.txt", b"Hi {{first_name}}.", "text/plain")})
    assert again.json()["filename"] == "v2.txt"
    assert (await client.delete("/api/calling/my-script", headers=h)).status_code == 204
    assert (await client.get(f"/api/calling/tasks/{task_id}/my-script",
                             headers=h)).status_code == 404


async def test_one_sdrs_script_is_never_shown_to_another(client):
    from nexus.core.security import decode_access_token
    from tests.conftest import auth, signup

    a = await signup(client, slug="mys2a", email="o@mys2a.io", company="Mys2a")
    b = await signup(client, slug="mys2b", email="o@mys2b.io", company="Mys2b")
    await client.post("/api/calling/my-script/file", headers=auth(a),
                      files={"file": ("mine.txt", SCRIPT.encode(), "text/plain")})
    assert (await client.get("/api/calling/my-script", headers=auth(b))).status_code == 404
    other_task = await _call(decode_access_token(a)["tid"])
    assert (await client.get(f"/api/calling/tasks/{other_task}/my-script",
                             headers=auth(b))).status_code == 404
