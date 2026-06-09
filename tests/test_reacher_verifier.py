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
