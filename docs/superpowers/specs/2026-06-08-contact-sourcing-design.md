# Contact Sourcing + Real Email Verification — Design Spec

**Sub-project B of the GTM capability program (improvement #4).** Consumes the Segment
Campaign Engine's `SKIP_NO_CONTACT` skip report so accounts that would otherwise be skipped
can have a deliverable contact *sourced* and re-enter the draft phase — backed by a real,
separately-hosted email verifier (Reacher / `check-if-email-exists`) and a verifying email
finder.

**Status:** approved design, ready for implementation planning.
**Date:** 2026-06-08
**Depends on:** sub-project A (Segment Campaign Engine), already merged to `master`.

---

## 1. Goal

When a campaign target is heading for `SKIP_NO_CONTACT` — because the account has *zero*
contacts, or its contact has *no email* — automatically **source a contact** (find/create a
person and a deliverable email), then re-draft that target instead of skipping it. Email
deliverability is graded by a **real** verifier hosted on a separate host/IP, with a
permutation-based email finder that scores candidate addresses by verification verdict.

Everything degrades cleanly offline: with no verifier configured, the system still sources,
drafts, and previews, but **holds** unverified/guessed addresses from sending. The offline
test path stays fully green with zero network.

## 2. Scope

**In scope**
- Both `SKIP_NO_CONTACT` sub-cases: (a) account has zero contacts → source a net-new person;
  (b) account has a contact with no email → find + verify their email.
- A real `ReacherEmailVerifier` (`/v0/check_email`) behind the existing
  `EmailVerificationProvider` abstraction, activated by `NEXUS_` settings.
- A verifying email finder (permutation + verify, catch-all aware) with validation scores.
- ESP provider-type detection (custom / gsuite / office365 / outlook / yahoo / disposable).
- Inline, config-gated auto-retry in the campaign draft phase.
- A per-campaign `send_risky` opt-in; risky addresses are drafted+previewed but held from
  sending unless the campaign owner opts in.

**Out of scope (YAGNI / later cycles)**
- CRM write-back of sourced contacts (improvement #5, its own cycle).
- A net-new *real* contact-data provider (Apollo/InfoJoy/ZoomInfo adapters): we ship the
  registry capability + an offline stub; real adapters slot in later behind the same seam.
- Any frontend work (Campaigns UI is a deferred follow-on).

## 3. Background: where `SKIP_NO_CONTACT` comes from

`CampaignService._classify(draft)` (in `nexus/campaigns/service.py`) returns
`SKIP_NO_CONTACT` when:

```python
if not draft.get("contact_id") or draft.get("email_status") is None:
    return SKIP_NO_CONTACT
```

- **No `contact_id`** — the messaging agent picks `ctx.contacts[0] if ctx.contacts else None`;
  an account with zero contacts yields `contact_id=None`.
- **`email_status is None`** — `ComposeMessageTool` only verifies when the chosen contact has
  an email, so an emailless contact leaves `email_status` unset.

Existing building blocks we reuse:
- `WaterfallEnricher.enrich_contact(ts, contact, account)` — fills email/phone on a *known*
  contact (offline: `PatternEmailProvider` guesses `first.last@domain` @ conf 0.4).
- `DataSourceRegistry` — the composition spine (budgets / circuit breakers / cache) in front
  of `company_search`, `search`, `research`, `verify_email`.
- `build_email_verifier(name)` already reserves the seam: *"everifier (separate-host SMTP
  probe) lands here later; fail safe to the stub for now."*

## 4. Architecture & data flow

```
draft_one → research_compose run → _classify(draft)
   └─ NO_CONTACT and sourcing enabled
        → ContactSourcingService.ensure_contact(account, icp)
             1. pick existing contact (prefer one with email) OR
                registry.contact_search(account, icp) → create Contact (provenance-marked)
             2. if no email → WaterfallEnricher.enrich_contact (verifying finder)
             3. return SourcingOutcome(contact, sourced, email_confidence)
        → got usable contact → re-run research_compose ONCE (targets that contact)
        → re-_classify → DRAFTED (draft.sourced=True) or skip
send_one → policy on draft.email_status (+ sourced, confidence, campaign.send_risky):
   valid → send | invalid → SKIP_UNDELIVERABLE | risky → send iff send_risky else SKIP_RISKY |
   unknown & sourced & conf<bar → SKIP_UNVERIFIED | unknown & real contact → send
```

The new `ContactSourcingService` owns no orchestration of its own — it composes the registry
(net-new search + verify) and the waterfall enricher (email finding). It is an independent,
directly-testable unit.

## 5. Components & files

### 5.1 `nexus/verification/provider.py` (modify)
- Add `STATUS_RISKY = "risky"`.
- Extend `EmailVerification` with two **optional, default-valued** fields so nothing
  downstream breaks: `provider_type: str | None = None`, `signals: dict = field(default_factory=dict)`.
  `signals` carries `is_catch_all`, `is_role_account`, `is_disposable`, `has_full_inbox`.
- `as_dict()` includes the new fields.
- `build_email_verifier`: add a `key == "reacher"` branch constructing `ReacherEmailVerifier`
  from settings; every other/unknown key still falls safe to the stub.
- Export `STATUS_RISKY` from `nexus/verification/__init__.py`.

### 5.2 `nexus/verification/reacher.py` (new)
`ReacherEmailVerifier(EmailVerificationProvider)`, `name = "reacher"`:
- `verify_one(email)` → POST `{"to_email": email}` to `self.url` via
  `httpx.AsyncClient(timeout=self.timeout)`; parse JSON; map per the table below. Never raises
  across the boundary: any exception / non-200 → fail-safe `EmailVerification(status=unknown,
  confidence=0.0, source="reacher")`.
- `verify_bulk` keeps the default fan-out (one-by-one) for v1.

Reacher `is_reachable` → our verdict:

| `is_reachable` | `status` | confidence | sendable by default |
|---|---|---|---|
| `safe` | `valid` | 0.95 | yes |
| `invalid` | `invalid` | 0.95 | never (hard gate) |
| `risky` | `risky` | 0.40 | no (held; opt-in via `send_risky`) |
| `unknown` | `unknown` | 0.20 | only if non-sourced existing contact |

Provider-type classification from `mx.records` host(s) (lowercased, first match wins):
`google`/`googlemail`/`l.google.com` → `gsuite`; `outlook`/`office365`/`protection.outlook.com`
→ `office365`; `mail.protection.outlook` (on-prem hybrid) → `outlook`; `yahoodns`/`yahoo` →
`yahoo`; `misc.is_disposable` true → `disposable`; otherwise → `custom`. `signals` is copied
from the `smtp`/`misc` blocks.

### 5.3 `nexus/enrichment/providers.py` (modify)
Add `VerifyingPatternEmailProvider(EnrichmentProvider)`, `name = "pattern_verified"`,
constructed with an injected async `verify(email) -> EmailVerification` callable (defaults to
`registry.verify_email`):
1. Build bounded permutations from `contact.full_name` + `account.domain`:
   `first.last`, `firstlast`, `flast`, `first`, `f.last` `@domain` (deduped, capped at
   `email_finder_max_candidates`).
2. Verify candidates in order; **stop early on the first `valid`** and return it at high
   confidence (verdict confidence).
3. **Catch-all aware:** if the first probe's `signals.is_catch_all` is true, stop permuting and
   return the canonical `first.last` guess as an `EnrichmentResult` flagged risky at moderate
   confidence (0.5) — a catch-all domain makes every guess look deliverable, so don't probe
   further.
4. If no `valid` found, return the best non-invalid candidate (risky > unknown) at its verdict
   confidence; if all invalid/none, return `EnrichmentResult()` (not found).
The existing `PatternEmailProvider` (blind guess, conf 0.4) remains as the offline fallback in
the waterfall list **after** the verifying finder, so with no verifier configured the finder's
candidates all come back `unknown` and the chain still yields the 0.4 guess.

`EnrichmentResult` gains optional `email_status: str | None = None` and
`provider_type: str | None = None` so the verdict travels back to the caller.

### 5.4 `nexus/integrations/contact_search.py` (new) + registry (modify)
- `ContactCandidate` dataclass: `full_name`, `title`, `seniority`, `email | None`,
  `linkedin_url | None`, `source`, `confidence`, `provenance: dict`.
- `ContactSearchProvider` ABC: `async search(account, icp, *, limit) -> list[ContactCandidate]`.
- `StubContactSearchProvider` (offline default): returns **one** deterministic candidate per
  account — title from `icp["buyer_titles"][0]` if present else `"Decision Maker"`; a
  deterministic display name derived from the account (e.g. `"<Account> Lead"`); `email=None`
  (the finder fills it); `source="stub"`. Deterministic → reproducible tests.
- `DataSourceRegistry.contact_search(account, icp, *, limit=3)` — same policy spine
  (budget/breaker/cache) as `company_search`; waterfalls the configured providers, dedupes by
  `(full_name, title)`. Cache key includes `account.id` + normalized icp.
- `build_registry_from_settings` wires `contact_search` from a new
  `contact_search_sources` setting (default `"stub"`; real adapters land later).

### 5.5 `nexus/campaigns/sourcing.py` (new)
```python
@dataclass(slots=True)
class SourcingOutcome:
    contact: Contact | None
    sourced: bool            # True if we created a person or filled a missing email
    email_confidence: float

class ContactSourcingService:
    async def ensure_contact(self, ts, account, *, icp) -> SourcingOutcome: ...
```
Logic:
1. `contacts = await ts.list(Contact, Contact.account_id == account.id)` (explicit query — never
   touch the lazy `account.contacts` relationship under async). `existing = best of contacts`
   (prefer one with an email; else any).
2. If none: `cands = registry.contact_search(account, icp)`; if empty → `SourcingOutcome(None,
   False, 0.0)`. Else create a `Contact(account_id=account.id, full_name, title, seniority,
   enrichment_source="sourcing:<provider>")`, flush. `sourced=True`.
3. `contact = existing or new`. If `not contact.email`: run `WaterfallEnricher.enrich_contact`;
   `sourced = sourced or bool(found email)`.
4. Return `SourcingOutcome(contact, sourced, contact.email_confidence)`.
Module-level singleton `get_contact_sourcing_service()` (mirrors the codebase pattern);
the enricher + registry are injected so tests can stub them.

### 5.6 `nexus/campaigns/service.py` (modify)
- `_draft_one`: after the first run + `_classify`, if reason is `SKIP_NO_CONTACT` **and**
  `get_settings().campaign_sourcing_enabled`:
  - `outcome = await sourcing.ensure_contact(ts, account, icp=campaign.icp)`.
  - If `outcome.contact` and (created or now-emailed): re-run `research_compose` **once** with
    the sourced `contact_id` threaded into the run goal_input, snapshot the new draft, mark
    `draft["sourced"] = True`, and re-`_classify`. A bounded one-shot — never loops.
  - If still unusable → skip as today.
- `_send_one`: replace the post-`ToolError` classification with an explicit pre-send policy on
  `draft` (status + `sourced` + `email_confidence` + `campaign.send_risky` +
  `campaign_sourced_min_send_confidence`), per §4. The universal `SendMessageTool` gates remain
  untouched (invalid always blocked).
- `_build_report`: unchanged shape; the two new skip reasons flow through `skips{}` naturally.

### 5.7 `nexus/models/campaign.py` (modify)
- Add `SKIP_UNVERIFIED = "unverified_contact"`, `SKIP_RISKY = "risky_address"`.
- Add `Campaign.send_risky: Mapped[bool] = mapped_column(Boolean, default=False)`.

### 5.8 `nexus/campaigns/schemas.py` (modify)
- `CampaignIn.send_risky: bool = False`.
- `CampaignOut` exposes `send_risky`.
- `CampaignTargetOut.draft` already passes through the new draft keys (`sourced`,
  `provider_type`, `email_signals`) since it serializes the whole dict.

### 5.9 `migrations/versions/0006_contact_sourcing.py` (new)
Alembic migration: add `campaigns.send_risky BOOLEAN NOT NULL DEFAULT 0`. (Contacts/Accounts
need no new columns — provenance uses the existing `enrichment_source`.) Mirror the
offline/SQLite-safe batch pattern used by `0005_campaigns.py`.

### 5.10 `nexus/core/config.py` (modify) — new `NEXUS_` settings
| setting | default | purpose |
|---|---|---|
| `email_verify_provider` | `"stub"` (unchanged) | set `reacher` to activate |
| `email_verify_url` | `http://158.69.113.127:8080/v0/check_email` | Reacher endpoint (overridable) |
| `email_verify_timeout_s` | `20.0` | SMTP probes are slow |
| `email_finder_max_candidates` | `5` | permutation cap per contact |
| `contact_search_sources` | `"stub"` | ordered net-new contact providers |
| `campaign_sourcing_enabled` | `True` | inline auto-retry on/off |
| `campaign_sourced_min_send_confidence` | `0.5` | bar a sourced contact must clear to send |

Default provider stays `stub` so the suite is zero-network; activation is a one-line env
change. `email_verify_url` is an endpoint, not a secret — safe to default, overridable via
`.env`.

## 6. Error handling

- **Verifier**: never raises across the boundary (network/timeout/parse → `unknown`). The
  registry's circuit breaker + per-source budget already wrap `verify_email`, so a flaky
  Reacher host degrades to skips rather than hanging a campaign.
- **Finder / sourcing**: per-provider isolation already in `WaterfallEnricher` and the registry
  policy; `ensure_contact` returns `SourcingOutcome(None, …)` rather than raising.
- **Draft re-run**: wrapped by `_draft_one`'s existing `try/except` → `TARGET_FAILED` on
  unexpected error, never blocking sibling targets. The one-shot retry cannot loop.
- **Send policy**: a sourced address that can't clear the bar becomes a *skip* (reportable),
  never a silent send.

## 7. Offline / zero-network guarantee

With defaults (`email_verify_provider="stub"`, `contact_search_sources="stub"`):
- Net-new sourcing returns the deterministic stub persona; the finder's permutations all
  resolve to stub `unknown`, so the canonical 0.4 guess is used.
- The sourced draft is `DRAFTED` (grounded, has contact + email_status) and shows in preview,
  but at send time `unknown & sourced & 0.4 < 0.5` → `SKIP_UNVERIFIED`. The whole
  source→draft→preview→hold pipeline is exercised with zero network and deterministic counts.
- Reacher and any real contact provider only ever activate under explicit non-default env.

## 8. Testing strategy (all offline)

1. **Reacher mapping** — `ReacherEmailVerifier` against canned `/v0/check_email` JSON fixtures
   (safe/invalid/risky/unknown + gsuite/office365/custom MX) using a stubbed `httpx`
   transport (`httpx.MockTransport`); assert status, confidence, `provider_type`, `signals`.
   Also assert network failure → fail-safe `unknown`.
2. **Verifying finder** — permutation generation, early-stop on first `valid`, catch-all short
   circuit, and degrade-to-guess, all against a fake `verify` fn (no network).
3. **`ContactSourcingService.ensure_contact`** — (a) zero-contact account → creates a
   provenance-marked persona + guessed email; (b) emailless existing contact → enriched in
   place; (c) no candidate → `SourcingOutcome(None, False, 0.0)`.
4. **Campaign integration** — a contactless target sources → drafts → holds at send
   (`SKIP_UNVERIFIED`); a `send_risky=True` campaign with a risky draft sends; report counts
   reflect the new skip reasons.
5. **Multi-tenant isolation** — sourced contacts and campaign flags never cross tenants.
6. **Full suite green**, zero network, no regressions to the 202 existing tests.

## 9. Risks & mitigations

- **Bulk SMTP reputation** — the finder probes several permutations per contact. All probes go
  through the separately-hosted Reacher IP (the governing constraint), never the app host;
  early-stop-on-valid and the per-candidate cap bound probe volume; the registry budget caps
  total calls per run.
- **Catch-all domains** — handled explicitly (no permutation blasting; returned risky).
- **Synthetic offline personas** — clearly provenance-marked and never sent offline (held by
  the confidence bar), so they cannot leak into real outreach.
- **Skip-reason contract growth** — two additions to a documented fixed contract; the report
  shape is unchanged (reasons are dict keys), so existing consumers keep working.

## 10. How this serves the 6-improvement program

Contact Sourcing (#4) closes the biggest hole in sub-project A's skip report and lands the
real email-verification substrate that every later sub-project leans on: Channel & Cadence (C,
#6) sends only verified addresses, Continuous Automation (D, #2) schedules campaigns whose
targets are now self-healing, and the Live Dashboard (E, #3) surfaces deliverability /
provider-type signals already computed here.
