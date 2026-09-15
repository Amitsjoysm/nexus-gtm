# Superadmin control plane: verifier health, missing settings, grouped panel

Date: 2026-09-15. Status: approved design, awaiting spec review.

## Why

Four reports from the product owner, each traced to code:

1. **Staging marks every found email `unknown` or `risky`.** Staging runs `email_verify_provider=reacher,dns`.
   When Reacher cannot be reached, the DNS fallback grades every address on a mail-receiving domain `risky` 0.5,
   and a stub grades everything `unknown`. Nothing says so: Platform health has no verifier row and startup logs
   nothing. The likely cause is `NEXUS_EMAIL_VERIFY_URL` pointing at a raw `:8080` port Azure cannot reach.
2. **The verifier URL cannot be changed from the Superadmin panel.** `email_verify_url` and
   `email_verify_timeout_s` are not in `runtime_config/catalog.py`, so fixing them means a redeploy.
3. **"Adding an IP" and "selecting Apify" do not work.** `RuntimeConfigTab.tsx` renders every non-bool setting
   without `options` as `<Input type="number">`. `admin_ip_allowlist` (`203.199.234.154, 122.170.197.80`) and
   `personalization_provider` (`apify`) are strings, so the browser refuses the text. The server is fine; the
   input type is wrong.
4. **Phone lookup has no setting at all.** `people/enrich.find_phone` always calls the Apify actor when a key
   exists. There is nothing to choose and nothing to turn off.

Also: 27 settings sit in seven alphabetical groups with no search, and many per-call tuning values on `Settings`
(batch sizes, intervals, caps) are only changeable by deploy.

## Decisions taken with the product owner (2026-09-15)

- Reacher has no token, only a URL we host. The panel gets the URL and timeout, not an auth header.
- Scope: verifier health + the panel fixes + audited extra settings.
- Phone lookup: a picker, Apify or Off, plus a health row.
- Design approved as presented.

## A. Verifier health

### A1. One check, shared by every caller

New `nexus/verification/health.py`:

```python
@dataclass(frozen=True, slots=True)
class VerifierCheck:
    status: str      # ok | degraded | unconfigured | error  (admin_health's vocabulary)
    detail: str
    provider: str    # the email_verify_provider value checked
    url: str         # "" when no Reacher key is configured

async def check_email_verifier(*, transport: httpx.AsyncBaseTransport | None = None) -> VerifierCheck
```

It reads the live `get_settings()`, so it checks what is in force, overrides included. It never raises.

- **No `reacher` key.**
  - With `dns` in the chain: `degraded`, "DNS only: domains are checked, no mailbox is ever confirmed, so no
    address can read valid".
  - Stub or blank: `unconfigured`, "stub: syntax check only, every address reads unknown".
  - No network either way.
- **`reacher` present.** POST `{}` (JSON) to `email_verify_url`, with the configured `Authorization` header if
  one is set. Timeout is `min(email_verify_timeout_s, 10)` with a 5s connect. An empty body means no address is
  sent, so no SMTP probe is spent from the verifier host. Results:

| Answer | Status | Detail says |
|---|---|---|
| 400 or 422 | ok | Reacher answered and rejected the empty request, as expected |
| 200 with a JSON body carrying `is_reachable` | ok | Reacher answered (a verdict for an empty address, no mailbox probed). Added during implementation: the live instance answers exactly this, measured 2026-09-15 |
| 200 without that shape | degraded | answered 200 without a Reacher verdict, so this does not look like Reacher's `/v0/check_email` |
| 401 or 403 | error | refused (HTTP n): the endpoint wants an Authorization header we do not send |
| 404 | error | not a Reacher endpoint (HTTP 404): check the path ends `/v0/check_email` |
| 405 | error | answers but not with POST (HTTP 405): a proxy is in front that does not forward to Reacher |
| 5xx | error | a proxy answered but Reacher behind it did not (HTTP n) |
| other code | error | unexpected HTTP n |
| connect error, timeout, any exception | error | unreachable (`ExceptionType`), followed by the consequence: with `dns` in the chain "every check falls back to DNS, which grades every address on a mail domain risky", otherwise "every check reads unknown" |

- A plain `http://` URL keeps its status. The detail gains "plain http: addresses cross the network in
  cleartext".
- The detail never contains the Authorization header value.

