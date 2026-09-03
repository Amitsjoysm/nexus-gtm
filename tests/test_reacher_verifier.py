"""Real email verifier (Reacher) + verdict model extensions. All offline."""
from __future__ import annotations

from nexus.verification import (
    STATUS_RISKY,
    STATUS_UNKNOWN,
    EmailVerification,
)


def test_status_risky_constant_value():
    assert STATUS_RISKY == "risky"


def test_email_verification_new_optional_fields_default_safely():
    v = EmailVerification(email="a@b.com")
    assert v.provider_type is None
    assert v.signals == {}
    # Existing behavior unchanged.
    assert v.status == STATUS_UNKNOWN
    assert v.confidence == 0.0


def test_as_dict_includes_new_fields():
    v = EmailVerification(
        email="a@b.com",
        status="valid",
        confidence=0.95,
        source="reacher",
        provider_type="gsuite",
        signals={"is_catch_all": False},
    )
    d = v.as_dict()
    assert d["provider_type"] == "gsuite"
    assert d["signals"] == {"is_catch_all": False}
    assert d["email"] == "a@b.com"
    assert d["status"] == "valid"


import httpx

from nexus.verification import STATUS_INVALID, STATUS_VALID
from nexus.verification.reacher import ReacherEmailVerifier


def _resp(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _verifier(handler) -> ReacherEmailVerifier:
    transport = httpx.MockTransport(handler)
    return ReacherEmailVerifier(
        url="http://verifier.test/v0/check_email", timeout=5.0, transport=transport
    )


SAFE_GSUITE = {
    "input": "jane@acme.com",
    "is_reachable": "safe",
    "misc": {"is_disposable": False, "is_role_account": False},
    "mx": {"accepts_mail": True, "records": ["aspmx.l.google.com."]},
    "smtp": {"is_catch_all": False, "has_full_inbox": False, "is_deliverable": True},
}

INVALID = {
    "input": "nope@acme.com",
    "is_reachable": "invalid",
    "misc": {"is_disposable": False, "is_role_account": False},
    "mx": {"accepts_mail": False, "records": []},
    "smtp": {"is_catch_all": False, "has_full_inbox": False, "is_deliverable": False},
}

RISKY_CATCHALL_O365 = {
    "input": "guess@acme.com",
    "is_reachable": "risky",
    "misc": {"is_disposable": False, "is_role_account": True},
    "mx": {"accepts_mail": True, "records": ["acme-com.mail.protection.outlook.com."]},
    "smtp": {"is_catch_all": True, "has_full_inbox": False, "is_deliverable": True},
}

UNKNOWN_CUSTOM = {
    "input": "x@acme.com",
    "is_reachable": "unknown",
    "misc": {"is_disposable": False, "is_role_account": False},
    "mx": {"accepts_mail": True, "records": ["mail.acme.com."]},
    "smtp": {"is_catch_all": False, "has_full_inbox": False, "is_deliverable": False},
}


async def test_safe_maps_to_valid_with_gsuite_provider_type():
    v = _verifier(lambda req: _resp(SAFE_GSUITE))
    out = await v.verify_one("jane@acme.com")
    assert out.status == STATUS_VALID
    assert out.confidence == 0.95
    assert out.provider_type == "gsuite"
    assert out.source == "reacher"
    assert out.signals["is_catch_all"] is False


async def test_invalid_maps_to_invalid_hard():
    v = _verifier(lambda req: _resp(INVALID))
    out = await v.verify_one("nope@acme.com")
    assert out.status == STATUS_INVALID
    assert out.confidence == 0.95


async def test_risky_catchall_office365_carries_signals():
    """`risky` is graded by REASON now, not a flat 0.40.

    Measured against the live Reacher instance on 2026-09-02: `safe` never appeared once across
    real B2B addresses, because catch-all domains and role accounts each force `risky` and
    prospecting addresses are overwhelmingly one or the other. A catch-all the server ACCEPTED and
    an address it did not both scored 0.40 with no reason attached, which is what made the whole
    feature read as broken.

    The status is deliberately unchanged — promoting a catch-all to `valid` would invent the
    certainty Reacher withheld. Only the confidence and the stated reason move.
    """
    v = _verifier(lambda req: _resp(RISKY_CATCHALL_O365))
    out = await v.verify_one("guess@acme.com")
    assert out.status == "risky"
    assert out.provider_type == "office365"
    assert out.signals["is_catch_all"] is True
    assert out.signals["is_role_account"] is True
    assert out.signals["risky_reason"] == "catch_all"
    assert out.confidence > 0.40, (
        "a catch-all the server accepted still scores like an unverifiable address"
    )


async def test_unknown_custom_low_confidence():
    v = _verifier(lambda req: _resp(UNKNOWN_CUSTOM))
    out = await v.verify_one("x@acme.com")
    assert out.status == "unknown"
    assert out.confidence == 0.20
    assert out.provider_type == "custom"


async def test_network_failure_fails_safe_to_unknown():
    def boom(req):
        raise httpx.ConnectError("down")

    v = _verifier(boom)
    out = await v.verify_one("x@acme.com")
    assert out.status == "unknown"
    assert out.confidence == 0.0
    assert out.source == "reacher"


async def test_non_200_fails_safe_to_unknown():
    v = _verifier(lambda req: httpx.Response(503, text="busy"))
    out = await v.verify_one("x@acme.com")
    assert out.status == "unknown"
    assert out.confidence == 0.0


async def test_posts_to_email_field():
    seen = {}

    def handler(req):
        import json
        seen.update(json.loads(req.content))
        return _resp(SAFE_GSUITE)

    v = _verifier(handler)
    await v.verify_one("jane@acme.com")
    assert seen == {"to_email": "jane@acme.com"}


async def test_sends_authorization_header_when_configured():
    """A public HTTPS verifier endpoint can require a token; we send it as Authorization."""
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        return _resp(SAFE_GSUITE)

    v = ReacherEmailVerifier(
        url="https://verify.test/v0/check_email", timeout=5.0,
        transport=httpx.MockTransport(handler), auth_header="Bearer s3cr3t",
    )
    await v.verify_one("jane@acme.com")
    assert seen["auth"] == "Bearer s3cr3t"


async def test_no_authorization_header_by_default():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        return _resp(SAFE_GSUITE)

    v = _verifier(handler)  # no auth_header
    await v.verify_one("jane@acme.com")
    assert seen["auth"] is None


def test_contact_sourcing_settings_defaults():
    from nexus.core.config import Settings

    s = Settings()
    assert s.email_verify_provider == "stub"
    assert s.email_verify_url == "http://158.69.113.127:8080/v0/check_email"
    assert s.email_verify_auth_header == ""  # no auth header unless configured
    assert s.email_verify_timeout_s == 20.0
    assert s.email_finder_max_candidates == 12
    assert s.contact_search_sources == "stub"
    assert s.campaign_sourcing_enabled is True
    assert s.campaign_sourced_min_send_confidence == 0.5
    assert s.contact_search_source_list == ["stub"]


async def test_circuit_breaker_opens_after_consecutive_failures():
    """A down verifier must fail fast: after the failure threshold the circuit opens and
    further calls return 'unknown' WITHOUT touching the network (no per-call timeout stack-up)."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("host down")

    v = ReacherEmailVerifier(
        url="http://down.example/v0/check_email", timeout=5.0,
        transport=httpx.MockTransport(handler), fail_threshold=2, cooldown_s=60.0,
    )
    r1 = await v.verify_one("a@x.com")   # failure 1 (hits transport)
    r2 = await v.verify_one("b@x.com")   # failure 2 -> opens circuit (hits transport)
    r3 = await v.verify_one("c@x.com")   # circuit open -> short-circuits, no transport
    assert r1.status == STATUS_UNKNOWN and r2.status == STATUS_UNKNOWN and r3.status == STATUS_UNKNOWN
    assert calls["n"] == 2  # the 3rd call never reached the network
