# Provider key management from the superadmin panel

**Status:** design, approved 2026-08-21
**Scope:** platform-wide provider credentials only. Not per-tenant, not money, not crypto roots.

## The problem, measured

Every external credential this product uses lives in an environment variable. Changing one means
editing `deploy/.env` and redeploying, and there is no way to see whether a key still works.

Two failures from 2026-08-20/21 are the argument for this feature, and both were invisible:

* **All five Groq keys returned 404.** Not a key problem — `llama-3.3-70b-versatile` had been
  withdrawn, and the account's catalog no longer contains any llama chat model. Every LLM call fell
  through `FallbackLLMProvider` to the stub, and the stub's output is emailed to real prospects. The
  garbled outreach reported earlier that week came from exactly this.
* **Both Apify accounts 403 `full-permission-actor-not-approved`, and the key that worked two weeks
  earlier now 401s.** Rotation could not help: approval is per Apify *account* and must be clicked
  in their console.

In both cases the deployment looked configured and produced nothing. That is the state this
feature removes.

## Locked decisions

| Decision | Choice | Why |
|---|---|---|
| Precedence | **DB layers over env; env is the floor** | A broken DB row can never brick a working deployment. With no rows, behaviour is byte-identical to today. |
| Key selection | **Superadmin pins a preferred key; it is used first** | Rotation is then a failure path, not the normal path. |
| Rotation | **On every error class that condemns the key** | Smooth transition to the next key without operator action. |
| Testing | **Two depths: probe (free, default) and verify (billable, opt-in)** | See below — the cheap probe would not have caught the Groq outage. |
| Scope | **Nine pooled provider keys** | groq, anthropic, openai_compat, exa, firecrawl, brave, serper, apify, github. |
| Dead keys | **Marked failed, never auto-deleted** | A transient 5xx and a revoked key look identical from one call. |

### Explicitly out of scope

* **Stripe** — money. A wrong value stops billing silently rather than erroring; it needs its own
  test and its own care.
* **CRM credentials** — per-tenant, and being built on another branch. Platform-wide and per-tenant
  are different axes; CLAUDE.md is explicit that conflating them is not a config change afterwards.
* **Telephony** — no implementation exists to hold a credential.
* **`secret_key`, `network_token_enc_key`, `mfa_secret_enc_key`** — cryptographic roots, not
  provider credentials. Changing one invalidates every sealed OAuth token, every MFA seed and every
  encrypted credential at once, with no way back. These must never be editable from a UI.

## Why two test depths

A cheap auth probe answers "does this credential authenticate". A full round-trip answers "does the
thing we actually do with it work". They are different questions and the Groq outage separated them
cleanly:

    GET /models        -> 200 for all five keys      (the credential is fine)
    POST /chat/completions -> 404 for all five keys  (the model is gone)

A panel that showed five green ticks while every draft came from the stub would be worse than no
panel. So:

* **probe** — cheapest call proving auth. Runs on save and on "Test all". Free.
* **verify** — through our own adapter (`GroqLLMProvider.complete`, `ExaSearchProvider.search`,
  `ApifyClient.run_actor`). Opt-in per key, one click, never swept, and the button states what it
  will spend.

`status` records which depth last succeeded. `probe_ok` and `verified` are distinct states, and a
key stuck at `probe_ok` while real calls fail is the Groq shape — the UI shows it as its own state,
not a green tick.

## Data model

One platform-global table, `provider_keys`. No `tenant_id`, so `scripts/apply_rls.py` leaves it
alone and everything reads through `get_platform_sessionmaker()` — the same rule as `companies`,
`people` and `source_databases`.

| Column | Notes |
|---|---|
| `id`, `provider`, `label` | `provider` is one of the nine; `label` is a human name |
| `key_encrypted` | Fernet-sealed. **Never** appears in a response model. |
| `key_hint` | Last 4 characters, so the UI can identify a row without the secret |
| `key_digest` | `sha256` of the key, so the same key cannot be added twice |
| `status` | `untested` / `probe_ok` / `verified` / `failed` |
| `last_tested_at`, `last_depth`, `last_error`, `last_error_status` | The provider's own error text, carried verbatim |
| `enabled` | Operator kill switch, separate from `status` |
| `preferred` | The pinned key. At most one per provider (partial unique index). |
| `created_at`, `updated_at`, `created_by_user_id` | |

**A request body never carries `status`.** Only the test functions set it — an admin who could set
`verified` by hand could mark a dead key working, which is the same rule `nexus/sources/service.py`
enforces for the source-database ladder.

**`prefer` implies `enable`, and disabling clears the pin.** Otherwise "pinned but disabled" is
expressible in the UI and the resolver has to silently ignore it.

## Resolution

```
key_pool(provider) -> [preferred, ...other enabled rows by created_at]   if any rows exist
                   -> settings.<provider>_api_key_list                    if none
```

**The real work is not the table.** `get_llm_provider()`, `build_engine()` and `get_apify_client()`
are memoized module singletons: they resolve once per process and never look again, so a key added
in the UI would not reach a running worker until it restarted. Each becomes a per-call pool read
with the constructed client cached — the same shape the in-flight CRM branch used, and for the same
reason.

## Rotation

Builds on the fix committed 2026-08-21 (`_KEY_REJECTED_STATUS`):

"Cover every type of error" does **not** mean "rotate on every error". The question each status
answers is *whose fault is it*, and rotating on a fault that is not the key's burns the whole pool
for nothing — which is exactly what would have happened to the five Groq keys.

| Condition | Action | Whose fault |
|---|---|---|
| 401 / 402 / 403 | **Rotate past, never retry.** | The key: revoked, forbidden, or out of credit. |
| 429 | Rotate, then back off after cycling the pool. Key stays in rotation. | The key, temporarily. |
| 5xx | Back off without rotating. | Upstream. |
| **400 / 404 / 422** | **Raise. Do not rotate.** | **Ours.** A malformed query, a withdrawn model, a bad payload — every key will fail identically, so trying them all just wastes the pool and hides the cause. 404 names `NEXUS_GROQ_MODEL` in its message, because that is the fix. |
| Anything else 4xx | Raise, do not rotate. | Unknown — and an unknown fault is not evidence against the key. |

The default for an unrecognised status is deliberately *not* to rotate. Rotation discards a working
credential's turn; doing that on a guess is how a single bad request takes down a nine-key pool.

**Runtime rejections write back.** When a key is condemned mid-crawl, its row is marked `failed`
with the provider's error. The panel then shows what actually happened in production, not just what
the last manual test said. This is what makes the feature operational rather than a form.

## Surface

* New permission **`providers.manage`**, in the `superadmin` preset only. Registering credentials
  and granting platform power are different acts — the same argument that keeps `sources.manage`
  separate.
* Endpoints under `/admin/provider-keys`: list, create, update label, delete, `prefer`, `test`
  (with `depth=probe|verify`), `test-all`. Every mutation audited via `record_admin_action`.
* A **Provider keys** tab in the existing Control plane, beside Rate cards / Plans / Subscriptions.

## Build order

Each step is independently shippable and the app keeps running on env vars throughout.

1. Migration + model + crypto + service + tests. Nothing reads it yet.
2. API endpoints + permission + audit.
3. Resolution refactor — the singleton change. Behaviour identical while the table is empty.
4. Runtime write-back on rotation.
5. UI tab.

## Open item

The CRM-credentials branch introduces `crm_crypto.py` and this introduces provider-key sealing.
When that branch lands, factor the Fernet envelope into one shared module rather than keeping three
copies (`network/crypto.py`, `core/crypto.py`, and these).