### A2. Platform health rows

Two entries are appended to `_PROBES` in `api/routers/admin_health.py`. The UI renders dependencies generically,
so it needs no change.

- **`email verifier`**: calls `check_email_verifier()`.
- **`phone lookup`**:
  - `phone_lookup_provider=off` → `unconfigured`, "turned off in Runtime settings; shared records and source
    databases still answer, nothing is bought".
  - Apify unkeyed → `unconfigured`, "no Apify key; lookups return not configured".
  - Otherwise GET the `phone_finder` actor's metadata with the first key:
    - non-200 → `error`;
    - `actorPermissionLevel == "FULL_PERMISSIONS"` → `degraded`, "phone_finder needs per-account approval in the
      Apify console";
    - otherwise `ok`.
  - That actor lookup becomes a small helper, shared with `_probe_apify`.

### A3. Startup warning

`health.warn_if_verifier_unreachable(logger)` runs the check and logs:
- `error` at WARNING with the prefix `EMAIL VERIFIER UNREACHABLE:`;
- `degraded` at WARNING;
- anything else at INFO.

It never raises.

- **API lifespan:** after the `apply_overrides` block, schedule it with `asyncio.create_task`. The task is kept in
  a module-level set so it is not garbage-collected. It is never awaited, so boot is never blocked or failed.
- **Worker `_main`:** today the worker applies overrides only on its first dispatched job. It now calls
  `refresh_if_stale(force=True)` right after `init_db()`, then schedules the same warning. Without that, it would
  check the environment URL while an override points somewhere else. `refresh_if_stale` never raises.

### A4. "Check connection"

- **Endpoint:** `POST /admin/runtime/email-verifier/check`, gated on `PRICING_WRITE` like the rest of that
  router. It returns `{status, detail, provider, url}`.
  - It is POST so the health console's route inventory never calls it on its own.
  - It is read-only, so there is no audit row.
- **UI:** the Email group header gets a "Check connection" button. The result shows inline as a status badge plus
  the detail text, and stays visible until the next check or a change to a verifier setting.

## B. Settings

### B1. Inputs follow the kind

`SettingRow` renders:
- `bool` → the existing switch button.
- A setting with `options` → `Select`.
- `int`/`float` → `Input type="number"`, with `min`/`max` and step `1` for int, `any` for float.
- `str` without options → `Input type="text"`, with the setting's `placeholder`.

Nothing else changes: the reason prompt on high risk, the confirm before switching a high-risk bool on, and the
Reset control.

### B2. Readable option labels

- `SettingSpec` gains `option_labels: tuple[tuple[str, str], ...] = ()` and `placeholder: str = ""`.
- `current_values()` returns `option_labels` as an object and `placeholder` as a string.
- The UI shows the label, then the raw value, then `(default)` for an empty value.

### B3. Personalization picker

`personalization_provider` gets `options=("apify", "stub")`, labelled "Apify (LinkedIn profile)" and "Off".
- `build_personalization_provider` is unchanged.
- A stored row outside the options is skipped by `apply_overrides` (existing behaviour, logged) and the
  environment value applies. The local database holds no runtime rows (checked 2026-09-15).

### B4. Verifier URL and timeout

- **`email_verify_url`**: `str`, risk high, placeholder `https://verifier.example.com/v0/check_email`.
  - Validator, run before the row is written:
    - the scheme is `http` or `https`;
    - a host is present;
    - `sources.safety._is_blocked_host(host, allow_private=settings.env in ("local", "test"))` passes, which
      refuses metadata names plus private, loopback, link-local and reserved addresses outside local/test;
    - the port is 1-65535.
  - The warning says a wrong URL turns every address unknown or risky, and to press Check connection after
    saving.
- **`email_verify_timeout_s`**: `float`, 2-60, risk medium.
- Both join `_ON_CHANGE` → `_reset_providers`, because `ReacherEmailVerifier` copies the URL and timeout at
  construction.
  - Every consumer resolves the verifier through `get_registry()` per call (`waterfall.py`, `providers.py`), or
    builds a fresh one (`reverify.fresh_verify`). Dropping the registry therefore reaches all of them.
- **Validators generalised:** `_EXTERNAL_VALIDATORS` becomes `_VALIDATORS` and covers `Settings`-backed keys too,
  still run before the row is written.
