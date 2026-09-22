# Found emails must be correct — implementation plan

> For agentic workers: TDD per task (failing test, run, implement, run, commit). Spec:
> `docs/superpowers/specs/2026-09-22-email-deliverability-design.md`.

**Goal:** save only addresses a verifier stands behind, never keep invalid ones, guess on the
organisation's real email domain, and resolve Similar people's employer domain automatically.

| # | Task | Files | Pinned by |
|---|---|---|---|
| 1 | Save policy at the waterfall choke point; `email_check` carried on `EnrichmentResult`; strict only when a verifier is configured | `enrichment/providers.py`, `enrichment/waterfall.py`, `enrichment/policy.py` | `tests/test_email_save_policy.py` |
| 2 | Invalid never kept: re-verify and bulk verify clear it and remember it; the finder skips remembered addresses | `enrichment/reverify.py`, `enrichment/providers.py` | `tests/test_invalid_never_kept.py`, existing reverify tests |
| 3 | Search-scraped addresses: on the email domain, name-matched, not a role address | `enrichment/providers.py` | `tests/test_search_email_attribution.py` |
| 4 | Email-domain resolution (colleagues → published on site → redirect → website with MX), company format inference, published-match first, stale-cache retry | `enrichment/mail_domain.py`, `enrichment/waterfall.py`, `enrichment/providers.py` | `tests/test_mail_domain.py` |
| 5 | Company-domain resolver, `GET /accounts/company-domain`, server fallback in `from-lookalike`, fill a blank domain | `enrichment/company_domain.py`, `api/routers/accounts.py` | `tests/test_company_domain.py` |
| 6 | Add-similar-person form prefills the resolved domain | `frontend/src/components/AddSimilarPerson.tsx`, `lib/api.ts`, `lib/types.ts` | typecheck + build |
| 7 | Cleanup script, dry run by default | `scripts/clean_invalid_emails.py` | `tests/test_clean_invalid_emails.py` |
| 8 | CLAUDE.md section | `CLAUDE.md` | — |

Verification: every task's tests; the enrichment, reverify, sourcing and contacts suites after tasks 1–5;
frontend typecheck and build after task 6; ruff; the full suite before handing over.
