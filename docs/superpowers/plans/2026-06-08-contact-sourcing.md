# Contact Sourcing + Real Email Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a Segment Campaign target would be skipped for `SKIP_NO_CONTACT`, automatically source a deliverable contact (real email verifier + verifying email finder, offline-stubbed by default) and re-draft the target instead of skipping it.

**Architecture:** A new `ContactSourcingService` composes the existing `DataSourceRegistry` (net-new contact search + email verification) and `WaterfallEnricher` (email finding). It owns no orchestration — `CampaignService._draft_one` calls it once when a target hits `SKIP_NO_CONTACT`, threads the sourced `contact_id` through a one-shot `research_compose` re-run, and `_send_one` applies an explicit pre-send deliverability policy. A real `ReacherEmailVerifier` (`check-if-email-exists` / `/v0/check_email`, separately hosted) slots behind the existing `EmailVerificationProvider` seam, activated only by `NEXUS_` env; defaults stay stub so the suite is zero-network.

**Tech Stack:** Python 3.11+, async SQLAlchemy 2.0, FastAPI, Pydantic v2, httpx (async client, already vendored), Alembic, pytest (`asyncio_mode=auto`).

---

## File Structure

**New files:**
- `nexus/verification/reacher.py` — `ReacherEmailVerifier` adapter (real verdict, ESP type, signals).
- `nexus/integrations/contact_search.py` — `ContactCandidate`, `ContactSearchProvider` ABC, `StubContactSearchProvider`.
- `nexus/campaigns/sourcing.py` — `SourcingOutcome`, `ContactSourcingService`, singleton accessor.
- `migrations/versions/0006_contact_sourcing.py` — adds `campaigns.send_risky`.
- `tests/test_reacher_verifier.py`, `tests/test_email_finder.py`, `tests/test_contact_sourcing.py`, `tests/test_campaign_sourcing.py` — new test modules.

**Modified files:**
- `nexus/verification/provider.py` — `STATUS_RISKY`, `EmailVerification` optional fields, `build_email_verifier` reacher branch.
- `nexus/verification/__init__.py` — export `STATUS_RISKY`, `ReacherEmailVerifier`.
- `nexus/core/config.py` — seven new `NEXUS_` settings.
- `nexus/enrichment/providers.py` — `VerifyingPatternEmailProvider`, `EnrichmentResult` fields.
- `nexus/enrichment/waterfall.py` — carry `email_status`/`provider_type` through merge; wire verifying finder.
- `nexus/integrations/registry.py` — `contact_search()` capability + settings wiring.
- `nexus/orchestration/planner.py` — thread `contact_id` into the `research_compose` compose step.
- `nexus/orchestration/tools.py` — `ComposeMessageTool` stores `provider_type`/`email_signals` on the draft.
- `nexus/models/campaign.py` — `SKIP_UNVERIFIED`, `SKIP_RISKY`, `Campaign.send_risky`.
- `nexus/campaigns/service.py` — sourcing auto-retry in `_draft_one`; explicit pre-send policy in `_send_one`.
- `nexus/campaigns/schemas.py` — `CampaignIn.send_risky`, `CampaignOut.send_risky`.

**Working directory:** `.worktrees/contact-sourcing` (branch `feature/contact-sourcing`). All paths below are relative to it.

**Test command convention:** run from the worktree root. Full suite: `python -m pytest -q`. Single test: `python -m pytest tests/test_x.py::test_y -v`.

---

## Task 1: Verification verdict — STATUS_RISKY + EmailVerification fields

**Files:**
- Modify: `nexus/verification/provider.py`
- Modify: `nexus/verification/__init__.py`
- Test: `tests/test_reacher_verifier.py` (created here, grows in Task 2)

- [ ] **Step 1: Write the failing test**

Create `tests/test_reacher_verifier.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_reacher_verifier.py -v`
Expected: FAIL with `ImportError: cannot import name 'STATUS_RISKY'`.

- [ ] **Step 3: Add STATUS_RISKY and the optional fields**

In `nexus/verification/provider.py`, add the constant beside the others (after `STATUS_UNKNOWN = "unknown"`):

```python
STATUS_UNKNOWN = "unknown"
STATUS_RISKY = "risky"
```

Change the `field` import line at the top (currently `from dataclasses import dataclass`):

```python
from dataclasses import dataclass, field
```

Extend the `EmailVerification` dataclass with two optional fields and update `as_dict`:

```python
@dataclass(slots=True)
class EmailVerification:
    email: str
    status: str = STATUS_UNKNOWN
    confidence: float = 0.0
    source: str = ""
    # Deliverability adapters (e.g. Reacher) enrich these; stub leaves them at defaults so
    # nothing downstream that ignores them breaks.
    provider_type: str | None = None
    signals: dict = field(default_factory=dict)

    @property
    def is_deliverable(self) -> bool:
        return self.status == STATUS_VALID

    def as_dict(self) -> dict:
        return {"email": self.email, "status": self.status,
                "confidence": self.confidence, "source": self.source,
                "provider_type": self.provider_type, "signals": dict(self.signals)}
```

- [ ] **Step 4: Export STATUS_RISKY**

In `nexus/verification/__init__.py`, add `STATUS_RISKY` to both the import block and `__all__`:

```python
from nexus.verification.provider import (
    STATUS_INVALID,
    STATUS_RISKY,
    STATUS_UNKNOWN,
    STATUS_VALID,
    EmailVerification,
    EmailVerificationProvider,
    StubEmailVerificationProvider,
    build_email_verifier,
    get_email_verifier,
    set_email_verifier,
)

__all__ = [
    "STATUS_INVALID",
    "STATUS_RISKY",
    "STATUS_UNKNOWN",
    "STATUS_VALID",
    "EmailVerification",
    "EmailVerificationProvider",
    "StubEmailVerificationProvider",
    "build_email_verifier",
    "get_email_verifier",
    "set_email_verifier",
]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_reacher_verifier.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add nexus/verification/provider.py nexus/verification/__init__.py tests/test_reacher_verifier.py
git commit -m "feat(verification): add STATUS_RISKY and EmailVerification provider_type/signals"
```

---

## Task 2: ReacherEmailVerifier adapter

