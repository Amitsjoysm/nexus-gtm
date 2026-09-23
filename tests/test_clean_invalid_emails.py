"""The cleanup for addresses saved before the policy existed.

A maintenance script that writes by default is one keystroke from a mistake nobody can undo, so this
one is a dry run until asked. It must also leave the customer's OWN data alone: an imported address
that has never been checked is not our guess to delete.
"""
from __future__ import annotations

from scripts.clean_invalid_emails import sweep
from nexus.models.account import Account, Contact
from nexus.verification import STATUS_INVALID, STATUS_UNKNOWN, STATUS_VALID
from tests.conftest import make_tenant, tenant_session


async def _seed(ts, tid):
    acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
    ts.add(acc)
    await ts.flush()
    people = {
        "dead": Contact(tenant_id=tid, account_id=acc.id, full_name="Dead One",
                        email="dead@acme.com", email_status=STATUS_INVALID,
                        enrichment_source="pattern_verified"),
        "guess": Contact(tenant_id=tid, account_id=acc.id, full_name="Guess One",
                         email="guess@acme.com", email_status=STATUS_UNKNOWN,
                         enrichment_source="pattern"),
        "good": Contact(tenant_id=tid, account_id=acc.id, full_name="Good One",
                        email="good@acme.com", email_status=STATUS_VALID,
                        enrichment_source="pattern_verified"),
        "imported": Contact(tenant_id=tid, account_id=acc.id, full_name="Imported One",
                            email="imported@acme.com", email_status=None,
                            enrichment_source="csv_import"),
    }
    for c in people.values():
        ts.add(c)
    await ts.flush()
    return people


async def test_a_dry_run_reports_and_writes_nothing():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        people = await _seed(ts, tid)

        result = await sweep(session=ts.session)

        assert result["invalid"] == 1
        assert result["unverified_guess"] == 1
        assert people["dead"].email == "dead@acme.com", "a dry run wrote to the database"
        assert people["guess"].email == "guess@acme.com"


async def test_apply_removes_the_right_addresses_and_keeps_the_rest():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        people = await _seed(ts, tid)

        await sweep(apply=True, session=ts.session)

        assert people["dead"].email is None
        assert "dead@acme.com" in people["dead"].custom_fields["rejected_emails"]
        # Nothing proved the guess wrong, so a later search may legitimately find it again.
        assert people["guess"].email is None
        assert not (people["guess"].custom_fields or {}).get("rejected_emails")
        assert people["good"].email == "good@acme.com"
        assert people["imported"].email == "imported@acme.com", "the customer's own data was deleted"
