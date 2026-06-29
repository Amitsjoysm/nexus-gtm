"""One-time-passcode primitives for two-step registration.

Design choices (these are the security-relevant ones):
  * **Generation** uses ``secrets`` (CSPRNG), never ``random`` — codes must be unguessable.
  * **At rest** the OTP is stored only as a keyed **HMAC-SHA256 hash**, never the plaintext and
    never reversibly encrypted: a database leak then exposes no live codes.
  * **Verification** uses ``hmac.compare_digest`` (constant-time) to avoid timing side-channels.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

_DIGITS = "0123456789"


def generate_otp(length: int = 6) -> str:
    """A cryptographically-random numeric code of the given length."""
    n = max(4, int(length))
    return "".join(secrets.choice(_DIGITS) for _ in range(n))


def hash_otp(code: str, secret: str) -> str:
    """Keyed HMAC-SHA256 hex digest of a code — what we persist (one-way)."""
    return hmac.new(secret.encode("utf-8"), (code or "").encode("utf-8"), hashlib.sha256).hexdigest()


def verify_otp(code: str, stored_hash: str, secret: str) -> bool:
    """Constant-time check that ``code`` hashes to ``stored_hash`` under ``secret``."""
    return hmac.compare_digest(hash_otp(code, secret), stored_hash or "")


def otp_secret() -> str:
    """The HMAC key for OTP hashing: the dedicated ``otp_secret`` if set, else the app secret."""
    from nexus.core.config import get_settings

    s = get_settings()
    return s.otp_secret or s.secret_key