**Files:**
- Create: `nexus/verification/reacher.py`
- Modify: `nexus/verification/provider.py` (build branch)
- Modify: `nexus/verification/__init__.py` (export)
- Modify: `nexus/core/config.py` (the three verifier settings this adapter reads — added fully in Task 3; add only what's needed here)
- Test: `tests/test_reacher_verifier.py`

**Note on settings:** this task references `settings.email_verify_url` and `settings.email_verify_timeout_s`. Add just those two settings now (Task 3 adds the rest). In `nexus/core/config.py`, under the `# LLM`/provider block near `email_verify_provider`, add:

```python
    email_verify_url: str = "http://158.69.113.127:8080/v0/check_email"
    email_verify_timeout_s: float = 20.0
```

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reacher_verifier.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_reacher_verifier.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.verification.reacher'`.

- [ ] **Step 3: Implement the adapter**

Create `nexus/verification/reacher.py`:

```python
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

    def __init__(self, *, url: str, timeout: float = 20.0, transport=None):
        self.url = url
        self.timeout = timeout
        # ``transport`` is a test seam (httpx.MockTransport); None = real network.
        self._transport = transport

    async def verify_one(self, email: str) -> EmailVerification:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport
            ) as client:
                resp = await client.post(self.url, json={"to_email": email})
            if resp.status_code != 200:
                return self._fail_safe(email)
            data = resp.json()
        except Exception as exc:  # never raise across the boundary
            logger.warning("reacher verify failed for %r: %r", email, exc)
            return self._fail_safe(email)
        return self._map(email, data)

    def _fail_safe(self, email: str) -> EmailVerification:
        return EmailVerification(
            email=email, status=STATUS_UNKNOWN, confidence=0.0, source=self.name
        )

    def _map(self, email: str, data: dict) -> EmailVerification:
        reachable = str(data.get("is_reachable", "unknown")).lower()
        status, confidence = _VERDICT.get(reachable, (STATUS_UNKNOWN, 0.20))
        mx = data.get("mx") or {}
        misc = data.get("misc") or {}
        smtp = data.get("smtp") or {}
        provider_type = _classify_provider(mx.get("records"), misc)
        signals = {
            "is_catch_all": bool(smtp.get("is_catch_all")),
            "is_role_account": bool(misc.get("is_role_account")),
            "is_disposable": bool(misc.get("is_disposable")),
            "has_full_inbox": bool(smtp.get("has_full_inbox")),
        }
        return EmailVerification(
            email=email, status=status, confidence=confidence, source=self.name,
            provider_type=provider_type, signals=signals,
        )
```

- [ ] **Step 4: Wire the build branch and export**

In `nexus/verification/provider.py`, replace the `build_email_verifier` body:

```python
def build_email_verifier(name: str) -> EmailVerificationProvider:
    key = (name or "").strip().lower()
    if key in ("stub", "", "none"):
        return StubEmailVerificationProvider()
    if key == "reacher":
        from nexus.core.config import get_settings
        from nexus.verification.reacher import ReacherEmailVerifier

        s = get_settings()
        return ReacherEmailVerifier(
            url=s.email_verify_url, timeout=s.email_verify_timeout_s
        )
    # Unknown keys still fail safe to the offline stub.
    return StubEmailVerificationProvider()
```

In `nexus/verification/__init__.py`, add the adapter export (append a separate import + `__all__` entry):

```python
from nexus.verification.reacher import ReacherEmailVerifier
```

Add `"ReacherEmailVerifier",` to `__all__`.

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_reacher_verifier.py -v`
Expected: PASS (all tests).

- [ ] **Step 6: Commit**

```bash
git add nexus/verification/reacher.py nexus/verification/provider.py nexus/verification/__init__.py nexus/core/config.py tests/test_reacher_verifier.py
git commit -m "feat(verification): add ReacherEmailVerifier adapter with ESP-type + signals"
```

---

## Task 3: Configuration settings

**Files:**
- Modify: `nexus/core/config.py`
- Test: `tests/test_reacher_verifier.py` (one settings assertion) — or inline in this task's verification

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reacher_verifier.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_reacher_verifier.py::test_contact_sourcing_settings_defaults -v`
Expected: FAIL with `AttributeError`/`assert` on `email_finder_max_candidates`.

- [ ] **Step 3: Add the remaining settings**

In `nexus/core/config.py`, the `email_verify_url`/`email_verify_timeout_s` pair was added in Task 2. Add the rest near them (the contact-sourcing block). Place after `email_verify_timeout_s`:

```python
    # Contact sourcing (sub-project B): net-new contact providers + the verifying email
    # finder. Defaults stay offline (stub) so CI is zero-network; activation is one env line.
    email_finder_max_candidates: int = 5        # permutation cap per contact
    contact_search_sources: str = "stub"        # ordered net-new contact providers
    campaign_sourcing_enabled: bool = True       # inline auto-retry on SKIP_NO_CONTACT
    campaign_sourced_min_send_confidence: float = 0.5  # bar a sourced address must clear to send
```

Add a CSV property beside `signal_source_list`:

```python
    @property
    def contact_search_source_list(self) -> list[str]:
        return self._csv_list(self.contact_search_sources)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_reacher_verifier.py::test_contact_sourcing_settings_defaults -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add nexus/core/config.py tests/test_reacher_verifier.py
git commit -m "feat(config): add contact-sourcing settings (finder cap, sources, send bar)"
```

---

## Task 4: VerifyingPatternEmailProvider + waterfall verdict passthrough

**Files:**
- Modify: `nexus/enrichment/providers.py`
- Modify: `nexus/enrichment/waterfall.py`
- Test: `tests/test_email_finder.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_email_finder.py`:

```python
"""Verifying email finder: permutation, early-stop on valid, catch-all, degrade. Offline."""
from __future__ import annotations

from nexus.enrichment.providers import (
    PatternEmailProvider,
    VerifyingPatternEmailProvider,
)
from nexus.enrichment.waterfall import WaterfallEnricher
from nexus.models.account import Account, Contact
from nexus.verification import (
    STATUS_INVALID,
    STATUS_UNKNOWN,
    STATUS_VALID,
    EmailVerification,
)
from tests.conftest import make_tenant, tenant_session


def _fake_verify(verdicts: dict):
    """Return an async verify(email) -> EmailVerification driven by a {email: (status,conf,signals)} map.
    Unlisted emails resolve to unknown/0.2."""

    async def verify(email: str) -> EmailVerification:
        status, conf, signals = verdicts.get(
            email, (STATUS_UNKNOWN, 0.2, {})
        )
        return EmailVerification(
            email=email, status=status, confidence=conf, source="fake", signals=signals
        )

    return verify


async def test_finder_generates_expected_permutations():
    prov = VerifyingPatternEmailProvider(verify=_fake_verify({}))
    cands = prov._candidates("Jane Doe", "acme.com")
    assert cands == [
        "jane.doe@acme.com",
        "janedoe@acme.com",
        "jdoe@acme.com",
        "jane@acme.com",
        "j.doe@acme.com",
    ]


async def test_finder_caps_candidates():
    prov = VerifyingPatternEmailProvider(verify=_fake_verify({}), max_candidates=2)
    cands = prov._candidates("Jane Doe", "acme.com")
    assert len(cands) == 2


async def test_finder_stops_on_first_valid():
    calls = []

    async def verify(email):
        calls.append(email)
        if email == "janedoe@acme.com":
            return EmailVerification(email=email, status=STATUS_VALID, confidence=0.95)
        return EmailVerification(email=email, status=STATUS_INVALID, confidence=0.95)

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe")
        res = await VerifyingPatternEmailProvider(verify=verify).enrich(acc, contact)
    assert res.email == "janedoe@acme.com"
    assert res.email_confidence == 0.95
    assert res.email_status == STATUS_VALID
    # Stopped after the second candidate; never probed jdoe/jane/j.doe.
    assert calls == ["jane.doe@acme.com", "janedoe@acme.com"]


async def test_finder_catch_all_short_circuits_to_canonical_risky():
    calls = []

    async def verify(email):
        calls.append(email)
        return EmailVerification(
            email=email, status="risky", confidence=0.4,
            signals={"is_catch_all": True},
        )

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe")
        res = await VerifyingPatternEmailProvider(verify=verify).enrich(acc, contact)
    assert res.email == "jane.doe@acme.com"  # canonical guess
    assert res.email_status == "risky"
    assert res.email_confidence == 0.5
    assert calls == ["jane.doe@acme.com"]  # did not blast further permutations


async def test_finder_degrades_to_best_unknown_when_no_valid():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe")
        res = await VerifyingPatternEmailProvider(
            verify=_fake_verify({})
        ).enrich(acc, contact)
    # All unknown -> returns the canonical guess at the unknown verdict confidence.
    assert res.email == "jane.doe@acme.com"
    assert res.email_status == STATUS_UNKNOWN
    assert res.found is True


async def test_finder_not_found_when_no_domain():
    prov = VerifyingPatternEmailProvider(verify=_fake_verify({}))

    class _C:
        full_name = "Jane Doe"

    class _A:
        domain = None

    res = await prov.enrich(_A(), _C())
    assert res.found is False


async def test_waterfall_pattern_fallback_after_finder_offline():
    """Offline: finder returns unknown; blind pattern (0.4) still wins the email."""
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = Account(tenant_id=tid, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        contact = Contact(tenant_id=tid, account_id=acc.id, full_name="Jane Doe")
        ts.add(contact)
        await ts.flush()
        enricher = WaterfallEnricher(
            providers=[
                VerifyingPatternEmailProvider(verify=_fake_verify({})),
                PatternEmailProvider(),
            ]
        )
        res = await enricher.enrich_contact(ts, contact, acc)
    assert contact.email == "jane.doe@acme.com"
    assert contact.email_confidence == 0.4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_email_finder.py -v`
Expected: FAIL with `ImportError: cannot import name 'VerifyingPatternEmailProvider'`.

- [ ] **Step 3: Add EnrichmentResult fields + the verifying finder**

In `nexus/enrichment/providers.py`, extend `EnrichmentResult` with two optional fields:

```python
@dataclass(slots=True)
class EnrichmentResult:
    found: bool = False
    email: str | None = None
    email_confidence: float = 0.0
    phone: str | None = None
    phone_confidence: float = 0.0
    source: str | None = None
    # Deliverability verdict that travels back with a found email (verifying finder).
    email_status: str | None = None
    provider_type: str | None = None

    @property
    def best_confidence(self) -> float:
        return max(self.email_confidence, self.phone_confidence)
```

Add the new provider at the end of the file. Add the imports it needs at the top (`from typing import Awaitable, Callable` and the verification import):

```python
from typing import Awaitable, Callable

from nexus.verification import STATUS_INVALID, STATUS_VALID, EmailVerification
```

Then the class:

```python
class VerifyingPatternEmailProvider(EnrichmentProvider):
    """Permutation-based email finder that *scores* each guess via a real verifier.

    Builds a bounded set of corporate-email permutations from the contact's name + the
    account domain, verifies them in priority order, and stops on the first ``valid``. It is
    catch-all aware (a catch-all domain makes every guess look deliverable, so it returns the
    canonical guess flagged risky rather than blasting probes). With no real verifier wired,
    every probe comes back ``unknown`` and the blind ``PatternEmailProvider`` after it in the
    waterfall still supplies the 0.4 guess — so the offline path is unchanged.
    """

    name = "pattern_verified"

    def __init__(
        self,
        *,
        verify: Callable[[str], Awaitable[EmailVerification]] | None = None,
        max_candidates: int | None = None,
    ):
        self._verify = verify
        self._max = max_candidates

    async def _resolve_verify(self) -> Callable[[str], Awaitable[EmailVerification]]:
        if self._verify is not None:
            return self._verify
        # Lazy default: the registry's cached, policy-wrapped verifier.
        from nexus.integrations.registry import get_registry

        return get_registry().verify_email

    def _cap(self) -> int:
        if self._max is not None:
            return self._max
        from nexus.core.config import get_settings

        return get_settings().email_finder_max_candidates

    def _candidates(self, full_name: str, domain: str) -> list[str]:
        parts = re.split(r"\s+", (full_name or "").strip().lower())
        first = parts[0] if parts else ""
        last = parts[-1] if len(parts) > 1 else ""
        domain = (domain or "").lower().lstrip("@")
        if not first or not domain:
            return []
        if last:
            locals_ = [f"{first}.{last}", f"{first}{last}", f"{first[0]}{last}",
                       first, f"{first[0]}.{last}"]
        else:
            locals_ = [first]
        seen: list[str] = []
        for loc in locals_:
            email = f"{loc}@{domain}"
            if email not in seen:
                seen.append(email)
        return seen[: self._cap()]

    async def enrich(self, account: Account, contact: Contact) -> EnrichmentResult:
        cands = self._candidates(contact.full_name, account.domain or "")
        if not cands:
            return EnrichmentResult()
        verify = await self._resolve_verify()
        canonical = cands[0]

        best: tuple[float, str, EmailVerification] | None = None  # (rank, email, verdict)
        for i, email in enumerate(cands):
            verdict = await verify(email)
            if i == 0 and verdict.signals.get("is_catch_all"):
                # Catch-all: every guess "works"; return canonical guess flagged risky.
                return EnrichmentResult(
                    found=True, email=canonical, email_confidence=0.5,
                    email_status="risky", provider_type=verdict.provider_type,
                    source=self.name,
                )
            if verdict.status == STATUS_VALID:
                return EnrichmentResult(
                    found=True, email=email, email_confidence=verdict.confidence,
                    email_status=STATUS_VALID, provider_type=verdict.provider_type,
                    source=self.name,
                )
            if verdict.status != STATUS_INVALID:
                rank = 2.0 if verdict.status == "risky" else 1.0
                if best is None or rank > best[0]:
                    best = (rank, email, verdict)

        if best is None:
            return EnrichmentResult()  # everything invalid
        _, _, verdict = best
        # Return the canonical guess (deterministic) at the best non-invalid verdict.
        return EnrichmentResult(
            found=True, email=canonical, email_confidence=verdict.confidence,
            email_status=verdict.status, provider_type=verdict.provider_type,
            source=self.name,
        )
```

Note: the degrade case returns `canonical` (first permutation) so offline counts are deterministic; `email_confidence` is the unknown verdict's confidence (0.2 from the stub), which the blind `PatternEmailProvider` (0.4) then beats in the waterfall.

- [ ] **Step 4: Carry the verdict through the waterfall merge**

In `nexus/enrichment/waterfall.py`, when an email wins, also copy `email_status`/`provider_type`. Replace the email-merge line inside the provider loop:

```python
            # Keep the highest-confidence value for each field.
            if r.email and r.email_confidence > merged.email_confidence:
                merged.email, merged.email_confidence = r.email, r.email_confidence
                merged.email_status = r.email_status
                merged.provider_type = r.provider_type
                merged.source = r.source
```

(Leave the phone branch and the rest untouched.)

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_email_finder.py -v`
Expected: PASS (all tests).

- [ ] **Step 6: Run the existing enrichment tests (no regression)**

Run: `python -m pytest tests/test_enrichment.py -v`
Expected: PASS (unchanged).

- [ ] **Step 7: Commit**

```bash
git add nexus/enrichment/providers.py nexus/enrichment/waterfall.py tests/test_email_finder.py
git commit -m "feat(enrichment): add verifying email finder + carry verdict through waterfall"
```

---

## Task 5: Net-new contact search (provider + registry capability)

**Files:**
- Create: `nexus/integrations/contact_search.py`
- Modify: `nexus/integrations/registry.py`
- Test: `tests/test_contact_search.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_contact_search.py`:

```python
"""Net-new contact search: stub determinism + registry waterfall/dedupe/policy. Offline."""
from __future__ import annotations

from nexus.integrations.contact_search import (
    ContactCandidate,
    ContactSearchProvider,
    StubContactSearchProvider,
)
from nexus.integrations.registry import DataSourceRegistry
from nexus.models.account import Account
from tests.conftest import make_tenant, tenant_session


async def test_stub_returns_one_deterministic_candidate_with_buyer_title():
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        prov = StubContactSearchProvider()
        cands = await prov.search(acc, {"buyer_titles": ["VP Sales", "CRO"]}, limit=3)
    assert len(cands) == 1
    assert cands[0].title == "VP Sales"
    assert cands[0].full_name == "Acme Lead"
    assert cands[0].email is None
    assert cands[0].source == "stub"


async def test_stub_defaults_title_when_no_buyer_titles():
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        cands = await StubContactSearchProvider().search(acc, {}, limit=3)
    assert cands[0].title == "Decision Maker"


async def test_registry_contact_search_dedupes_by_name_title():
    class Fake(ContactSearchProvider):
        def __init__(self, name, cands):
            self.name = name
            self._cands = cands

        async def search(self, account, icp, *, limit=3):
            return list(self._cands)[:limit]

    dup = ContactCandidate(full_name="Jane Doe", title="VP", source="a")
    same = ContactCandidate(full_name="Jane Doe", title="VP", source="b")
    other = ContactCandidate(full_name="Bob Roe", title="Director", source="b")
    reg = DataSourceRegistry(
        contact_search=[Fake("a", [dup]), Fake("b", [same, other])]
    )
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        out = await reg.contact_search(acc, {}, limit=5)
    keys = {(c.full_name, c.title) for c in out}
    assert keys == {("Jane Doe", "VP"), ("Bob Roe", "Director")}
    assert len(out) == 2


async def test_registry_contact_search_empty_when_no_providers_find():
    reg = DataSourceRegistry(contact_search=[StubContactSearchProvider()])
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        out = await reg.contact_search(acc, {"buyer_titles": ["CTO"]}, limit=3)
    assert len(out) == 1
    assert out[0].title == "CTO"


async def test_build_registry_wires_stub_contact_search_by_default():
    from nexus.integrations.registry import build_registry_from_settings

    reg = build_registry_from_settings()
    assert [p.name for p in reg.contact_search_providers] == ["stub"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_contact_search.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.integrations.contact_search'`.

- [ ] **Step 3: Implement the provider module**

Create `nexus/integrations/contact_search.py`:

```python
"""Net-new contact discovery: find a buying-committee person for an account.

The offline default returns one deterministic stub persona so the sourcing path is fully
exercisable with zero network and reproducible test counts. Real providers (Apollo / InfoJoy /
ZoomInfo) slot in behind this same ABC later, wired through ``contact_search_sources``.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field

from nexus.models.account import Account


@dataclass(slots=True)
class ContactCandidate:
    full_name: str
    title: str | None = None
    seniority: str | None = None
    email: str | None = None
    linkedin_url: str | None = None
    source: str = ""
    confidence: float = 0.0
    provenance: dict = field(default_factory=dict)


class ContactSearchProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def search(
        self, account: Account, icp: dict, *, limit: int = 3
    ) -> list[ContactCandidate]: ...


class StubContactSearchProvider(ContactSearchProvider):
    """Deterministic offline persona: one candidate, titled from the ICP's first buyer title."""

    name = "stub"

    async def search(
        self, account: Account, icp: dict, *, limit: int = 3
    ) -> list[ContactCandidate]:
        buyer_titles = (icp or {}).get("buyer_titles") or []
        title = buyer_titles[0] if buyer_titles else "Decision Maker"
        return [
            ContactCandidate(
                full_name=f"{account.name} Lead",
                title=title,
                email=None,
                source=self.name,
                confidence=0.3,
            )
        ]
```

- [ ] **Step 4: Add the registry capability + wiring**

In `nexus/integrations/registry.py`, add the import at the top (beside the other integration imports):

```python
from nexus.integrations.contact_search import (
    ContactCandidate,
    ContactSearchProvider,
    StubContactSearchProvider,
)
```

Extend `DataSourceRegistry.__init__` signature + body. Add the parameter (after `email_verify`) and store the providers:

```python
        email_verify: EmailVerificationProvider | None = None,
        contact_search: list[ContactSearchProvider] | None = None,
        per_source_budget: int = 64,
```

In the body, beside `self.email_verifier = email_verify`:

```python
        self.contact_search_providers = contact_search or [StubContactSearchProvider()]
```

Add the capability method (place after `verify_email`, before `source_health`):

```python
    # ----------------------------------------------------------- contact_search
    async def contact_search(
        self, account, icp: dict, *, limit: int = 3
    ) -> list[ContactCandidate]:
        """Waterfall the configured contact providers, deduping by (full_name, title)."""
        key = _norm_key("contact_search", account.id, icp, limit)
        if self._cache_enabled and key in self._cache:
            return list(self._cache[key])  # type: ignore[arg-type]

        merged: dict[tuple, ContactCandidate] = {}
        order: list[tuple] = []
        for provider in self.contact_search_providers:
            found = await self._policy.call(
                provider.name,
                lambda p=provider: p.search(account, icp, limit=limit),
            )
            for cand in found or []:
                ck = ((cand.full_name or "").lower(), (cand.title or "").lower())
                if ck not in merged:
                    merged[ck] = cand
                    order.append(ck)
        result = [merged[k] for k in order][:limit]
        if self._cache_enabled:
            self._cache[key] = list(result)
        return result
```

Add the source builder (beside `_build_company_search`):

```python
def _build_contact_search(sources: list[str]) -> list[ContactSearchProvider]:
    providers: list[ContactSearchProvider] = []
    for token in sources:
        key = token.strip().lower()
        if not key:
            continue
        if key == "stub":
            providers.append(StubContactSearchProvider())
        else:
            # Apollo / InfoJoy / ZoomInfo adapters land here later; skip unknown tokens
            # rather than silently substituting another source.
            logger.warning("unknown contact_search source %r; skipping", token)
    if not providers:
        providers.append(StubContactSearchProvider())
    return providers
```

Wire it in `build_registry_from_settings` (add to the `DataSourceRegistry(...)` call):

```python
        research=build_research_provider(s.research_provider),
        email_verify=build_email_verifier(s.email_verify_provider),
        contact_search=_build_contact_search(s.contact_search_source_list),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_contact_search.py -v`
Expected: PASS (all tests).

- [ ] **Step 6: Run existing registry tests (no regression)**

Run: `python -m pytest tests/test_registry.py -v`
Expected: PASS (unchanged).

- [ ] **Step 7: Commit**

```bash
git add nexus/integrations/contact_search.py nexus/integrations/registry.py tests/test_contact_search.py
git commit -m "feat(integrations): add contact_search capability + stub provider"
```

---

## Task 6: Thread contact_id through research_compose + draft provider signals

**Files:**
- Modify: `nexus/orchestration/planner.py`
- Modify: `nexus/orchestration/tools.py`
- Test: `tests/test_orchestration.py` (append) — and `tests/test_campaign_sourcing.py` later exercises end-to-end

- [ ] **Step 1: Write the failing test**

Append to `tests/test_orchestration.py` (it already imports the planner; if not, add `from nexus.orchestration.planner import get_planner`):

```python
def test_research_compose_threads_contact_id_into_compose_step():
    from nexus.orchestration.planner import get_planner

    plan = get_planner().plan("research_compose", {"contact_id": "c123"})
    compose = next(s for s in plan if s["tool"] == "compose_message")
    assert compose["inputs"].get("contact_id") == "c123"
    # No send step in the draft-phase recipe.
    assert all(s["tool"] != "send_message" for s in plan)


def test_research_compose_omits_contact_id_when_absent():
    from nexus.orchestration.planner import get_planner

    plan = get_planner().plan("research_compose", {})
    compose = next(s for s in plan if s["tool"] == "compose_message")
    assert "contact_id" not in compose["inputs"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_orchestration.py::test_research_compose_threads_contact_id_into_compose_step -v`
Expected: FAIL (compose step has empty inputs).

- [ ] **Step 3: Thread contact_id in the recipe**

In `nexus/orchestration/planner.py`, replace `_research_compose_plan`:

```python
def _research_compose_plan(goal_input: dict) -> list[PlanStep]:
    """Draft-phase goal for the Segment Campaign Engine: research, score, and compose a
    grounded draft — but NO send and NO approval gate. When the caller supplies a
    ``contact_id`` (a sourced/targeted person), thread it into the compose step so the
    messaging agent drafts to exactly that contact instead of defaulting to ``contacts[0]``.
    The send happens later, once, in the campaign send phase, so the outbound gates stay in
    exactly one place."""
    compose_inputs: dict = {}
    if goal_input.get("contact_id"):
        compose_inputs["contact_id"] = goal_input["contact_id"]
    return [
        PlanStep(idx=0, tool="research", depends_on=[]),
        PlanStep(idx=1, tool="scoring", depends_on=[0]),
        PlanStep(idx=2, tool="compose_message", inputs=compose_inputs, depends_on=[1]),
    ]
```

- [ ] **Step 4: Store provider signals on the draft**

In `nexus/orchestration/tools.py`, inside `ComposeMessageTool.run`, capture the provider type/signals when verifying, and add them to the staged draft. Replace the verify block + draft dict:

```python
        # Verify the recipient *now* ...
        email_status: str | None = None
        email_confidence: float | None = None
        provider_type: str | None = None
        email_signals: dict = {}
        contact_id = out.get("contact_id")
        if contact_id:
            contact = await tc.ts.get(Contact, contact_id)
            if contact is not None and contact.email:
                verdict = await tc.runtime.registry.verify_email(contact.email)
                email_status = verdict.status
                email_confidence = verdict.confidence
                provider_type = verdict.provider_type
                email_signals = dict(verdict.signals)
                contact.email_status = verdict.status

        tc.blackboard["draft"] = {
            "contact_id": contact_id,
            "subject": out.get("subject", ""),
            "body": out.get("body", ""),
            "message": out.get("message", ""),
            "grounded": grounded,
            "grounding": {
                "facts": list(facts)[:6],
                "sources": research.get("sources", []),
            },
            "email_status": email_status,
            "email_confidence": email_confidence,
            "provider_type": provider_type,
            "email_signals": email_signals,
        }
        return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_orchestration.py -v`
Expected: PASS (new tests + existing).

- [ ] **Step 6: Commit**

```bash
git add nexus/orchestration/planner.py nexus/orchestration/tools.py tests/test_orchestration.py
git commit -m "feat(orchestration): thread contact_id into research_compose + draft ESP signals"
```

---

## Task 7: ContactSourcingService

**Files:**
- Create: `nexus/campaigns/sourcing.py`
- Test: `tests/test_contact_sourcing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_contact_sourcing.py`:

```python
"""ContactSourcingService.ensure_contact: create persona / fill email / no-candidate. Offline."""
from __future__ import annotations

from nexus.campaigns.sourcing import ContactSourcingService, SourcingOutcome
from nexus.enrichment.providers import PatternEmailProvider
from nexus.enrichment.waterfall import WaterfallEnricher
from nexus.integrations.contact_search import StubContactSearchProvider
from nexus.integrations.registry import DataSourceRegistry
from nexus.models.account import Account, Contact
from tests.conftest import make_tenant, tenant_session


def _service() -> ContactSourcingService:
    registry = DataSourceRegistry(contact_search=[StubContactSearchProvider()])
    enricher = WaterfallEnricher(providers=[PatternEmailProvider()])
    return ContactSourcingService(registry=registry, enricher=enricher)


async def test_zero_contact_account_sources_persona_and_email():
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        outcome = await _service().ensure_contact(ts, acc, icp={"buyer_titles": ["VP Sales"]})
    assert isinstance(outcome, SourcingOutcome)
    assert outcome.sourced is True
    assert outcome.contact is not None
    assert outcome.contact.full_name == "Acme Lead"
    assert outcome.contact.title == "VP Sales"
    assert outcome.contact.email == "acme.lead@acme.com"
    assert outcome.contact.enrichment_source.startswith("sourcing:")
    assert outcome.email_confidence == 0.4


async def test_emailless_existing_contact_filled_in_place():
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        c = Contact(tenant_id=ts.tenant_id, account_id=acc.id, full_name="Jane Doe")
        ts.add(c)
        await ts.flush()
        outcome = await _service().ensure_contact(ts, acc, icp={})
    assert outcome.sourced is True
    assert outcome.contact.id == c.id           # same row, filled in place
    assert outcome.contact.email == "jane.doe@acme.com"


async def test_existing_contact_with_email_is_returned_not_re_sourced():
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        c = Contact(tenant_id=ts.tenant_id, account_id=acc.id, full_name="Jane Doe",
                    email="jane@acme.com", email_confidence=0.9)
        ts.add(c)
        await ts.flush()
        outcome = await _service().ensure_contact(ts, acc, icp={})
    assert outcome.contact.id == c.id
    assert outcome.sourced is False
    assert outcome.email_confidence == 0.9


async def test_no_candidate_returns_empty_outcome():
    class Empty(StubContactSearchProvider):
        async def search(self, account, icp, *, limit=3):
            return []

    registry = DataSourceRegistry(contact_search=[Empty()])
    enricher = WaterfallEnricher(providers=[PatternEmailProvider()])
    svc = ContactSourcingService(registry=registry, enricher=enricher)
    async with tenant_session(await make_tenant()) as ts:
        acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
        ts.add(acc)
        await ts.flush()
        outcome = await svc.ensure_contact(ts, acc, icp={})
    assert outcome.contact is None
    assert outcome.sourced is False
    assert outcome.email_confidence == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_contact_sourcing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.campaigns.sourcing'`.

- [ ] **Step 3: Implement the service**

Create `nexus/campaigns/sourcing.py`:

```python
"""ContactSourcingService: ensure an account has a contact with a (best-effort) email.

Composes the registry (net-new contact search) and the waterfall enricher (verifying email
finder). Owns no orchestration — the campaign draft phase calls it once when a target would be
skipped for ``SKIP_NO_CONTACT``. Never raises across its boundary: a no-candidate / failed
sourcing returns ``SourcingOutcome(None, False, 0.0)`` so the caller can skip cleanly. All
synthetic personas are provenance-marked (``enrichment_source="sourcing:<provider>"``) and,
offline, never clear the send bar — so they cannot leak into real outreach.
"""
from __future__ import annotations

from dataclasses import dataclass

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact


@dataclass(slots=True)
class SourcingOutcome:
    contact: Contact | None
    sourced: bool            # True if we created a person or filled a missing email
    email_confidence: float


class ContactSourcingService:
    def __init__(self, *, registry=None, enricher=None):
        self._registry = registry
        self._enricher = enricher

    @property
    def registry(self):
        if self._registry is None:
            from nexus.integrations.registry import get_registry

            self._registry = get_registry()
        return self._registry

    @property
    def enricher(self):
        if self._enricher is None:
            from nexus.enrichment.waterfall import get_enricher

            self._enricher = get_enricher()
        return self._enricher

    async def ensure_contact(
        self, ts: TenantSession, account: Account, *, icp: dict
    ) -> SourcingOutcome:
        # Explicit query — never touch the lazy ``account.contacts`` relationship under async.
        contacts = await ts.list(Contact, Contact.account_id == account.id)
        existing = self._best_existing(contacts)

        sourced = False
        contact = existing
        if contact is None:
            cands = await self.registry.contact_search(account, icp)
            if not cands:
                return SourcingOutcome(None, False, 0.0)
            cand = cands[0]
            contact = Contact(
                tenant_id=ts.tenant_id,
                account_id=account.id,
                full_name=cand.full_name,
                title=cand.title,
                seniority=cand.seniority,
                email=cand.email,
                enrichment_source=f"sourcing:{cand.source}",
            )
            ts.add(contact)
            await ts.flush()
            sourced = True

        if not contact.email:
            await self.enricher.enrich_contact(ts, contact, account)
            sourced = sourced or bool(contact.email)

        return SourcingOutcome(contact, sourced, contact.email_confidence)

    @staticmethod
    def _best_existing(contacts: list[Contact]) -> Contact | None:
        if not contacts:
            return None
        with_email = [c for c in contacts if c.email]
        if with_email:
            return max(with_email, key=lambda c: c.email_confidence)
        return contacts[0]


_service: ContactSourcingService | None = None


def get_contact_sourcing_service() -> ContactSourcingService:
    global _service
    if _service is None:
        _service = ContactSourcingService()
    return _service


def set_contact_sourcing_service(svc: ContactSourcingService | None) -> None:
    global _service
    _service = svc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_contact_sourcing.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add nexus/campaigns/sourcing.py tests/test_contact_sourcing.py
git commit -m "feat(campaigns): add ContactSourcingService (search + verifying enrich)"
```

---

## Task 8: Campaign model skip reasons + send_risky + migration + schemas

**Files:**
- Modify: `nexus/models/campaign.py`
- Create: `migrations/versions/0006_contact_sourcing.py`
- Modify: `nexus/campaigns/schemas.py`
- Test: `tests/test_campaign_sourcing.py` (created here, grows in Task 9/10)

- [ ] **Step 1: Write the failing test**

Create `tests/test_campaign_sourcing.py`:

```python
"""Campaign contact-sourcing: model fields, schemas, draft retry, send policy. Offline."""
from __future__ import annotations

from nexus.models.campaign import (
    SKIP_RISKY,
    SKIP_UNVERIFIED,
    Campaign,
)
from nexus.campaigns.schemas import CampaignIn, CampaignOut
from tests.conftest import make_tenant, tenant_session


def test_new_skip_reason_constants():
    assert SKIP_UNVERIFIED == "unverified_contact"
    assert SKIP_RISKY == "risky_address"


async def test_campaign_send_risky_defaults_false():
    async with tenant_session(await make_tenant()) as ts:
        c = Campaign(tenant_id=ts.tenant_id, name="C", list_id="l1")
        ts.add(c)
        await ts.flush()
        assert c.send_risky is False


def test_schema_in_accepts_send_risky_default_false():
    body = CampaignIn(name="C", list_id="l1")
    assert body.send_risky is False
    assert CampaignIn(name="C", list_id="l1", send_risky=True).send_risky is True


async def test_schema_out_exposes_send_risky():
    async with tenant_session(await make_tenant()) as ts:
        c = Campaign(tenant_id=ts.tenant_id, name="C", list_id="l1", send_risky=True)
        ts.add(c)
        await ts.flush()
        out = CampaignOut.from_model(c)
    assert out.send_risky is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_campaign_sourcing.py -v`
Expected: FAIL with `ImportError: cannot import name 'SKIP_UNVERIFIED'`.

- [ ] **Step 3: Add model constants + column**

In `nexus/models/campaign.py`, update the import line to include `Boolean`:

```python
from sqlalchemy import Boolean, ForeignKey, Index, JSON, String, Text
```

Add the two skip reasons after `SKIP_RESEARCH_FAILED`:

```python
SKIP_RESEARCH_FAILED = "research_failed"
SKIP_UNVERIFIED = "unverified_contact"   # sourced address below the send-confidence bar
SKIP_RISKY = "risky_address"             # risky verdict, campaign did not opt into send_risky
```

Add the column to `Campaign` (after `report`):

```python
    # Per-campaign opt-in: send to addresses graded "risky" by the verifier (held by default).
    send_risky: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
```

- [ ] **Step 4: Create the migration**

Create `migrations/versions/0006_contact_sourcing.py`:

```python
"""Contact sourcing: campaigns.send_risky opt-in column.

Revision ID: 0006_contact_sourcing
Revises: 0005_campaigns
Create Date: 2026-06-08
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_contact_sourcing"
down_revision = "0005_campaigns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("campaigns") as batch:
        batch.add_column(
            sa.Column(
                "send_risky", sa.Boolean(), nullable=False, server_default=sa.text("0")
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_column("send_risky")
```

- [ ] **Step 5: Add schema fields**

In `nexus/campaigns/schemas.py`, add `send_risky` to `CampaignIn`:

```python
class CampaignIn(BaseModel):
    name: str = Field(..., max_length=200)
    list_id: str
    icp: dict = Field(default_factory=dict)
    sequence: str = Field(default="ai-orchestrated-outbound", max_length=120)
    send_risky: bool = False
```

Add `send_risky` to `CampaignOut` (field + in `from_model`):

```python
class CampaignOut(BaseModel):
    id: str
    name: str
    list_id: str
    status: str
    sequence: str
    icp: dict = Field(default_factory=dict)
    report: dict = Field(default_factory=dict)
    send_risky: bool = False
    created_at: datetime

    @classmethod
    def from_model(cls, c: Campaign) -> "CampaignOut":
        return cls(
            id=c.id,
            name=c.name,
            list_id=c.list_id,
            status=c.status,
            sequence=c.sequence,
            icp=c.icp or {},
            report=c.report or {},
            send_risky=c.send_risky,
            created_at=c.created_at,
        )
```

- [ ] **Step 6: Thread send_risky through CampaignService.create**

In `nexus/campaigns/service.py`, add `send_risky` to the `create` signature and the `Campaign(...)` construction. Update the signature:

```python
    async def create(
        self,
        ts: TenantSession,
        *,
        name: str,
        list_id: str,
        icp: dict,
        sequence: str,
        created_by_user_id: str | None,
        send_risky: bool = False,
    ) -> Campaign:
```

And the construction (add `send_risky=send_risky,` after `sequence=...`):

```python
        campaign = Campaign(
            tenant_id=ts.tenant_id,
            name=name,
            list_id=list_id,
            icp=icp or {},
            sequence=sequence or "ai-orchestrated-outbound",
            send_risky=send_risky,
            created_by_user_id=created_by_user_id,
        )
```

In `nexus/api/routers/campaigns.py`, pass it through `create_campaign`:

```python
    campaign = await svc.create(
        ts,
        name=body.name,
        list_id=body.list_id,
        icp=body.icp,
        sequence=body.sequence,
        created_by_user_id=principal.user_id,
        send_risky=body.send_risky,
    )
```

- [ ] **Step 7: Run test to verify it passes**

Run: `python -m pytest tests/test_campaign_sourcing.py -v`
Expected: PASS (4 passed).

- [ ] **Step 8: Run existing campaign tests (no regression)**

Run: `python -m pytest tests/test_campaign_engine.py -v`
Expected: PASS (unchanged).

- [ ] **Step 9: Commit**

```bash
git add nexus/models/campaign.py migrations/versions/0006_contact_sourcing.py nexus/campaigns/schemas.py nexus/campaigns/service.py nexus/api/routers/campaigns.py tests/test_campaign_sourcing.py
git commit -m "feat(campaigns): add send_risky + sourcing skip reasons + migration 0006"
```

---

## Task 9: Draft-phase sourcing retry + pre-send policy

**Files:**
- Modify: `nexus/campaigns/service.py`
- Modify: `nexus/enrichment/waterfall.py` (wire the verifying finder into the default enricher)
- Test: `tests/test_campaign_sourcing.py` (append)

- [ ] **Step 1: Wire the verifying finder into the default enricher**

In `nexus/enrichment/waterfall.py`, import the new provider and add it to the default chain (before the blind pattern fallback). Update the import:

```python
from nexus.enrichment.providers import (
    EnrichmentProvider,
    EnrichmentResult,
    PatternEmailProvider,
    SearchEnrichmentProvider,
    VerifyingPatternEmailProvider,
)
```

Update `get_enricher`:

```python
        _enricher = WaterfallEnricher(
            providers=[
                SearchEnrichmentProvider(get_browser_provider()),
                VerifyingPatternEmailProvider(),
                PatternEmailProvider(),
            ]
        )
```

(The verifying finder lazily resolves `get_registry().verify_email`; offline that is the stub, so every guess is `unknown` and the 0.4 blind pattern still wins — unchanged offline behavior, real verdicts when Reacher is active.)

- [ ] **Step 2: Write the failing test**

Append to `tests/test_campaign_sourcing.py`:

```python
import pytest

from nexus.campaigns.service import CampaignService
from nexus.campaigns.sourcing import set_contact_sourcing_service, ContactSourcingService
from nexus.enrichment.providers import PatternEmailProvider
from nexus.enrichment.waterfall import WaterfallEnricher
from nexus.integrations.contact_search import StubContactSearchProvider
from nexus.integrations.registry import DataSourceRegistry
from nexus.models.account import Account, Contact
from nexus.models.campaign import (
    CAMP_AWAITING_APPROVAL,
    TARGET_DRAFTED,
    TARGET_SKIPPED,
)
from nexus.models.workflow import ListItem, ProspectList


async def _list_with_account(ts, *, with_contact=False):
    plist = ProspectList(tenant_id=ts.tenant_id, name="L")
    ts.add(plist)
    await ts.flush()
    acc = Account(tenant_id=ts.tenant_id, name="Acme", domain="acme.com")
    ts.add(acc)
    await ts.flush()
    if with_contact:
        ts.add(Contact(tenant_id=ts.tenant_id, account_id=acc.id, full_name="Jane Doe"))
    ts.add(ListItem(tenant_id=ts.tenant_id, list_id=plist.id, account_id=acc.id))
    await ts.flush()
    return plist.id


@pytest.fixture(autouse=True)
def _stub_sourcing():
    """Force the deterministic offline sourcing service (stub search + blind pattern)."""
    registry = DataSourceRegistry(contact_search=[StubContactSearchProvider()])
    enricher = WaterfallEnricher(providers=[PatternEmailProvider()])
    set_contact_sourcing_service(ContactSourcingService(registry=registry, enricher=enricher))
    yield
    set_contact_sourcing_service(None)


async def test_contactless_target_sources_drafts_then_holds_at_send(offline_services):
    async with tenant_session(await make_tenant()) as ts:
        list_id = await _list_with_account(ts, with_contact=False)
        svc = CampaignService()
        campaign = await svc.create(
            ts, name="C", list_id=list_id, icp={"buyer_titles": ["VP Sales"]},
            sequence="seq", created_by_user_id=None,
        )
        await svc.run_draft_phase(ts, campaign)
        assert campaign.status == CAMP_AWAITING_APPROVAL
        targets = await svc.list_targets(ts, campaign.id)
        assert len(targets) == 1
        t = targets[0]
        # A persona was sourced; the draft is grounded and marked sourced.
        assert t.status == TARGET_DRAFTED
        assert t.draft.get("sourced") is True
        assert t.draft.get("contact_id")

        # Send phase: offline the sourced 0.4 guess is unknown & below 0.5 -> held.
        await svc.approve_and_send(ts, campaign, decided_by=None)
        targets = await svc.list_targets(ts, campaign.id)
        assert targets[0].status == TARGET_SKIPPED
        assert targets[0].skip_reason == "unverified_contact"
        assert campaign.report["skips"].get("unverified_contact") == 1
```

(Add `from tests.conftest import make_tenant, tenant_session` at the top if not already imported, and ensure `offline_services` fixture is available from conftest.)

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_campaign_sourcing.py::test_contactless_target_sources_drafts_then_holds_at_send -v`
Expected: FAIL (target is SKIPPED `no_deliverable_contact`, not DRAFTED — sourcing not wired into `_draft_one` yet).

- [ ] **Step 4: Add the sourcing retry to `_draft_one`**

In `nexus/campaigns/service.py`, add imports at the top:

```python
from nexus.campaigns.sourcing import get_contact_sourcing_service
from nexus.core.config import get_settings
from nexus.models.account import Account
from nexus.models.campaign import (
    ...
    SKIP_NO_CONTACT,
    SKIP_RESEARCH_FAILED,
    SKIP_RISKY,
    SKIP_UNDELIVERABLE,
    SKIP_UNGROUNDED,
    SKIP_UNVERIFIED,
)
```

Replace `_draft_one` with the sourcing-aware version:

```python
    async def _draft_one(self, ts, campaign, target, *, engine, runtime) -> None:
        target.status = TARGET_DRAFTING
        await ts.flush()
        try:
            run = await engine.create_run(
                ts,
                "research_compose",
                {"account_id": target.account_id},
                account_id=target.account_id,
            )
            await engine.execute_run(ts, run, runtime=runtime)
            target.run_id = run.id

            if run.status != RUN_COMPLETED:
                target.status = TARGET_SKIPPED
                target.skip_reason = SKIP_RESEARCH_FAILED
                target.error = run.error
                await ts.flush()
                return

            draft = dict((run.blackboard or {}).get("draft") or {})
            target.draft = draft
            reason = self._classify(draft)

            # Self-healing: a NO_CONTACT target can have a contact sourced + re-drafted once.
            if reason == SKIP_NO_CONTACT and get_settings().campaign_sourcing_enabled:
                draft = await self._source_and_redraft(
                    ts, campaign, target, engine=engine, runtime=runtime
                )
                if draft is not None:
                    target.draft = draft
                    reason = self._classify(draft)

            if reason is None:
                target.status = TARGET_DRAFTED
            else:
                target.status = TARGET_SKIPPED
                target.skip_reason = reason
            await ts.flush()
        except Exception as exc:  # isolation: one bad target never blocks the rest
            target.status = TARGET_FAILED
            target.error = f"{type(exc).__name__}: {exc}"
            await ts.flush()

    async def _source_and_redraft(self, ts, campaign, target, *, engine, runtime) -> dict | None:
        """Source a contact and re-run research_compose ONCE targeting it. Bounded; never loops.
        Returns the new draft (marked ``sourced``) or None if nothing usable was sourced."""
        account = await ts.get(Account, target.account_id)
        if account is None:
            return None
        outcome = await get_contact_sourcing_service().ensure_contact(
            ts, account, icp=campaign.icp or {}
        )
        if outcome.contact is None:
            return None
        run = await engine.create_run(
            ts,
            "research_compose",
            {"account_id": target.account_id, "contact_id": outcome.contact.id},
            account_id=target.account_id,
        )
        await engine.execute_run(ts, run, runtime=runtime)
        target.run_id = run.id
        if run.status != RUN_COMPLETED:
            return None
        draft = dict((run.blackboard or {}).get("draft") or {})
        draft["sourced"] = True
        return draft
```

- [ ] **Step 5: Replace `_send_one` classification with the explicit pre-send policy**

In `nexus/campaigns/service.py`, replace `_send_one`:

```python
    async def _send_one(self, ts, campaign, target, *, tool, runtime) -> None:
        # Pre-send deliverability policy on the snapshot draft. Decide BEFORE invoking the
        # send tool so sourced/risky/unverified addresses become reportable skips, never
        # silent sends. The universal SendMessageTool gates still fire underneath.
        draft = dict(target.draft or {})
        skip = self._send_policy(draft, campaign)
        if skip is not None:
            target.status = TARGET_SKIPPED
            target.skip_reason = skip
            await ts.flush()
            return

        target.status = TARGET_APPROVED
        await ts.flush()
        run = await ts.get(OrchestrationRun, target.run_id) if target.run_id else None
        if run is None:
            target.status = TARGET_FAILED
            target.error = "missing draft run for send"
            await ts.flush()
            return
        run.blackboard = dict(run.blackboard or {})
        run.blackboard["draft"] = dict(target.draft or {})
        tc = ToolContext(
            ts=ts, runtime=runtime, run=run, inputs={"sequence": campaign.sequence}
        )
        try:
            await tool.run(tc)
            target.status = TARGET_SENT
            await ts.flush()
        except ToolError as exc:
            msg = str(exc).lower()
            target.status = TARGET_SKIPPED
            if "ungrounded" in msg:
                target.skip_reason = SKIP_UNGROUNDED
            elif "undeliverable" in msg or "invalid" in msg:
                target.skip_reason = SKIP_UNDELIVERABLE
            else:
                target.skip_reason = SKIP_NO_CONTACT
            target.error = str(exc)
            await ts.flush()
        except Exception as exc:  # isolation: one bad send never blocks the rest
            target.status = TARGET_FAILED
            target.error = f"{type(exc).__name__}: {exc}"
            await ts.flush()

    @staticmethod
    def _send_policy(draft: dict, campaign) -> str | None:
        """Return a skip reason to hold this draft from sending, or None to proceed.

        valid            -> send
        invalid          -> SKIP_UNDELIVERABLE (also hard-blocked by SendMessageTool)
        risky            -> send iff campaign.send_risky else SKIP_RISKY
        unknown & sourced & confidence < bar -> SKIP_UNVERIFIED
        unknown & real (non-sourced) contact -> send (no regression vs. today)
        """
        status = draft.get("email_status")
        if status == STATUS_INVALID:
            return SKIP_UNDELIVERABLE
        if status == STATUS_RISKY:
            return None if campaign.send_risky else SKIP_RISKY
        if status == STATUS_UNKNOWN or status is None:
            if draft.get("sourced"):
                bar = get_settings().campaign_sourced_min_send_confidence
                if (draft.get("email_confidence") or 0.0) < bar:
                    return SKIP_UNVERIFIED
        return None
```

Add the needed imports to `nexus/campaigns/service.py` verification constants:

```python
from nexus.verification import STATUS_INVALID, STATUS_RISKY, STATUS_UNKNOWN
```

(Replace the existing `from nexus.verification import STATUS_INVALID` line.)

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_campaign_sourcing.py -v`
Expected: PASS (all tests).

- [ ] **Step 7: Run existing campaign tests (no regression)**

Run: `python -m pytest tests/test_campaign_engine.py tests/test_grounded_send.py -v`
Expected: PASS (unchanged).

- [ ] **Step 8: Commit**

```bash
git add nexus/campaigns/service.py nexus/enrichment/waterfall.py tests/test_campaign_sourcing.py
git commit -m "feat(campaigns): self-healing sourcing retry + explicit pre-send policy"
```

---

## Task 10: send_risky path, multi-tenant isolation, full suite green

**Files:**
- Test: `tests/test_campaign_sourcing.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_campaign_sourcing.py`:

```python
async def test_send_risky_campaign_sends_risky_draft(offline_services):
    svc = CampaignService()
    # A draft graded risky; send_risky=True should let it through the policy.
    risky = {"email_status": "risky", "email_confidence": 0.4, "sourced": True,
             "grounded": True, "contact_id": "c1", "subject": "Hi", "body": "x"}

    class _C:
        send_risky = True

    assert svc._send_policy(risky, _C()) is None

    class _D:
        send_risky = False

    assert svc._send_policy(risky, _D()) == "risky_address"


def test_send_policy_invalid_always_held():
    svc = CampaignService()

    class _C:
        send_risky = True

    assert svc._send_policy({"email_status": "invalid"}, _C()) == "undeliverable_address"


def test_send_policy_real_unknown_contact_sends():
    svc = CampaignService()

    class _C:
        send_risky = False

    # Not sourced (a real existing contact) + unknown -> send, no regression.
    assert svc._send_policy(
        {"email_status": "unknown", "sourced": False}, _C()
    ) is None


async def test_sourced_contacts_never_cross_tenants(offline_services):
    tid_a = await make_tenant()
    tid_b = await make_tenant()
    async with tenant_session(tid_a) as ts:
        list_id = await _list_with_account(ts, with_contact=False)
        svc = CampaignService()
        campaign = await svc.create(
            ts, name="A", list_id=list_id, icp={}, sequence="seq",
            created_by_user_id=None,
        )
        await svc.run_draft_phase(ts, campaign)
        a_contacts = await ts.list(Contact)
    # Tenant B sees none of tenant A's sourced personas.
    async with tenant_session(tid_b) as ts:
        b_contacts = await ts.list(Contact)
    assert len(a_contacts) == 1
    assert b_contacts == []
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest tests/test_campaign_sourcing.py -v`
Expected: PASS (all tests). If `_send_policy` signature differs, fix to match Task 9.

- [ ] **Step 3: Run the full suite — zero network, no regressions**

Run: `python -m pytest -q`
Expected: PASS — all prior tests (202) plus the new modules green, zero network.

- [ ] **Step 4: Verify the migration chain loads**

Run: `python -c "import importlib; m = importlib.import_module('migrations.versions.0006_contact_sourcing'); print(m.revision, m.down_revision)"`
Expected: prints `0006_contact_sourcing 0005_campaigns`.

(If the module path with a leading digit fails to import directly, instead assert via Alembic's script directory: `python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; s=ScriptDirectory.from_config(Config('alembic.ini')); print([r.revision for r in s.walk_revisions()][:2])"` and confirm `0006_contact_sourcing` is the head above `0005_campaigns`.)

- [ ] **Step 5: Commit**

```bash
git add tests/test_campaign_sourcing.py
git commit -m "test(campaigns): send_risky policy + multi-tenant isolation for sourcing"
```

---

## Self-Review (completed during planning)

**1. Spec coverage** — every §5 component maps to a task:
- §5.1 verdict model + STATUS_RISKY → Task 1. §5.2 Reacher adapter + build branch → Task 2.
- §5.10 settings → Tasks 2–3. §5.3 verifying finder + EnrichmentResult fields → Task 4.
- §5.4 contact_search module + registry → Task 5. §4/§5.6 contact_id threading + draft signals → Task 6.
- §5.5 ContactSourcingService → Task 7. §5.7/§5.8/§5.9 model + schemas + migration → Task 8.
- §5.6 draft retry + send policy → Task 9. §8 testing (Reacher map, finder, sourcing, campaign integration, multi-tenant, full suite) → Tasks 2,4,7,9,10.

**2. Placeholder scan** — no TBD/TODO; every code step shows complete code; every test step shows the assertions; every run step shows the exact command + expected result.

**3. Type consistency** — `EmailVerification(provider_type, signals)` defined in Task 1 and consumed identically in Tasks 2/4/6. `EnrichmentResult(email_status, provider_type)` defined Task 4, set by the finder and copied by the waterfall. `SourcingOutcome(contact, sourced, email_confidence)` defined Task 7, consumed in Task 9. `_send_policy(draft, campaign)` signature defined Task 9, tested Task 10. `ContactSearchProvider.search(account, icp, *, limit)` consistent across Task 5 (stub + registry) and Task 7 (consumed). `_research_compose_plan` reads `goal_input["contact_id"]` (Task 6) which `_source_and_redraft` supplies (Task 9).

**Offline guarantee** — defaults (`email_verify_provider="stub"`, `contact_search_sources="stub"`) keep the suite zero-network; the sourced 0.4 guess is held at send (`SKIP_UNVERIFIED`), exercising source→draft→preview→hold deterministically (Task 9/10).

