# tests/test_send_approval.py
"""An approval is for the email that was shown, and for no other.

Sending a one-off email was one call, which is defensible while only a person can reach it and
stops being defensible once an assistant can: "send this" then means whatever the model decided it
should say. So a send is split into a preview that renders exactly what would leave and returns a
short-lived token, and a send that presents it.

The property these tests exist for is the binding: the token carries a digest of the recipient, the
subject and the body, so an email changed after it was approved is REFUSED. Without that, a model
could preview a polite note and send something else to the same address, and the approval record
would say a person agreed to it.

What none of this proves is that a human read the preview — nothing reaching the process can. These
tests pin what is actually true: the words that were approved are the words that go.
"""
from __future__ import annotations

import pytest

from nexus.engagement import approval

TENANT = "t-1"
USER = "u-1"
CONTACT = "c-1"
TO = "jane@acme.io"
SUBJECT = "quick question"
BODY = "Worth a look?"


def _token(**over) -> str:
    args = {"tenant_id": TENANT, "user_id": USER, "contact_id": CONTACT, "to_addr": TO,
            "subject": SUBJECT, "body": BODY, "surface": approval.SURFACE_MCP}
    args.update(over)
    return approval.issue(**args)


def _verify(token: str, **over):
    args = {"tenant_id": TENANT, "user_id": USER, "contact_id": CONTACT, "to_addr": TO,
            "subject": SUBJECT, "body": BODY}
    args.update(over)
    return approval.verify(token, **args)


def test_the_email_that_was_approved_is_the_email_that_may_be_sent():
    got = _verify(_token())
    assert got.user_id == USER
    assert got.contact_id == CONTACT
    assert got.surface == approval.SURFACE_MCP


def test_changing_the_body_after_approval_refuses_the_send():
    """The reason the token is bound to content rather than to the request."""
    token = _token()
    with pytest.raises(approval.ApprovalError) as exc:
        _verify(token, body="Wire me $5,000.")
    assert "changed since it was approved" in str(exc.value)


def test_changing_the_subject_after_approval_refuses_the_send():
    token = _token()
    with pytest.raises(approval.ApprovalError):
        _verify(token, subject="URGENT: invoice overdue")


def test_changing_the_recipient_after_approval_refuses_the_send():
    """Approving an email to one buyer must not send it to another."""
    token = _token()
    with pytest.raises(approval.ApprovalError):
        _verify(token, to_addr="someone.else@acme.io")


def test_a_subject_differing_only_in_case_is_a_different_subject():
    """"Re: pricing" and "RE: PRICING" read differently to the person receiving them."""
    token = _token()
    with pytest.raises(approval.ApprovalError):
        _verify(token, subject=SUBJECT.upper())


def test_the_address_is_matched_without_regard_to_case():
    """An address is case-insensitive, so a mail client's capitalisation is not a changed email."""
    assert _verify(_token(), to_addr=TO.upper()) is not None


def test_trailing_whitespace_is_not_a_changed_email():
    """A text box adds it and a mail client strips it; refusing on it would be noise."""
    assert _verify(_token(), body=BODY + "\n\n  ") is not None


def test_one_persons_approval_is_not_anothers():
    token = _token()
    with pytest.raises(approval.ApprovalError) as exc:
        _verify(token, user_id="u-2")
    assert "somebody else" in str(exc.value)


def test_an_approval_does_not_cross_workspaces():
    token = _token()
    with pytest.raises(approval.ApprovalError) as exc:
        _verify(token, tenant_id="t-2")
    assert "another workspace" in str(exc.value)


def test_an_approval_for_one_contact_is_not_an_approval_for_another():
    token = _token()
    with pytest.raises(approval.ApprovalError):
        _verify(token, contact_id="c-2")


def test_no_token_says_what_to_do_rather_than_failing_blankly():
    """The message is read by a model deciding what to do next, so it names the next step."""
    with pytest.raises(approval.ApprovalError) as exc:
        _verify("")
    assert "preview" in str(exc.value).lower()


def test_a_stale_approval_is_refused():
    """An approval is for the email in front of you now, not a standing permission."""
    from jose import jwt

    from nexus.core.config import get_settings
    from nexus.core.db import utcnow

    s = get_settings()
    stale = jwt.encode(
        {"typ": approval.TOKEN_TYP, "tid": TENANT, "sub": USER, "cid": CONTACT,
         "dig": approval.content_digest(to_addr=TO, subject=SUBJECT, body=BODY),
         "srf": approval.SURFACE_APP,
         "iat": int((utcnow().timestamp())) - 7200,
         "exp": int((utcnow().timestamp())) - 3600},
        s.secret_key, algorithm=s.jwt_algorithm)
    with pytest.raises(approval.ApprovalError) as exc:
        _verify(stale)
    assert "expired" in str(exc.value).lower()


def test_a_token_this_server_did_not_sign_is_refused():
    from jose import jwt

    forged = jwt.encode(
        {"typ": approval.TOKEN_TYP, "tid": TENANT, "sub": USER, "cid": CONTACT,
         "dig": approval.content_digest(to_addr=TO, subject=SUBJECT, body=BODY),
         "srf": approval.SURFACE_MCP, "exp": 9999999999},
        "not-our-secret", algorithm="HS256")
    with pytest.raises(approval.ApprovalError):
        _verify(forged)


def test_an_access_token_is_not_an_approval():
    """A bearer the caller already holds must not double as permission to send.

    Signed with OUR secret, so it passes the signature check and is refused on `typ` alone — which
    is the check being pinned. Without it, any token this server issues would authorise a send.
    """
    from nexus.core.security import create_access_token

    with pytest.raises(approval.ApprovalError) as exc:
        _verify(create_access_token(user_id=USER, tenant_id=TENANT, role="admin"))
    assert "not an approval" in str(exc.value)
