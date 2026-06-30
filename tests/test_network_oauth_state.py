# tests/test_network_oauth_state.py
from __future__ import annotations

import time


def test_pkce_pair_is_valid():
    import base64
    import hashlib

    from nexus.network.oauth import make_pkce

    verifier, challenge = make_pkce()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_state_sign_verify_round_trip_and_tamper_reject():
    from nexus.network.oauth import sign_state, verify_state

    tok = sign_state(member_id="m1", tenant_id="t1", provider="google", verifier="v123")
    claims = verify_state(tok)
    assert claims and claims["mid"] == "m1" and claims["tid"] == "t1"
    assert claims["prov"] == "google" and claims["pkce"] == "v123"

    assert verify_state(tok + "x") is None          # tampered signature
    assert verify_state("not.a.jwt") is None         # garbage
    # an access token (wrong typ) must not pass as oauth state
    from nexus.core.security import create_access_token

    assert verify_state(create_access_token(user_id="u", tenant_id="t", role="rep")) is None


def test_state_expires():
    from nexus.network import oauth

    tok = oauth.sign_state(member_id="m1", tenant_id="t1", provider="google", verifier="v",
                           ttl_s=1)
    assert oauth.verify_state(tok) is not None
    time.sleep(2)
    assert oauth.verify_state(tok) is None