- **`email_verify_auth_header` joins `FORBIDDEN`.** It is a credential, and `current_values()` returns values in
  plaintext. It stays environment-only.

### B5. Phone lookup provider

- **Setting:** new `Settings.phone_lookup_provider: str = "apify"` (`NEXUS_PHONE_LOOKUP_PROVIDER`).
  - Catalog: `options=("apify", "off")`, labelled "Apify phone finder" and "Off", group "Contacts & enrichment",
    risk medium.
- **`find_phone`:** after step 2 (source database) and before step 3 (the actor), `off` returns
  `PhoneResult(status="disabled")`.
  - Step 4 already skips `unconfigured` and `no_identity`; it now also skips `disabled`. A turned-off lookup must
    never record a `not_found` into the shared person record, because a recorded miss is never re-purchased.
  - Step 5 meters only `found`/`not_found`, so `disabled` is not charged.
  - A shared-record or source-database answer still returns and is metered exactly as today. The customer
    received an answer.
  - Any value other than `off` behaves as `apify`, so a typo in the environment keeps today's behaviour.
- **Endpoint:** `enrich_contact_phone` maps `disabled` to 400: "Phone lookup is turned off in Runtime settings
  (phone_lookup_provider)."

### B6. Audited extra settings

Every one of these is read per call through the live `Settings` object, which each reader was checked for, so a
plain override takes effect. Each gets an `effect`, plus a `warning` when medium or high (existing test). Bounds
follow what each reader does with the value at the edges.

| Key | Group | Kind | Min | Max | Risk | Edge behaviour the bound respects |
|---|---|---|---|---|---|---|
| `email_finder_max_candidates` | Email | int | 1 | 20 | medium | each candidate is a verification call |
| `email_reverify_cooldown_days` | Email | int | 0 | 365 | low | 0 re-checks every time |
| `campaign_sourced_min_send_confidence` | Outreach | float | 0 | 1 | high | lower sends to less-proven sourced addresses |
| `personalization_max_posts` | Personalization | int | 1 | 10 | medium | also the actor's `maxPosts` (floored at 1 there) |
| `signal_dork_max_queries` | Signals | int | 0 | 10 | medium | each dork is a billed search; needs B7 |
| `tenant_daily_source_runs` | Signals | int | 0 | 100000 | medium | 0 means no cap (`_over_daily_budget`) |
| `inbox_min_signal_strength` | Signals | float | 0 | 1 | medium | lower puts weak mentions on the inbox |
| `inbox_realert_cooldown_days` | Signals | int | 0 | 90 | low | 0 disables the cool-down |
| `digest_interval_hours` | Signals | int | 1 | 168 | low | |
| `automation_tick_interval_s` | Automation | int | 15 | 3600 | medium | read each scheduler loop; reaches the scheduler via the worker's dispatch refresh |
| `account_refresh_interval_s` | Automation | int | 3600 | 604800 | high | hot-tier crawl frequency, the largest COGS line |
| `account_refresh_interval_cold_s` | Automation | int | 3600 | 2592000 | medium | |
| `account_hot_signal_window_days` | Automation | int | 1 | 365 | medium | wider makes more accounts hot |
| `account_refresh_batch_size` | Automation | int | 1 | 1000 | medium | |
| `icp_discovery_daily_count` | Automation | int | 1 | 100 | medium | 0 would search a pool of 0; turn discovery off instead |
| `icp_discovery_min_fit` | Automation | int | 0 | 100 | medium | |
| `icp_discovery_interval_hours` | Automation | int | 1 | 168 | medium | |
| `icp_discovery_enrich_max` | Automation | int | 0 | 200 | medium | 0 enriches none |
| `lookalike_enrich_max` | Contacts & enrichment | int | 0 | 50 | medium | 0 enriches none |
| `cadence_batch_size` | Outreach | int | 1 | 1000 | low | |
| `cadence_max_duration_days` | Outreach | int | 1 | 365 | medium | 0 would stop every enrollment at once |
| `crm_sync_batch_size` | Outreach | int | 1 | 1000 | low | |
| `billing_dunning_schedule_days` | Billing | str | | | medium | validator: 1-10 comma-separated whole days, each 1-60 |

"Email" is "Email finding & verification"; "Outreach" is "Outreach & CRM"; "Signals" is "Signals & alerts";
"Automation" is "Automation & schedules".

