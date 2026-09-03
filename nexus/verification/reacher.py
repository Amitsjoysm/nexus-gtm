"""Reacher email verifier adapter (check-if-email-exists `/v0/check_email`).

The real deliverability backend. It MUST run on a separate host/IP (the governing
constraint) so bulk SMTP probing never spams from the app's own domain. This adapter is the
only place that talks to it; it never raises across the boundary — any network/timeout/parse
failure degrades to a low-confidence ``unknown`` so a flaky verifier host can never hang or
crash a campaign. Activated only when ``NEXUS_EMAIL_VERIFY_PROVIDER=reacher``; offline the
stub is used and this module is never constructed.
"""
from __future__ import annotations

import logging
import time

import httpx

from nexus.verification.provider import (
    STATUS_INVALID,
    STATUS_UNKNOWN,
    STATUS_VALID,
    EmailVerification,
    EmailVerificationProvider,
)

logger = logging.getLogger("nexus.verification.reacher")

# Reacher `is_reachable` verdict -> (our status, confidence).
_VERDICT = {
    "safe": (STATUS_VALID, 0.95),
    "invalid": (STATUS_INVALID, 0.95),
    "risky": ("risky", 0.40),
    "unknown": (STATUS_UNKNOWN, 0.20),
}

# ESP classification from the MX record hosts (lowercased, joined). Order matters: the
# office365 business needle (`mail.protection.outlook.com`) is checked before the consumer
# outlook needle (`outlook.com`), which it would otherwise also match.
_PROVIDER_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("gsuite", ("google.com", "googlemail", "l.google.com")),
    ("office365", ("mail.protection.outlook.com", "office365")),
    ("outlook", ("outlook.com", "hotmail", "live.com")),
    ("yahoo", ("yahoodns", "yahoo.com")),
]


def _classify_provider(records: list, misc: dict) -> str | None:
    if misc.get("is_disposable"):
        return "disposable"
    blob = " ".join((str(r) or "").lower() for r in (records or []))
    if not blob.strip():
        return None
    for ptype, needles in _PROVIDER_RULES:
        if any(n in blob for n in needles):
            return ptype
    return "custom"


