# nexus/auth/mfa.py
"""Second-factor primitives: HOTP/TOTP, provisioning URIs, and recovery codes.

Pure functions, no database and no framework — the service layer (``nexus/auth/mfa_service.py``)
owns persistence. Implemented against RFC 4226 (HOTP) and RFC 6238 (TOTP) with nothing but
``hmac``/``hashlib``/``base64``/``secrets``: the repo rule is to reduce dependencies, and the
algorithm is thirty lines. ``tests/test_mfa_totp.py`` pins it to the published RFC vectors.

Three properties are load-bearing:

* **The seed is never stored in the clear.** :func:`seal_secret` wraps it in Fernet
  (``nexus.core.crypto``); a database leak yields no working authenticator.
* **Codes cannot be replayed.** A TOTP code stays valid for a whole step plus drift, so verifying
  "is this code currently correct" is not enough — an observed code would work again for up to a
  minute. :func:`verify_totp` takes the highest counter already accepted and refuses anything at
  or below it. The caller must persist the returned counter.
* **Comparison is constant-time.** Every candidate is checked with ``hmac.compare_digest`` and the
  loop does not exit early, so neither the code nor the matching drift offset leaks via timing.

Email OTP is the *same* primitive on a longer step (``mfa_email_code_step_s``, 5 minutes) rather
than a second mechanism: one verification path, one replay guard, and no extra table to hold an
in-flight code. Drift ±1 gives a mailed code between 5 and 10 minutes of life, matching the
registration OTP's TTL.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

from nexus.auth.otp import hash_otp, otp_secret

# Excludes I/O/0/1 — recovery codes get read off a screen and typed by hand under stress.
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_RECOVERY_GROUP = 5


# --------------------------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------------------------
def generate_secret(num_bytes: int = 20) -> str:
    """A fresh base32 seed. 20 bytes = 160 bits, the size RFC 4226 §4 R6 recommends.

    Padding is stripped because authenticator apps and QR scanners choke on ``=`` in the
    ``secret`` query parameter; :func:`_decode_secret` restores it.
    """
    return base64.b32encode(secrets.token_bytes(num_bytes)).decode("ascii").rstrip("=")


def _decode_secret(secret_b32: str) -> bytes:
    """Base32 → raw bytes, tolerating lower case, spaces and missing padding."""
    cleaned = (secret_b32 or "").strip().replace(" ", "").replace("-", "").upper()
    if not cleaned:
        raise ValueError("empty TOTP secret")
    cleaned += "=" * (-len(cleaned) % 8)
    return base64.b32decode(cleaned, casefold=True)


def seal_secret(secret_b32: str) -> str:
    """Fernet-seal a seed for the ``user_mfa.secret`` column."""
    from nexus.core.config import get_settings
    from nexus.core.crypto import seal_text

    return seal_text(secret_b32, key=get_settings().mfa_secret_enc_key)


def unseal_secret(sealed: str) -> str:
    """Unseal a stored seed. ``""`` when missing or tampered — the caller treats that as
    'this factor is unusable', which fails closed into 'verification fails', never 'allow'."""
    from nexus.core.config import get_settings
    from nexus.core.crypto import unseal_text

    return unseal_text(sealed, key=get_settings().mfa_secret_enc_key)


# --------------------------------------------------------------------------------------------
# HOTP / TOTP
# --------------------------------------------------------------------------------------------
def hotp_code(secret: bytes, counter: int, *, digits: int = 6) -> str:
    """RFC 4226 HOTP over HMAC-SHA1, dynamically truncated to ``digits``."""
    mac = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    binary = (
        (mac[offset] & 0x7F) << 24
        | mac[offset + 1] << 16
        | mac[offset + 2] << 8
        | mac[offset + 3]
    )
    return str(binary % (10**digits)).zfill(digits)


def counter_at(timestamp: float, step: int) -> int:
    """The RFC 6238 time counter T = floor(unix_time / step)."""
    return int(timestamp // max(1, step))


def totp_code(
    secret_b32: str,
    *,
    timestamp: float | None = None,
    step: int = 30,
    digits: int = 6,
) -> str:
    """The TOTP for ``secret_b32`` at ``timestamp`` (defaults to now)."""
    ts = time.time() if timestamp is None else timestamp
    return hotp_code(_decode_secret(secret_b32), counter_at(ts, step), digits=digits)


def verify_totp(
    secret_b32: str,
    code: str,
    *,
    timestamp: float | None = None,
    step: int = 30,
    digits: int = 6,
    drift: int = 1,
    last_counter: int | None = None,
) -> int | None:
    """Check ``code`` and return the counter it matched, or ``None``.

    ``drift`` steps either side of now are accepted (clock skew between phone and server).
    ``last_counter`` is the replay guard: a counter at or below one already spent is refused even
    though the code is arithmetically correct and still inside its window. Persist the returned
    value; without that, a code shoulder-surfed or lifted from a phishing proxy is reusable for
    the rest of its step.
    """
    code = (code or "").strip().replace(" ", "").replace("-", "")
    if not code or not code.isdigit() or len(code) != digits:
        return None
    try:
        secret = _decode_secret(secret_b32)
    except (ValueError, TypeError):
        return None

    ts = time.time() if timestamp is None else timestamp
    now_counter = counter_at(ts, step)
    matched: int | None = None
    # Deliberately no early break: the loop runs a fixed number of constant-time comparisons so
    # the response time does not reveal which offset (or whether any) matched.
    for offset in range(-abs(drift), abs(drift) + 1):
        candidate = now_counter + offset
        if candidate < 0:
            continue
        if hmac.compare_digest(hotp_code(secret, candidate, digits=digits), code):
            matched = candidate
    if matched is None:
        return None
    if last_counter is not None and matched <= last_counter:
        return None  # replay: this counter (or a later one) has already been spent
    return matched


def provisioning_uri(
    secret_b32: str,
    *,
    account_name: str,
    issuer: str,
    digits: int = 6,
    step: int = 30,
) -> str:
    """The ``otpauth://`` URI an authenticator app scans as a QR code (Key URI Format).

    The issuer appears twice on purpose — as the label prefix for older apps and as a query
    parameter for current ones — which is what the format specifies.
    """
    label = f"{quote(issuer, safe='')}:{quote(account_name, safe='')}"
    params = urlencode(
        {
            "secret": secret_b32,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": digits,
            "period": step,
        }
    )
    return f"otpauth://totp/{label}?{params}"


# --------------------------------------------------------------------------------------------
# Recovery codes
# --------------------------------------------------------------------------------------------
def generate_recovery_codes(count: int = 10, *, groups: int = 2) -> list[str]:
    """``count`` fresh break-glass codes, e.g. ``ABCDE-FGHJK``.

    Two groups of five from a 32-symbol alphabet is 50 bits — far beyond online guessing, which
    is why a keyed hash rather than a slow KDF is the right way to store them.
    """
    out: list[str] = []
    for _ in range(max(1, count)):
        parts = [
            "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_GROUP))
            for _ in range(max(1, groups))
        ]
        out.append("-".join(parts))
    return out


def normalize_recovery_code(code: str) -> str:
    """Canonical form for hashing/comparison: upper case, only alphabet characters.

    A user retyping ``abcde fghjk`` must match the stored ``ABCDE-FGHJK``; formatting is not a
    security property.
    """
    return "".join(ch for ch in (code or "").upper() if ch in _RECOVERY_ALPHABET)


def hash_recovery_code(code: str) -> str:
    """One-way HMAC-SHA256 of the normalized code, keyed exactly like the registration OTP."""
    return hash_otp(normalize_recovery_code(code), otp_secret())


def verify_recovery_code(code: str, stored_hash: str) -> bool:
    """Constant-time check of a candidate code against a stored digest."""
    return hmac.compare_digest(hash_recovery_code(code), stored_hash or "")