### B7. The dork cap is read when the source runs

`DorkedSearchSource` copies `max_queries` at construction, inside the process-wide `IngestionService`, so an
override of `signal_dork_max_queries` would save and change nothing. Resetting that singleton is not an option:
the suite injects its demo source through it.

- `max_queries` becomes `int | None = None`. `None` reads `get_settings().signal_dork_max_queries` at fetch time;
  an explicit value still wins.
- `timeout_s` becomes a property computed from the effective cap.
- `get_ingestion_service` stops passing the setting.

### B8. Withheld, with reasons (catalog docstring; `FORBIDDEN` where noted)

- **`email_verify_auth_header`**, `FORBIDDEN`: a credential, and the panel returns values in plaintext.
- **`billing_support_credit_cap`**, `FORBIDDEN`: it widens what another platform permission can grant, which is
  a permissions decision rather than a runtime setting.
- **`personalization_posts_window`**: passed verbatim to the Apify posts actor, whose accepted values nobody has
  observed.
- **`cadence_tick_interval_s`**: no reader in the tree.
- **`signal_sources`**: read once into the ingestion singleton the suite injects through; a rebuild hook is out
  of scope.

### B9. Guards against "saved, applied nothing"

New structural tests:

- **Reader coverage:** every catalog key that is not an external sink is read somewhere in `nexus/` outside
  `core/config.py` and `runtime_config/`, as `.key` or `getattr(..., "key"`.
- **Free text is validated:** a `str` setting without options has an entry in `_VALIDATORS`.
- **Groups:** every spec's group is in `GROUP_ORDER`.

### B10. Server-defined grouping

`catalog.GROUP_ORDER`:

1. Email finding & verification
2. Contacts & enrichment
3. Personalization
4. AI & research
5. Signals & alerts
6. Automation & schedules
7. Outreach & CRM
8. Billing
9. Access & security
10. Reliability

`current_values()` sorts by the group's position, then by declaration order in `_SPECS`, which is a deliberate
reading order rather than alphabetical.

Existing settings move to:

- **Email finding & verification**: `email_verify_provider`, `email_verify_url`, `email_verify_timeout_s`,
  `email_finder_max_candidates`, `email_reverify_cooldown_days`.
- **Contacts & enrichment**: `contact_search_sources`, `campaign_sourcing_enabled`, `account_enrich_enabled`,
  `account_enrich_min_interval_days`, `phone_lookup_provider`, `phone_enrich_auto`, `lookalike_enrich_max`.
- **Personalization**: `personalization_provider`, `personalization_posts_enabled`, `personalization_max_posts`.
- **AI & research**: `llm_provider`, `research_provider`.
- **Signals & alerts**:
  - `signal_search_provider`, `signal_sources_concurrent`, `shared_company_crawl_enabled`;
  - `signal_alerts_enabled`, `signal_alert_floor`, `signal_dork_max_queries`;
  - `tenant_daily_source_runs`, `inbox_min_signal_strength`, `inbox_realert_cooldown_days`,
    `digest_interval_hours`.
- **Automation & schedules**:
  - `automation_enabled`, `automation_tick_interval_s`;
  - `account_refresh_interval_s`, `account_refresh_interval_cold_s`, `account_hot_signal_window_days`,
    `account_refresh_batch_size`;
  - `icp_discovery_enabled`, `icp_discovery_daily_count`, `icp_discovery_min_fit`,
    `icp_discovery_interval_hours`, `icp_discovery_enrich_max`.
- **Outreach & CRM**: `cadence_enabled`, `cadence_batch_size`, `cadence_max_duration_days`,
  `campaign_sourced_min_send_confidence`, `crm_sync_enabled`, `crm_sync_batch_size`, `calling_enabled`.
- **Billing**: `billing_enforcement`, `billing_dunning_enabled`, `billing_dunning_schedule_days`.
- **Access & security**: `otp_registration_enabled`, `admin_ip_allowlist`.
- **Reliability**: `job_retry_enabled`, `idempotency_enabled`, `metrics_enabled`.

That is 53 settings: 27 existing, 3 new in B4 and B5, and 23 in B6.

## C. Panel UI

Built with the `impeccable` skill against `DESIGN.md` tokens and existing `components/ui` primitives. No new
dependencies.