class ReacherEmailVerifier(EmailVerificationProvider):
    name = "reacher"

    def __init__(
        self, *, url: str, timeout: float = 20.0, transport=None,
        fail_threshold: int = 2, cooldown_s: float = 60.0,
        auth_header: str | None = None,
    ):
        self.url = url
        self.timeout = timeout
        # ``transport`` is a test seam (httpx.MockTransport); None = real network.
        self._transport = transport
        # Optional ``Authorization`` header so a public HTTPS verifier endpoint can require a
        # token instead of being an open relay. None/blank = no header sent.
        self._headers = {"Authorization": auth_header} if auth_header else None
        # Circuit breaker: a down verifier must fail FAST, not block its timeout on every
        # guessed-email permutation (which would hang contact sourcing for minutes). After
        # ``fail_threshold`` consecutive failures the circuit opens for ``cooldown_s`` and
        # verifications return ``unknown`` instantly without touching the network.
        self._fail_threshold = max(1, fail_threshold)
        self._cooldown_s = cooldown_s
        self._consecutive_failures = 0
        self._open_until = 0.0

    async def verify_one(self, email: str) -> EmailVerification:
        if time.monotonic() < self._open_until:  # circuit open: verifier is known-down
            return self._fail_safe(email)
        try:
            # Cap the connect phase so an unreachable host fails in seconds, not the full read
            # timeout (which is for slow-but-up servers).
            timeout = httpx.Timeout(self.timeout, connect=min(5.0, self.timeout))
            async with httpx.AsyncClient(
                timeout=timeout, transport=self._transport
            ) as client:
                resp = await client.post(
                    self.url, json={"to_email": email}, headers=self._headers
                )
            if resp.status_code != 200:
                self._note_failure()
                return self._fail_safe(email)
            data = resp.json()
        except Exception as exc:  # never raise across the boundary
            logger.warning("reacher verify failed for %r: %r", email, exc)
            self._note_failure()
            return self._fail_safe(email)
        self._consecutive_failures = 0  # a success closes the circuit
        return self._map(email, data)

    def _note_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._fail_threshold:
            self._open_until = time.monotonic() + self._cooldown_s
            logger.warning(
                "reacher circuit opened for %.0fs after %d consecutive failures (%s)",
                self._cooldown_s, self._consecutive_failures, self.url,
            )

    def _fail_safe(self, email: str) -> EmailVerification:
        return EmailVerification(
            email=email, status=STATUS_UNKNOWN, confidence=0.0, source=self.name
        )

    # Why an address graded `risky`, in the order that decides how usable it is. Measured against
    # the live instance on 2026-09-02: `safe` never appeared once across real B2B addresses —
    # catch-all domains and role accounts both force `risky`, and prospecting addresses are
    # overwhelmingly one or the other. Flattening all of them to one amber label at 0.40 is what
    # makes verification look broken, because two of these are addresses the receiving server
    # explicitly ACCEPTED and one is a throwaway.
    #
    # (signal, reason, confidence) — first match wins.
    _RISKY_GRADES: tuple[tuple[str, str, float], ...] = (
        # A throwaway domain. Deliverable and worthless; the worst kind of risky.
        ("is_disposable", "disposable", 0.15),
        # Accepted, but the mailbox is full — mail may bounce today and land tomorrow.
        ("has_full_inbox", "full_inbox", 0.25),
        # The server accepts every recipient, so acceptance proves nothing about this mailbox.
        # Still the best of the risky outcomes when the server did accept it.
        ("is_catch_all", "catch_all", 0.55),
        # A shared inbox (info@, support@). Real, reachable, rarely the person you want.
        ("is_role_account", "role_account", 0.35),
    )

    def _map(self, email: str, data: dict) -> EmailVerification:
        reachable = str(data.get("is_reachable", "unknown")).lower()
        status, confidence = _VERDICT.get(reachable, (STATUS_UNKNOWN, 0.20))
        mx = data.get("mx") or {}
        misc = data.get("misc") or {}
        smtp = data.get("smtp") or {}
        provider_type = _classify_provider(mx.get("records"), misc)
        deliverable = bool(smtp.get("is_deliverable"))
        signals = {
            "is_catch_all": bool(smtp.get("is_catch_all")),
            "is_role_account": bool(misc.get("is_role_account")),
            "is_disposable": bool(misc.get("is_disposable")),
            "has_full_inbox": bool(smtp.get("has_full_inbox")),
            # The receiving server's own answer, previously dropped. It is the single most useful
            # fact behind a `risky` grade: "the server accepted this recipient" is a different
            # situation from "it did not", and the screen could not tell them apart.
            "is_deliverable": deliverable,
        }

        if status == "risky":
            # THE STATUS IS NOT PROMOTED. Reacher declined to certify the mailbox, and turning that
            # into `valid` would invent a certainty it explicitly withheld — which is how a
            # campaign bounces. Only the confidence and the stated reason change, so the UI can say
            # "accepted, catch-all domain" rather than an unexplained amber label.
            for key, reason, graded in self._RISKY_GRADES:
                if signals.get(key):
                    signals["risky_reason"] = reason
                    confidence = graded
                    break
            else:
                signals["risky_reason"] = "unspecified"
            # The server accepting the recipient is corroboration; not accepting it is not proof of
            # absence on a catch-all, so this only ever adds.
            #
            # Never for a disposable address: "the throwaway domain accepts mail" is not
            # reassurance, it is the thing that makes it useless. Corroborating deliverability
            # there would rank a burner above a real shared inbox.
            if deliverable and signals.get("risky_reason") != "disposable":
                confidence = min(0.75, confidence + 0.10)

        return EmailVerification(
            email=email, status=status, confidence=confidence, source=self.name,
            provider_type=provider_type, signals=signals,
        )
