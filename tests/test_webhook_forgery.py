# tests/test_webhook_forgery.py
"""The webhook endpoint is a public URL. Everything it trusts must come from the request.

Anyone who learns the URL can POST to it, so there is no network boundary doing any of this work.
Three independent guards, each of which is the whole defence when the others are bypassed:

* **Signature** — HMAC-SHA256 over ``{timestamp}.{raw_body}``, compared in constant time.
* **Freshness** — a valid signature is valid forever unless the timestamp is checked, so a
  captured request would be replayable indefinitely.
* **Exactly-once** — the provider's event id is the PRIMARY KEY of `billing_webhook_events`, so
  replay protection is a database constraint rather than an application check two concurrent
  deliveries could race.

These were verified live against the running deployment in round two; this makes them repeatable,
because a guard that is only ever checked by hand is a guard that silently regresses.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from nexus.billing.webhooks import (
    SignatureError,
    StaleWebhookError,
    WebhookError,
    verify_stripe_signature,
)

SECRET = "whsec_test_secret_value"


def _body(event_id: str = "evt_1", event_type: str = "invoice.paid") -> bytes:
    return json.dumps(
        {"id": event_id, "type": event_type, "data": {"object": {"id": "in_1"}}}
    ).encode()


def _sign(raw: bytes, secret: str = SECRET, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


class TestSignature:
    def test_a_correctly_signed_event_verifies(self):
        raw = _body()
        event = verify_stripe_signature(raw, _sign(raw), SECRET)
        assert event.event_id == "evt_1"
        assert event.event_type == "invoice.paid"

    def test_a_body_changed_after_signing_is_refused(self):
        """The attack that matters: take a real event, edit the amount, keep the signature."""
        raw = _body()
        header = _sign(raw)
        tampered = raw.replace(b'"in_1"', b'"in_ATTACKER"')
        with pytest.raises(SignatureError):
            verify_stripe_signature(tampered, header, SECRET)

    def test_a_signature_from_a_different_secret_is_refused(self):
        raw = _body()
        with pytest.raises(SignatureError):
            verify_stripe_signature(raw, _sign(raw, secret="whsec_attacker"), SECRET)

    @pytest.mark.parametrize("header", ["", "garbage", "v1=abc", "t=123", "t=abc,v1=def"])
    def test_a_malformed_or_missing_signature_header_is_refused(self, header):
        raw = _body()
        with pytest.raises((SignatureError, WebhookError)):
            verify_stripe_signature(raw, header, SECRET)

    def test_an_unconfigured_secret_refuses_everything(self):
        """Treating "no secret" as "allow" would make the whole check decorative — and that is
        the state a deployment is in before anyone sets the variable."""
        raw = _body()
        with pytest.raises(SignatureError):
            verify_stripe_signature(raw, _sign(raw), "")

    def test_an_empty_body_is_refused(self):
        with pytest.raises(SignatureError):
            verify_stripe_signature(b"", _sign(b""), SECRET)

    def test_a_rotated_secret_accepts_either_signature(self):
        """Multiple v1 entries are legitimate during a rotation; any one matching is enough."""
        raw = _body()
        ts = int(time.time())
        old = hmac.new(b"whsec_old", f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
        new = hmac.new(SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
        event = verify_stripe_signature(raw, f"t={ts},v1={old},v1={new}", SECRET)
        assert event.event_id == "evt_1"


class TestFreshness:
    def test_a_captured_request_stops_working(self):
        """Without this a signature is valid forever and a captured POST is a permanent key."""
        raw = _body()
        stale = int(time.time()) - 4000
        with pytest.raises(StaleWebhookError):
            verify_stripe_signature(raw, _sign(raw, ts=stale), SECRET)

    def test_a_timestamp_from_the_future_is_refused(self):
        """A clock the attacker controls must not buy an indefinitely valid window."""
        raw = _body()
        ahead = int(time.time()) + 4000
        with pytest.raises(StaleWebhookError):
            verify_stripe_signature(raw, _sign(raw, ts=ahead), SECRET)

    def test_the_tolerance_leaves_room_for_ordinary_clock_skew(self):
        raw = _body()
        recent = int(time.time()) - 120
        assert verify_stripe_signature(raw, _sign(raw, ts=recent), SECRET).event_id == "evt_1"


class TestMalformedPayload:
    def test_a_body_that_is_not_json_is_refused(self):
        raw = b"not json at all"
        with pytest.raises(WebhookError):
            verify_stripe_signature(raw, _sign(raw), SECRET)

    def test_a_json_array_is_refused(self):
        """An event must be an object; a list would sail past `.get()` as an AttributeError."""
        raw = json.dumps([1, 2, 3]).encode()
        with pytest.raises(WebhookError):
            verify_stripe_signature(raw, _sign(raw), SECRET)

    @pytest.mark.parametrize("payload", [{"type": "invoice.paid"}, {"id": "evt_1"}, {}])
    def test_an_event_missing_its_id_or_type_is_refused(self, payload):
        raw = json.dumps(payload).encode()
        with pytest.raises(WebhookError):
            verify_stripe_signature(raw, _sign(raw), SECRET)


class TestExactlyOnce:
    async def test_an_event_id_can_only_be_recorded_once(self):
        """Replay protection is the PRIMARY KEY, not an application check — so two concurrent
        deliveries cannot both pass a "have we seen this?" test and then both act."""
        from sqlalchemy.exc import IntegrityError

        from nexus.billing.webhooks import already_processed, mark_processed
        from nexus.core.db import get_platform_sessionmaker

        raw = _body("evt_dupe")
        event = verify_stripe_signature(raw, _sign(raw), SECRET)

        async with get_platform_sessionmaker()() as session:
            assert await already_processed(session, "evt_dupe") is False
            await mark_processed(session, event)
            await session.commit()

        async with get_platform_sessionmaker()() as session:
            assert await already_processed(session, "evt_dupe") is True

        # And the constraint holds even if the check is skipped entirely.
        async with get_platform_sessionmaker()() as session:
            with pytest.raises(IntegrityError):
                await mark_processed(session, event)
                await session.commit()

    async def test_a_rejected_event_leaves_no_row(self):
        """The dedupe table records only events that verified, so a bad signature leaves no
        trace there — which is why the metrics counter is its only evidence."""
        from sqlalchemy import func, select

        from nexus.core.db import get_platform_sessionmaker
        from nexus.models.billing import BillingWebhookEvent

        raw = _body("evt_forged")
        with pytest.raises(SignatureError):
            verify_stripe_signature(raw, _sign(raw, secret="wrong"), SECRET)

        async with get_platform_sessionmaker()() as session:
            n = await session.scalar(
                select(func.count(BillingWebhookEvent.id)).where(
                    BillingWebhookEvent.id == "evt_forged"
                )
            )
        assert n == 0