- **Intro card:** the intro card is shortened to one paragraph.
- **Toolbar** above the groups:
  - a labelled search box matching label, key and effect;
  - a "Changed only" toggle (a button with `aria-pressed`) that keeps overridden settings;
  - a result count.
  - An empty result says "No setting matches “x”" with a Clear button.
- **Section index:**
  - Wide screens: a `nav` labelled "Settings sections", listing each group with its count and how many are
    changed. Each entry links to the group's card anchor.
  - Narrow screens: it becomes a horizontal scrolling row.
  - Groups with no match under the current filter are hidden from both the index and the page.
- **Group cards:** one per group, in server order.
  - The Email card carries Check connection (A4).
  - The Billing card ends with the existing Stripe webhook panel. It no longer sits alone at the top.
- **Rows:** B1 and B2 inputs. Everything else about a row is unchanged.
- **Accessibility:**
  - visible focus on every control;
  - keyboard reachable index links;
  - the search result count announced through `aria-live="polite"`;
  - no colour-only state, since every badge carries text.

## D. Tests

Backend, TDD, one file per concern:

- **`tests/test_verifier_health.py`**:
  - `httpx.MockTransport` for each row of the A1 table;
  - DNS-only and stub need no network;
  - the request body carries no `to_email`;
  - the auth header is sent when configured and never appears in the detail;
  - `check_email_verifier` never raises;
  - the startup warning logs `EMAIL VERIFIER UNREACHABLE` on error, and the lifespan scheduling never blocks.
- **`tests/test_admin_health.py`**:
  - the dependency names include `email verifier` and `phone lookup`;
  - phone lookup set to `off` reports `unconfigured` without a network call.
- **`tests/test_runtime_control_plane.py`**:
  - **URL validator:** refuses `ftp://`, a missing host, `169.254.169.254`, `metadata.google.internal`, and
    `127.0.0.1` under `env=prod`. It accepts a public IP literal. A refused value writes no row.
  - **Rebuilds:** setting the URL or the timeout rebuilds the registry's verifier.
  - **Pickers:** personalization options enforced and labels returned.
  - **Dunning:** the schedule validator works.
  - **Grouping and structure:** group order in `current_values`; reader coverage; free-text settings have a
    validator; every extra is present with its bounds.
  - **Check endpoint:** `PRICING_WRITE` only, and it returns the check.
- **`tests/test_phone_lookup_provider.py`**:
  - with `off`, the actor is never called, nothing is recorded, nothing is metered, and the status is
    `disabled`;
  - a shared-record hit still returns;
  - the endpoint returns 400 with the message.
- **`tests/test_signal_dork_cap.py`**: the cap is read at fetch; an explicit value wins; `timeout_s` follows.
- **`tests/test_runtime_config_ui.py`** reads the source, as the other UI tests do:
  - no `type="number"` input can render for a `str` setting;
  - search, the Changed-only toggle and the section index exist;
  - Check connection calls `api.checkEmailVerifier`.

Then:
- `npm run typecheck` and `npm run build`;
- the touched test files plus `test_runtime_config.py`, `test_runtime_provider_settings.py`,
  `test_admin_health.py`;
- a browser check of the panel against the local stack.

The full CI-equivalent suite runs before any push, as always.

## E. Docs

- **`CLAUDE.md` Runtime configuration:**
  - a free-text setting needs a validator;
  - the reader-coverage test;
  - grouping lives on the server;
  - the verifier check and startup warning;
  - `phone_lookup_provider`.
- **`docs/deployment/15-STAGING-PARITY.md` §2:** new section names; the verifier URL is now settable from the
  panel, followed by Check connection.

## Out of scope

- **`deps_ip.client_ip_of` trusting `X-Forwarded-For` from any peer:** reported separately. Changing a security
  guard needs its own approval.
- **Making Reacher reachable over HTTPS from Azure:** operator work. This change only makes the failure visible
  and fixable without a redeploy.
- **Runtime control of `signal_sources` and the verifier auth header.**

## Compatibility

- No migration.
- Every new catalog entry defaults to today's environment value, and `phone_lookup_provider` defaults to
  `apify`. Nothing changes until an operator changes a setting.
- The grouping and input changes are presentation only; the API keeps every existing field and adds
  `option_labels` and `placeholder`.
