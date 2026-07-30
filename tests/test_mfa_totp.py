# tests/test_mfa_totp.py
"""The MFA primitives, pinned to the published RFC vectors.

A hand-rolled OTP that is subtly wrong fails in the worst possible way: it looks like it works
(you can always verify a code you just generated) and only breaks against a real authenticator
app, in production, for a user who is now locked out. So the arithmetic is checked against RFC
4226 Appendix D and RFC 6238 Appendix B rather than against itself.
"""
from __future__ import annotations

import base64

from nexus.auth.mfa import (
    counter_at,
    generate_recovery_codes,
    generate_secret,
    hash_recovery_code,
    hotp_code,
    normalize_recovery_code,
    provisioning_uri,
    seal_secret,
    totp_code,
    unseal_secret,
    verify_recovery_code,
    verify_totp,
)

# The shared RFC secret: the ASCII string "12345678901234567890".
_RFC_SECRET_BYTES = b"12345678901234567890"
_RFC_SECRET_B32 = base64.b32encode(_RFC_SECRET_BYTES).decode().rstrip("=")


def test_hotp_matches_rfc4226_appendix_d():
    expected = [
        "755224", "287082", "359152", "969429", "338314",
        "254676", "287922", "162583", "399871", "520489",
    ]
    got = [hotp_code(_RFC_SECRET_BYTES, c) for c in range(10)]
    assert got == expected


def test_totp_matches_rfc6238_appendix_b_sha1():
    """The published 8-digit SHA1 vectors, and the 6-digit truncation we actually ship."""
    vectors = {
        59: "94287082",
        1111111109: "07081804",
        1111111111: "14050471",
        1234567890: "89005924",
        2000000000: "69279037",
        20000000000: "65353130",
    }
    for ts, expected8 in vectors.items():
        assert totp_code(_RFC_SECRET_B32, timestamp=ts, digits=8) == expected8
        # Our default is 6 digits, which is the same truncation modulo 10^6.
        assert totp_code(_RFC_SECRET_B32, timestamp=ts, digits=6) == expected8[-6:]


def test_counter_is_floor_of_time_over_step():
    assert counter_at(59, 30) == 1
    assert counter_at(60, 30) == 2
    assert counter_at(1111111109, 30) == 0x23523EC


def test_verify_accepts_the_current_code_and_reports_its_counter():
    ts = 1_700_000_000
    code = totp_code(_RFC_SECRET_B32, timestamp=ts)
    assert verify_totp(_RFC_SECRET_B32, code, timestamp=ts) == counter_at(ts, 30)


def test_verify_accepts_one_step_of_drift_either_side():
    """A phone whose clock is 30s off must still get in — that is the whole point of drift."""
    ts = 1_700_000_000
    previous = totp_code(_RFC_SECRET_B32, timestamp=ts - 30)
    upcoming = totp_code(_RFC_SECRET_B32, timestamp=ts + 30)
    assert verify_totp(_RFC_SECRET_B32, previous, timestamp=ts) == counter_at(ts - 30, 30)
    assert verify_totp(_RFC_SECRET_B32, upcoming, timestamp=ts) == counter_at(ts + 30, 30)


def test_verify_refuses_beyond_the_drift_window():
    ts = 1_700_000_000
    stale = totp_code(_RFC_SECRET_B32, timestamp=ts - 120)
    assert verify_totp(_RFC_SECRET_B32, stale, timestamp=ts) is None


def test_replay_is_refused_even_inside_the_valid_window():
    """The security property that a naive implementation misses.

    The code is still arithmetically correct and still inside its 30-second step, but the counter
    has already been spent — so a code lifted off a screen or a phishing proxy is dead on arrival.
    """
    ts = 1_700_000_000
    code = totp_code(_RFC_SECRET_B32, timestamp=ts)
    first = verify_totp(_RFC_SECRET_B32, code, timestamp=ts, last_counter=None)
    assert first is not None
    # Same code, same window, but the counter is now recorded as spent.
    assert verify_totp(_RFC_SECRET_B32, code, timestamp=ts, last_counter=first) is None
    # And an *older* drift-window code cannot be walked backwards either.
    older = totp_code(_RFC_SECRET_B32, timestamp=ts - 30)
    assert verify_totp(_RFC_SECRET_B32, older, timestamp=ts, last_counter=first) is None


