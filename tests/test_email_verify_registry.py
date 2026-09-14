"""Email checks keep reaching the verifier for the whole life of the app process.

Found 2026-09-14 on marketjoy.com: the live Reacher answered `curtis.bent@marketjoy.com` invalid and
`curtis@marketjoy.com` valid, yet the product showed `curtis.bent@` as risky or unknown. The
registry sits in front of the verifier as a process-wide singleton, and three of its policies were
written for paid search and are wrong for verification:

* a budget of 64 calls per source for the whole process. Once spent, every check answered `unknown`
  without asking the verifier, and the finder fell back to first.last;
* a circuit breaker that, once open, never closed;
* a cache that kept every verdict forever, including `unknown` and the DNS fallback's `risky`, so a
  single verifier blip stayed on those addresses until the next deploy.

All offline: the verifiers are in-process fakes.
"""
from __future__ import annotations

from types import SimpleNamespace

import nexus.integrations.registry as registry_mod
from nexus.enrichment.providers import VerifyingPatternEmailProvider
from nexus.integrations.registry import DataSourceRegistry
from nexus.verification import (
    STATUS_INVALID,
    STATUS_RISKY,
    STATUS_UNKNOWN,
    STATUS_VALID,
    EmailVerification,
)
from nexus.verification.provider import CompositeEmailVerifier, EmailVerificationProvider

#: What the live verifier said about this domain on 2026-09-14.
REAL_MAILBOXES = {"curtis@marketjoy.com"}


class _Reacher(EmailVerificationProvider):
    """Answers like the live Reacher did for marketjoy.com, or fails safe to unknown when down."""

    name = "reacher"

    def __init__(self, *, down: bool = False) -> None:
        self.down = down
        self.calls = 0

    async def verify_one(self, email: str) -> EmailVerification:
        self.calls += 1
        if self.down:
            return EmailVerification(
                email=email, status=STATUS_UNKNOWN, confidence=0.0, source=self.name
            )
        status = STATUS_VALID if email in REAL_MAILBOXES else STATUS_INVALID
        return EmailVerification(email=email, status=status, confidence=0.95, source=self.name)


class _Dns(EmailVerificationProvider):
    """What `dns.py` answers for any domain that publishes MX records."""

    name = "dns"
    probes_mailbox = False

    async def verify_one(self, email: str) -> EmailVerification:
        return EmailVerification(email=email, status=STATUS_RISKY, confidence=0.5, source=self.name)


class _Scripted(EmailVerificationProvider):
    """Plays back outcomes in order, repeating the last: a status string, or an exception to raise."""

    name = "scripted"

    def __init__(self, outcomes: list) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def verify_one(self, email: str) -> EmailVerification:
        self.calls += 1
        outcome = self._outcomes[min(self.calls, len(self._outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return EmailVerification(email=email, status=outcome, confidence=0.9, source=self.name)


async def _find_curtis(reg: DataSourceRegistry):
    return await VerifyingPatternEmailProvider(verify=reg.verify_email).enrich(
        SimpleNamespace(domain="marketjoy.com"), SimpleNamespace(full_name="Curtis Bent")
    )


# ---- the registry keeps asking ------------------------------------------------------------------

async def test_email_checks_are_not_capped_for_the_life_of_the_process():
    verifier = _Scripted([STATUS_VALID])
    reg = DataSourceRegistry(email_verify=verifier)

    verdicts = [await reg.verify_email(f"person{i}@example.com") for i in range(100)]

    assert {v.status for v in verdicts} == {STATUS_VALID}
    assert verifier.calls == 100


async def test_a_failing_verifier_is_retried_instead_of_switched_off():
    verifier = _Scripted([RuntimeError("verifier down")] * 5 + [STATUS_VALID])
    reg = DataSourceRegistry(email_verify=verifier)

    for _ in range(5):
        assert (await reg.verify_email("a@example.com")).status == STATUS_UNKNOWN
    assert (await reg.verify_email("a@example.com")).status == STATUS_VALID
    assert verifier.calls == 6


async def test_an_unknown_verdict_is_not_kept():
    verifier = _Scripted([STATUS_UNKNOWN, STATUS_VALID])
    reg = DataSourceRegistry(email_verify=verifier)

    await reg.verify_email("a@example.com")

    assert (await reg.verify_email("a@example.com")).status == STATUS_VALID
    assert verifier.calls == 2


async def test_a_risky_verdict_is_not_kept():
    """A DNS fallback answers `risky` for every address on a domain with MX records. Keeping it would
    pin that answer on the address long after the mailbox verifier could have answered."""
    verifier = _Scripted([STATUS_RISKY, STATUS_VALID])
    reg = DataSourceRegistry(email_verify=verifier)

    await reg.verify_email("a@example.com")

    assert (await reg.verify_email("a@example.com")).status == STATUS_VALID
    assert verifier.calls == 2


async def test_a_conclusive_verdict_is_reused_until_it_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(registry_mod, "_clock", lambda: now[0])
    verifier = _Scripted([STATUS_VALID])
    reg = DataSourceRegistry(email_verify=verifier)

    await reg.verify_email("a@example.com")
    await reg.verify_email("a@example.com")
    assert verifier.calls == 1  # one lookup run does not probe the same mailbox twice

    now[0] += registry_mod.VERIFY_CACHE_TTL_S + 1
    await reg.verify_email("a@example.com")
    assert verifier.calls == 2  # people leave: a day-old "valid" is asked again


# ---- the finder, end to end -----------------------------------------------------------------------

async def test_the_finder_still_reaches_the_verifier_after_many_lookups():
    reg = DataSourceRegistry(email_verify=CompositeEmailVerifier([_Reacher(), _Dns()]))
    for i in range(64):
        await reg.verify_email(f"someone{i}@other.example")

    found = await _find_curtis(reg)

    assert (found.email, found.email_status) == ("curtis@marketjoy.com", STATUS_VALID)


async def test_the_finder_recovers_once_the_verifier_is_back():
    reacher = _Reacher(down=True)
    reg = DataSourceRegistry(email_verify=CompositeEmailVerifier([reacher, _Dns()]))

    degraded = await _find_curtis(reg)
    # While the mailbox verifier cannot answer, every pattern ties on the DNS fallback and the
    # earliest pattern is kept. That part is unchanged; what must not happen is it staying that way.
    assert degraded.email == "curtis.bent@marketjoy.com"

    reacher.down = False
    found = await _find_curtis(reg)

    assert (found.email, found.email_status) == ("curtis@marketjoy.com", STATUS_VALID)
