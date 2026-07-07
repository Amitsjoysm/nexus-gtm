# tests/test_network_crypto.py
from __future__ import annotations


def test_seal_unseal_round_trip_and_opacity():
    from nexus.network.crypto import seal_tokens, unseal_tokens

    bundle = {"access_token": "AT-123", "refresh_token": "RT-456", "expires_at": 1900000000}
    blob = seal_tokens(bundle)
    assert isinstance(blob, dict) and "enc" in blob
    assert "AT-123" not in blob["enc"]  # ciphertext, not plaintext
    assert unseal_tokens(blob) == bundle


def test_unseal_tolerates_empty_and_garbage():
    from nexus.network.crypto import unseal_tokens

    assert unseal_tokens({}) == {}
    assert unseal_tokens({"enc": "not-a-valid-token"}) == {}
    assert unseal_tokens(None) == {}  # type: ignore[arg-type]