def test_replay_guard_still_admits_the_next_step():
    ts = 1_700_000_000
    spent = verify_totp(_RFC_SECRET_B32, totp_code(_RFC_SECRET_B32, timestamp=ts), timestamp=ts)
    later = ts + 30
    nxt = totp_code(_RFC_SECRET_B32, timestamp=later)
    assert verify_totp(_RFC_SECRET_B32, nxt, timestamp=later, last_counter=spent) == spent + 1


def test_verify_rejects_malformed_input_without_raising():
    ts = 1_700_000_000
    for bad in ["", "   ", "abcdef", "12345", "1234567", None]:
        assert verify_totp(_RFC_SECRET_B32, bad, timestamp=ts) is None  # type: ignore[arg-type]
    # A corrupt/empty seed fails closed rather than exploding on the login path.
    assert verify_totp("", "123456", timestamp=ts) is None
    assert verify_totp("not!base32!", "123456", timestamp=ts) is None


def test_email_method_is_the_same_primitive_on_a_longer_step():
    """A mailed code must survive the minutes it takes to read an email."""
    ts = 1_700_000_000
    code = totp_code(_RFC_SECRET_B32, timestamp=ts, step=300)
    # Still valid four minutes later within the same step...
    assert verify_totp(_RFC_SECRET_B32, code, timestamp=ts + 240, step=300) is not None
    # ...and dead well beyond the drift window.
    assert verify_totp(_RFC_SECRET_B32, code, timestamp=ts + 1200, step=300) is None


def test_generated_secrets_are_random_base32_and_usable():
    secrets_seen = {generate_secret() for _ in range(20)}
    assert len(secrets_seen) == 20  # 160 bits: collisions do not happen
    s = generate_secret()
    assert "=" not in s  # unpadded, so it survives a QR/URI round trip
    ts = 1_700_000_000
    assert verify_totp(s, totp_code(s, timestamp=ts), timestamp=ts) is not None


def test_provisioning_uri_is_a_scannable_key_uri():
    uri = provisioning_uri(_RFC_SECRET_B32, account_name="rep@acme.com", issuer="InfoJoy GTM")
    assert uri.startswith("otpauth://totp/InfoJoy%20GTM:rep%40acme.com?")
    assert f"secret={_RFC_SECRET_B32}" in uri
    assert "algorithm=SHA1" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


def test_secret_is_sealed_not_stored_in_the_clear():
    secret = generate_secret()
    sealed = seal_secret(secret)
    assert sealed != secret
    assert secret not in sealed
    assert unseal_secret(sealed) == secret
    # Tampered or missing ciphertext degrades to "unusable factor", never to a crash.
    assert unseal_secret("") == ""
    assert unseal_secret("not-a-fernet-token") == ""


def test_recovery_codes_are_unique_readable_and_hashed_one_way():
    codes = generate_recovery_codes(10)
    assert len(codes) == 10
    assert len(set(codes)) == 10
    for c in codes:
        assert len(c) == 11 and c[5] == "-"
        # No visually ambiguous characters — these get typed by hand.
        assert not (set(c) & set("IO01"))
        digest = hash_recovery_code(c)
        assert c not in digest and len(digest) == 64
        assert verify_recovery_code(c, digest)


def test_recovery_code_matching_ignores_formatting_but_not_content():
    code = generate_recovery_codes(1)[0]
    digest = hash_recovery_code(code)
    assert verify_recovery_code(code.lower(), digest)
    assert verify_recovery_code(code.replace("-", " "), digest)
    assert verify_recovery_code(code.replace("-", ""), digest)
    assert normalize_recovery_code(code.lower()) == code.replace("-", "")
    assert not verify_recovery_code(generate_recovery_codes(1)[0], digest)
    assert not verify_recovery_code("", digest)
    assert not verify_recovery_code(code, "")
