"""Re-verifying existing contact emails. Offline (injected fake verifier)."""
from __future__ import annotations

from nexus.enrichment.reverify import reverify_contact, reverify_contacts
from nexus.models.account import Account, Contact
from nexus.verification import (
    STATUS_INVALID,
    STATUS_UNKNOWN,
    STATUS_VALID,
    EmailVerification,
)
from tests.conftest import make_tenant, tenant_session


def _verify(verdicts: dict):
    async def verify(email: str) -> EmailVerification:
        status, conf = verdicts.get(email, (STATUS_UNKNOWN, 0.2))
        return EmailVerification(email=email, status=status, confidence=conf, source="fake")

    return verify


async def test_reverify_contact_sets_status_and_lifts_confidence_on_valid():
    c = Contact(tenant_id="t", account_id="a", full_name="Jane Doe",
                email="jane.doe@acme.com", email_confidence=0.4, email_status=None)
    changed = await reverify_contact(c, _verify({"jane.doe@acme.com": (STATUS_VALID, 0.95)}))
    assert changed is True
    assert c.email_status == STATUS_VALID
    assert c.email_confidence == 0.95  # lifted by the confirmed verdict


async def test_reverify_contact_risky_does_not_downgrade_found_confidence():
    """A catch-all 'risky' verdict must set the status without clobbering a high web-sourced
    confidence that the address is the right one."""
    c = Contact(tenant_id="t", account_id="a", full_name="Eric Glyman",
                email="eric@ramp.com", email_confidence=0.8, email_status=None)
    changed = await reverify_contact(c, _verify({"eric@ramp.com": ("risky", 0.4)}))
    assert changed is True
    assert c.email_status == "risky"
    assert c.email_confidence == 0.8  # unchanged


async def test_reverify_contact_no_email_is_noop():
    c = Contact(tenant_id="t", account_id="a", full_name="No Email", email=None)
    assert await reverify_contact(c, _verify({})) is False


async def test_reverify_contacts_only_unverified_skips_already_verified():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        # One unverified (null), one already valid (must be left alone).
        unv = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe",
                      email="jane.doe@acme.com", email_confidence=0.4, email_status=None)
        done = Contact(tenant_id=tid, account_id=acc.id, full_name="Al Ready",
                       email="al@acme.com", email_confidence=0.95, email_status=STATUS_VALID)
        ts.add_all([unv, done])
        await ts.flush()

        verify = _verify({
            "jane.doe@acme.com": (STATUS_VALID, 0.95),
            "al@acme.com": (STATUS_INVALID, 0.95),  # would flip 'done' IF it were checked
        })
        result = await reverify_contacts(ts, verify=verify, only_unverified=True)

        await ts.session.refresh(unv)
        await ts.session.refresh(done)

    assert result["checked"] == 1            # only the unverified one was scanned
    assert result["updated"] == 1
    assert unv.email_status == STATUS_VALID
    assert done.email_status == STATUS_VALID  # untouched (not invalid)


async def test_reverify_contacts_all_includes_verified():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        c = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe",
                    email="jane.doe@acme.com", email_confidence=0.95, email_status=STATUS_VALID)
        ts.add(c)
        await ts.flush()

        verify = _verify({"jane.doe@acme.com": (STATUS_INVALID, 0.95)})
        result = await reverify_contacts(ts, verify=verify, only_unverified=False)
        await ts.session.refresh(c)

    assert result["checked"] == 1
    assert c.email_status == STATUS_INVALID  # re-checked even though it had a verdict


async def test_reverify_stamps_checked_at_even_when_status_unchanged():
    """unknown -> unknown used to look like a no-op to the SDR; the check time is now recorded."""
    c = Contact(tenant_id="t", account_id="a", full_name="U", email="u@x.com",
                email_status=STATUS_UNKNOWN)
    changed = await reverify_contact(c, _verify({}))  # fake returns unknown again
    assert changed is False
    assert c.email_checked_at is not None


async def test_full_pass_skips_fresh_valid_addresses():
    """A confirmed-valid address checked within the 30-day cool-down is not re-probed on a full
    pass; stale-valid and unknown addresses still are."""
    from datetime import timedelta

    from nexus.core.db import utcnow

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="A", domain="a.co")
        ts.add(acc)
        await ts.flush()
        fresh = Contact(tenant_id=tid, account_id=acc.id, full_name="F", email="f@a.co",
                        email_status=STATUS_VALID, email_checked_at=utcnow())
        stale = Contact(tenant_id=tid, account_id=acc.id, full_name="S", email="s@a.co",
                        email_status=STATUS_VALID,
                        email_checked_at=utcnow() - timedelta(days=45))
        unk = Contact(tenant_id=tid, account_id=acc.id, full_name="U", email="u@a.co",
                      email_status=STATUS_UNKNOWN)
        ts.add_all([fresh, stale, unk])
        await ts.flush()

        seen: list[str] = []

        async def verify(email: str) -> EmailVerification:
            seen.append(email)
            return EmailVerification(email=email, status=STATUS_VALID, confidence=0.9,
                                     source="fake")

        res = await reverify_contacts(ts, verify=verify, only_unverified=False)
        assert "f@a.co" not in seen                 # fresh valid skipped (cool-down)
        assert {"s@a.co", "u@a.co"} <= set(seen)    # stale valid + unknown re-checked
        assert res["checked"] == 2
