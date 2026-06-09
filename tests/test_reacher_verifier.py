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
import pytest

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
    v = _verifier(lambda req: _resp(RISKY_CATCHALL_O365))
    out = await v.verify_one("guess@acme.com")
    assert out.status == "risky"
    assert out.confidence == 0.40
    assert out.provider_type == "office365"
    assert out.signals["is_catch_all"] is True
    assert out.signals["is_role_account"] is True


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


def test_contact_sourcing_settings_defaults():
    from nexus.core.config import Settings

    s = Settings()
    assert s.email_verify_provider == "stub"
    assert s.email_verify_url == "http://158.69.113.127:8080/v0/check_email"
    assert s.email_verify_timeout_s == 20.0
    assert s.email_finder_max_candidates == 5
    assert s.contact_search_sources == "stub"
    assert s.campaign_sourcing_enabled is True
    assert s.campaign_sourced_min_send_confidence == 0.5
    assert s.contact_search_source_list == ["stub"]
