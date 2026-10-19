"""The campaign hold on Prospector emails (decision D19).

Prospector keeps the provider's email unverified, and the engagement engine had no gate on
verification, so without this a bulk campaign would send to every unchecked vendor address. A
`sourcing:prospector` contact is skipped at enrollment and marked in the candidate picker until a
verifier calls its address deliverable. Contacts from anywhere else are untouched.
"""
from __future__ import annotations

from tests.conftest import tenant_session
from tests.test_engagement_sequences import STEPS, _world


async def _mark(tid: str, contact_id: str, *, source: str | None, status: str | None) -> None:
    from nexus.models.account import Contact

    async with tenant_session(tid) as ts:
        contact = await ts.get(Contact, contact_id)
        contact.enrichment_source = source
        contact.email_status = status
        await ts.flush()


async def test_an_unverified_prospector_email_is_held_out_of_a_campaign():
    from nexus.engagement.sequences.service import create_campaign, enroll

    tid, user_id, mailbox_id, ids = await _world("hold1", contacts=3)
    await _mark(tid, ids[0], source="sourcing:prospector", status=None)
    await _mark(tid, ids[1], source="sourcing:prospector", status="unknown")
    await _mark(tid, ids[2], source="crm:hubspot", status=None)          # untouched

    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Cold", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        result = await enroll(ts, campaign, ids)

    assert result.added == [ids[2]]
    reasons = {s["contact_id"]: s["reason"] for s in result.skipped}
    assert reasons == {ids[0]: "unverified_prospector_email",
                       ids[1]: "unverified_prospector_email"}


async def test_a_verified_prospector_email_enrolls():
    from nexus.engagement.sequences.service import create_campaign, enroll

    tid, user_id, mailbox_id, ids = await _world("hold2", contacts=3)
    for contact_id, status in zip(ids, ("valid", "catch_all", "risky")):
        await _mark(tid, contact_id, source="sourcing:prospector", status=status)

    async with tenant_session(tid) as ts:
        campaign = await create_campaign(ts, name="Cold", owner_user_id=user_id,
                                         mailbox_id=mailbox_id, steps=STEPS)
        result = await enroll(ts, campaign, ids)

    assert sorted(result.added) == sorted(ids) and result.skipped == []


async def test_the_picker_marks_held_contacts():
    from nexus.engagement.sequences.candidates import candidates

    tid, _user, _mailbox, ids = await _world("hold3", contacts=2)
    await _mark(tid, ids[0], source="sourcing:prospector", status=None)
    async with tenant_session(tid) as ts:
        rows = {c.contact_id: c for c in await candidates(ts)}

    assert rows[ids[0]].held is True and rows[ids[1]].held is False


def test_the_rule_reads_only_the_source_and_the_verdict():
    from types import SimpleNamespace

    from nexus.prospector.hold import held_until_verified

    def contact(source, status):
        return SimpleNamespace(enrichment_source=source, email_status=status)

    assert held_until_verified(contact("sourcing:prospector", None))
    assert held_until_verified(contact("sourcing:prospector", "invalid"))
    assert not held_until_verified(contact("sourcing:prospector", "valid"))
    assert not held_until_verified(contact("sourcing:linkedin", None))
    assert not held_until_verified(contact(None, None))
