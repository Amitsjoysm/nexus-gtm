# Prospector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Prospector (`/prospector`): reps search the InfoJoy B2B database of contacts and companies,
browse partially masked results for free, and pay per field delivered to save, list or export them.

**Architecture:** A new `nexus/prospector/` package of single-purpose modules (fields → filters → client →
records/identity/masking → cache → holdings/limits → search/saved → pricing/charges → deliver/export →
verify), a thin router, two MCP tools, a small enrollment gate in the engagement engine, and a React page
composed from the existing UI primitives. Spec: `docs/superpowers/specs/2026-10-16-prospect-engine-design.md`
(decisions D1–D25, referenced below as Dn).

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, Pydantic v2, httpx, Alembic, pytest (asyncio_mode=auto,
`-n auto --dist loadfile`), React 18 + TypeScript strict + Vite, CSS Modules over `tokens.css`.

**Rules for every task (from the owner and CLAUDE.md):**
- Work only on `feat/prospect-engine`. One commit per task. Never commit to, merge into, or push master; never tag;
  pushing the branch needs the owner's go-ahead.
- Test-first: write the failing test, run it red, implement, run it green, commit.
- `timezone.utc`, never `datetime.UTC` (Python 3.11 in CI, `requires-python >=3.10`).
- No PII in logs: log filter *names* and counts, never values.
- Commit message trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

**How this plan treats code.** The owner asked for the build to proceed straight after the plan, so tasks
give the exact files, the public interfaces (signatures and types), the test cases with their assertions,
the commands, and the commit, and the implementation follows them directly. Private helpers are left to
the implementer within those interfaces.

---

## File map

Backend (new):
| File | Responsibility |
|---|---|
| `nexus/prospector/__init__.py` | Package docstring only. |
| `nexus/prospector/errors.py` | `ProspectorError(status, detail, headers=None)`: the one exception the router maps to HTTP. |
| `nexus/prospector/fields.py` | Section/field catalog per view; scope availability; enum options. |
| `nexus/prospector/filters.py` | `SearchFilters`, `WorkspaceFilters` Pydantic models; `api_payload(view)`. |
| `nexus/prospector/records.py` | `VendorContact`, `VendorCompany`, `ParsedPage`; tolerant parsers. |
| `nexus/prospector/identity.py` | Identities, sha256 fingerprints, tenant-bound HMAC row keys. |
| `nexus/prospector/masking.py` | Partial masks (D7). |
| `nexus/prospector/client.py` | `InfoJoyClient.search(...) -> SearchOutcome`; never raises. |
| `nexus/prospector/cache.py` | Platform-global sealed page cache (D10); total. |
| `nexus/prospector/fetch.py` | `fetch_page(...)`: cache-through vendor fetch + COGS record; raises `ProspectorError`. |
| `nexus/prospector/holdings.py` | Batched "already held?" lookups and the paid ledger read. |
| `nexus/prospector/limits.py` | Per-user rate limit (D21) and daily row budget (D11, D22). |
| `nexus/prospector/cursor.py` | Signed Net new cursor encode/decode. |
| `nexus/prospector/presenter.py` | Vendor record + holdings → masked/saved row dicts. |
| `nexus/prospector/search.py` | Total and Net new pages. |
| `nexus/prospector/saved.py` | Saved tab over workspace data (D13, D14). |
| `nexus/prospector/selection.py` | `Selection` model and `resolve()`: row keys → vendor records. |
| `nexus/prospector/pricing.py` | `quote()`: fields to charge per row (D4–D6). |
| `nexus/prospector/charges.py` | `preflight_all()`, `meter_all()`, ledger writes. |
| `nexus/prospector/writers.py` | `upsert_account()`, `upsert_contact()` from vendor records. |
| `nexus/prospector/deliver.py` | Save / list / reveal / company-list in one savepoint. |
| `nexus/prospector/export.py` | CSV export (charged once, formula-safe). |
| `nexus/prospector/hold.py` | `held_until_verified(contact)` (D19). |
| `nexus/prospector/verify.py` | Verify selected contacts (D19, D20); inline ≤ 25, job above. |
| `nexus/prospector/schemas.py` | Request/response Pydantic models for the router. |
| `nexus/models/prospector.py` | `ProspectorPage`, `ProspectorPurchase`, `ProspectorDailyRows`. |
| `migrations/versions/0072_prospector.py` | The three tables. |
| `nexus/api/routers/prospector.py` | Thin router. |
| `scripts/infojoy_probe.py` | First real calls (D25), redacted output. |

Backend (modified): `nexus/providers/catalog.py`, `nexus/providers/testing.py`, `nexus/core/config.py`,
`nexus/runtime_config/catalog.py`, `nexus/billing/catalog.py`, `nexus/billing/rates.py`, `nexus/models/__init__.py`,
`nexus/api/routers/__init__.py`, `nexus/api/csv_export.py` (add `safe_cell`), `nexus/workers/tasks.py`
(prune + verify jobs), `nexus/workers/scheduler.py` (prune), `nexus/features/pages.py`,
`nexus/engagement/sequences/service.py` + `candidates.py` (hold gate), `nexus/mcp/gateway.py`,
`nexus/mcp/tools/catalog.py`, `CLAUDE.md`.

Frontend (new): `frontend/src/lib/prospectorTypes.ts`, `frontend/src/pages/prospector/{ProspectorPage,FilterRail,
FilterSection,ChipInput,OptionList,ResultsTable,BulkBar,ConfirmDeliveryDialog,Pager,ListPicker}.tsx` with CSS
modules, `frontend/src/pages/prospector/{useProspectorSearch,filterState}.ts`.
Frontend (modified): `frontend/src/lib/api.ts`, `frontend/src/app/nav.tsx`, `frontend/src/App.tsx`,
`frontend/src/components/ui/icons.tsx`.

Tests (new): `tests/prospector_fixtures.py`, `tests/test_prospector_{catalog,filters,records,masking,client,cache,
limits,search,saved,deliver,export,verify,campaign_hold,ui}.py`, `tests_integration/test_prospector_rls.py`;
additions to `tests/test_mcp_tools.py`, `tests/test_rate_card_conformance.py` (`OBSERVE_ONLY`).

---

### Task 1: Provider key, settings and the probe script (deployable on its own, no migration)

**Files:** Modify `nexus/providers/catalog.py`, `nexus/providers/testing.py`, `nexus/core/config.py`.
Create `scripts/infojoy_probe.py`, `tests/test_prospector_catalog.py`.

Interfaces:
```python
# nexus/core/config.py (Settings)
infojoy_search_api_key: str = ""
infojoy_search_api_keys: str = ""          # comma-separated rotation pool
infojoy_search_base_url: str = "https://infojoysearchapi-d5e7g0cydphvcme7.centralus-01.azurewebsites.net"
@property
def infojoy_search_api_key_list(self) -> list[str]: return self._key_pool(...)
# nexus/providers/catalog.py
"infojoy_search": ProviderSpec("infojoy_search", "InfoJoy Search (prospect database)",
                               "infojoy_search_api_key_list")
# nexus/providers/testing.py: probe POSTs a one-row contacts search with X-Api-Token;
# verify reuses the probe and upgrades to "verified", like the search providers.
```
Tests (`tests/test_prospector_catalog.py`):
- `test_infojoy_search_is_a_manageable_provider`: `"infojoy_search" in PROVIDERS`, `env_pool` reads the list.
- `test_probe_sends_the_token_as_a_header_never_in_the_url`: `httpx.MockTransport` captures the request;
  header `X-Api-Token == key`, key not in `str(request.url)`, body `PageSize == 1`, `Filters` present → `probe_ok`.
- `test_probe_reports_a_rejected_token`: 401 → `failed`, `http_status == 401`.

Probe script: argparse, reads keys through `providers.resolver.key_pool("infojoy_search")` inside the app
container; runs the calls listed in spec §11 with PageSize ≤ 10; prints field names, JSON types, distinct
values of enum-like fields, count field name, and which location filters changed results; every value of an
email/phone/name/url-like field replaced by `<redacted:len>` before printing. It never writes to disk.

- [ ] Write the tests; run `python -m pytest tests/test_prospector_catalog.py -n0 -q` → FAIL.
- [ ] Implement; run again → PASS; run `python -m pytest tests -k "provider" -q` → PASS.
- [ ] Commit `feat(prospector): InfoJoy Search provider key, settings and probe script`.

### Task 2: Local deploy + first real calls (runs whenever the owner has stored the token)

Not a code task. Gate for Task 6's parser and the location fields (D15, D25).
- [ ] `docker tag nexus-gtm:latest nexus-gtm:rollback-prospector-<date>`; `pg_dump -U nexus -d nexus -Fc` in
  `nexus-gtm-postgres-1` into `.claude/backups/`.
- [ ] `docker build -f deploy/Dockerfile -t nexus-gtm:latest <worktree>`; from the main repo
  `docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env up -d --no-build app worker`;
  `docker restart nexus-uiproxy`. Only after Task 1's commit (no migration in that image).
- [ ] Owner stores the token in Superadmin > Provider keys.
- [ ] `docker exec nexus-gtm-app-1 python -m scripts.infojoy_probe`; commit the redacted observation as
  `docs/superpowers/specs/2026-10-16-prospector-observed-api.md`; update `records.py` aliases, `fields.py`
  API names (person vs company location) and enum options to what was observed; rerun the suite.
- [ ] If the second location set cannot be proven, ask the owner for InfoJoy's field names.

### Task 3: Tables, migration, billing catalog and rate cards, runtime settings

**Files:** Create `nexus/models/prospector.py`, `migrations/versions/0072_prospector.py`. Modify
`nexus/models/__init__.py`, `nexus/billing/catalog.py`, `nexus/billing/rates.py`, `nexus/runtime_config/catalog.py`,
`nexus/core/config.py`, `tests/test_rate_card_conformance.py`. Extend `tests/test_prospector_catalog.py`.

Models:
```python
class ProspectorPage(TimestampMixin, Base):          # platform-global, no tenant_id
    __tablename__ = "prospector_pages"
    id: str (64, pk)        # sha256(kind|payload|page|size)
    kind: str (16); page_number: int; page_size: int
    payload_sealed: Text; row_count: int; total_count: int | None
    fetched_at, expires_at (indexed "ix_prospector_pages_expires")
class ProspectorPurchase(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "prospector_purchases"
    fingerprint: str (64); field: str (16)        # email | phone | company
    contact_id / account_id: str | None (32); request_id: str (64)
    first_charged_at, last_delivered_at
    UniqueConstraint("tenant_id", "fingerprint", "field", name="uq_prospector_purchase")
class ProspectorDailyRows(IdMixin, TimestampMixin, TenantScoped, Base):
    __tablename__ = "prospector_daily_rows"
    day: str (10, "YYYY-MM-DD" UTC); rows: int
    UniqueConstraint("tenant_id", "day", name="uq_prospector_daily_rows")
```
Migration `0072_prospector` (15 characters), `down_revision = "0071_ledger_erasures"`, literal DDL matching the
models (indexes and unique constraints by the same names), and a `downgrade`.

Billing: `module.prospector` (module, `enabled`); `prospector.email` (unit `email`), `prospector.phone` (unit
`phone`), `prospector.company` (unit `company`), all `metered`, `depends_on=["module.prospector"]`;
`prospector.search_rows` (unit `row`, default shadow, depends on the module), observe-only;
`prospector.daily_rows` (unit `row`, `enabled`, depends on the module): a limit holder whose entitlement
`quota` is the daily row budget. Rate cards: `_r("prospector.email", 1, 0.003, ...)`, phone and company the
same. `prospector.search_rows` gets a cost and no price: add it, and `prospector.daily_rows`, to the
conformance test's `OBSERVE_ONLY` set with the reason.

Runtime settings (group ACCESS): `prospector_rate_limit_per_minute` (int 1–600, default 30),
`prospector_net_new_scan_rows` (int 100–2000, default 500), `prospector_daily_rows_default`
(int 100–1,000,000, default 2,500). Each states `effect` and, where medium risk, a `warning`. Each is read in
`nexus/prospector/limits.py` or `search.py` (the "read outside config" guard).

Tests:
- `test_the_capabilities_and_cards_clear_the_floor`: each card `validate_rate` ≥ 0.5; each depends on `module.prospector`.
- `test_the_module_gate_changes_nothing_for_existing_plans`: no plan in `plans.py` lists `module.prospector`.
- `test_the_runtime_settings_state_their_effect`: three keys present, `effect` non-empty.
- `tests/test_migrations_replay.py` passes unchanged (it diffs replay vs `Base.metadata` and checks id length).

- [ ] Tests red → implement → green: `python -m pytest tests/test_prospector_catalog.py tests/test_rate_card_conformance.py tests/test_migrations_replay.py tests/test_runtime_control_plane.py -q`.
- [ ] Commit `feat(prospector): tables, migration 0072, capabilities, rate cards and runtime settings`.

### Task 4: Field catalog and filters

**Files:** Create `nexus/prospector/__init__.py`, `errors.py`, `fields.py`, `filters.py`, `tests/test_prospector_filters.py`.

Interfaces:
```python
# fields.py
VIEWS = ("contacts", "companies"); SCOPES = ("total", "net_new", "saved")
@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str; label: str; kind: Literal["text", "text_list", "enum_list", "bool"]
    api: dict[str, str]                 # view -> API filter name; "" = not available on that view's API
    options: tuple[str, ...] = ()       # observed vocabulary; empty = free text
@dataclass(frozen=True, slots=True)
class SectionSpec:
    key: str; label: str; views: tuple[str, ...]; fields: tuple[FieldSpec, ...]
    workspace_only: bool = False        # D14: Saved tab only
    saved_unsupported: tuple[str, ...] = ()   # field keys not stored on workspace records
SECTIONS: tuple[SectionSpec, ...]       # spec §8, in rail order
def sections_for(view: str) -> list[SectionSpec]
def field(key: str) -> FieldSpec
def catalog(view: str) -> dict          # served by GET /prospector/filters
# filters.py
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
TextList = Annotated[list[Text], Field(max_length=50)]
class SearchFilters(BaseModel):         # extra="forbid"; every API-backed field key from fields.py
    first_name: Text | None = None; last_name: Text | None = None
    titles: TextList = []; departments: TextList = []; seniorities: TextList = []
    company_names: TextList = []; company_domains: TextList = []
    person_countries/person_states/person_cities/person_zips: TextList = []
    company_countries/company_states/company_cities/company_zips: TextList = []
    employee_sizes: TextList = []; revenue_ranges: TextList = []
    industries, exclude_industries, sub_industries, exclude_sub_industries, sic_codes,
    keywords, exclude_keywords, technologies: TextList = []
    has_direct_phone: StrictBool | None = None; has_linkedin: StrictBool | None = None
    def active_keys(self) -> list[str]
    def api_payload(self, view: str) -> dict   # {ApiName: value}, empties dropped; raises ProspectorError(422)
                                               # for a field the view's API cannot take
class WorkspaceFilters(BaseModel):     # extra="forbid"
    email_statuses: list[Literal["valid","catch_all","risky","unknown","invalid","none"]] = Field([], max_length=6)
    list_ids: list[IdStr] = Field([], max_length=50)
    signal_kinds: list[SignalKindStr] = Field([], max_length=12)
    funding: StrictBool = False; job_postings: StrictBool = False
    signal_days: Literal[30, 90, 180] = 90
```
Tests:
- `test_empty_values_are_dropped_and_no_sort_is_ever_sent`: `api_payload` keys ⊆ the documented filter names; no key contains "sort".
- `test_a_list_longer_than_fifty_is_refused` / `test_a_value_longer_than_120_is_refused` / `test_unknown_fields_are_refused`.
- `test_has_direct_phone_must_be_a_real_boolean`: `"false"` (string) is refused.
- `test_a_companies_view_refuses_contact_only_fields`: `titles` on companies → `ProspectorError` 422.
- `test_the_catalog_marks_workspace_only_sections`: `catalog("contacts")` lists email_status/lists/signals/funding/job_postings with `workspace_only: true`; contacts has both location sections, companies has company location only.

- [ ] Red → implement → green (`python -m pytest tests/test_prospector_filters.py -q`). Commit `feat(prospector): filter catalog and strict filter models`.

### Task 5: Records, identity and masking

**Files:** Create `records.py`, `identity.py`, `masking.py`, `tests/prospector_fixtures.py`,
`tests/test_prospector_records.py`, `tests/test_prospector_masking.py`.

Interfaces:
```python
# records.py
@dataclass(frozen=True, slots=True)
class VendorContact:
    first_name: str; last_name: str; title: str; department: str; seniority: str
    email: str; direct_phone: str; company_name: str; company_url: str
    country: str; state: str = ""; city: str = ""; zip: str = ""
    @property full_name -> str
@dataclass(frozen=True, slots=True)
class VendorCompany:
    company_name: str; company_url: str; country: str; state: str; city: str
    industry: str; employee_size_range: str; linkedin_url: str
@dataclass(slots=True)
class ParsedPage: items: list[dict]; total: int | None
def parse_page(body: object) -> ParsedPage | None    # None = unparseable
def parse_contact(item: dict) -> VendorContact
def parse_company(item: dict) -> VendorCompany
# keys are matched case- and punctuation-insensitively ("CompanyURL" == "companyUrl" == "company_url")
# identity.py
def contact_identity(c: VendorContact) -> str   # "e:<email lower>" | "n:<first last>|<domain>" | ""
def company_identity(c: VendorCompany) -> str   # "d:<normalised domain>" | "n:<normalise_name>" | ""
def domain_of(url: str) -> str                  # companies.resolution.normalise_domain
def fingerprint(identity: str) -> str           # sha256 hex
def row_key(tenant_id: str, identity: str) -> str  # hmac-sha256 over a key derived from secret_key; 32 hex
# masking.py
def mask_email(value: str) -> str; def mask_phone(value: str) -> str
def mask_domain(value: str) -> str; def mask_linkedin(value: str) -> str
```
Tests:
- records: PascalCase and camelCase items parse identically; missing fields read `""`; `parse_page` takes the total from `TotalCount`/`totalCount`/`Total`; a non-dict body → `None`.
- identity: same email different case → same identity; no email → name+domain; row keys differ between tenants and never equal `sha256(email)`.
- masking (spec §5 table, parametrised): `john.smith@example.com` → `j•••@e•••.com`; `a@b.co` → `•••@•••.co`; `+1 (415) 555-7890` → `•••-•••-7890`; `12345` → `•••-•••-••••`; `example.com` → `e•••.com`; `https://linkedin.com/company/example-inc` → `linkedin.com/company/e•••`; the property test: for each fixture value, the mask contains no substring of length ≥ 3 of the local part, domain label, slug, or first 6 digits.

- [ ] Red → implement → green. Commit `feat(prospector): vendor records, identities and partial masks`.

### Task 6: The InfoJoy client

**Files:** Create `client.py`, `tests/test_prospector_client.py`.
```python
@dataclass(slots=True)
class SearchOutcome:
    status: Literal["ok","not_configured","invalid","rejected","busy","unavailable","bad_response"]
    page: ParsedPage | None = None; detail: str = ""; vendor_rows: int = 0
    @property
    def ok(self) -> bool
ENDPOINTS = {"contacts": "/api/search/contacts", "companies": "/api/search/companies"}
class InfoJoyClient:
    def __init__(self, *, keys: list[str] | None = None, base_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout: httpx.Timeout = httpx.Timeout(20.0, connect=5.0)) -> None
    async def search(self, view: str, filters: dict, *, page: int, size: int,
                     count: bool = False) -> SearchOutcome
```
Tests (MockTransport): body is exactly `{PageNumber, PageSize, IncludeTotalCount, Filters}`; `size > 500` → `invalid`
without sending; no keys → `not_configured` without sending; 401 on key 1 then 200 on key 2 → `ok` and
`record_rejection_from_response` called for key 1 (monkeypatched); all keys 401 → `rejected`; 429 everywhere → `busy`;
`{"Errors": ["PageSize cannot be greater than 500."]}` with 400 → `invalid` carrying that sentence; timeout →
`unavailable`; a 200 whose body is a list → `bad_response`; the raw key never appears in `detail`.

- [ ] Red → implement → green. Commit `feat(prospector): InfoJoy search client (total, rotating, never raises)`.

### Task 7: Sealed page cache, fetch-through, prune job

**Files:** Create `cache.py`, `fetch.py`, `tests/test_prospector_cache.py`; modify `nexus/workers/tasks.py`
(`handle_prune_prospector_pages`, `enqueue_prune_prospector_pages`, registry entry) and `nexus/workers/scheduler.py`
(enqueue beside the web-cache prune).
```python
# cache.py
TTL = timedelta(hours=24)
def page_key(view: str, payload: dict, page: int, size: int) -> str
async def get_page(key: str) -> ParsedPage | None
async def put_page(key: str, *, view: str, page: int, size: int, parsed: ParsedPage) -> None
async def prune() -> int
# fetch.py
@dataclass(slots=True)
class Fetched: parsed: ParsedPage; cached: bool
async def fetch_page(view: str, payload: dict, *, page: int, size: int, count: bool = False,
                     client: InfoJoyClient | None = None) -> Fetched   # raises ProspectorError
```
`fetch_page` maps outcomes per spec §10 (400/422/502/503/504). A cached page without a total and a `count=True`
request is refetched. Sealing uses `core.crypto.fernet_for` with a key derived from `secret_key` plus
`"|prospector-cache"`. Cache work runs before any tenant write (the `fetching/cache.py` ordering rule).

Tests: a stored page round-trips and the DB column holds no plaintext email; expired → miss; a failed outcome is
never stored (the next call asks the vendor again); an undecryptable row reads as a miss; `prune` deletes only
expired rows; `fetch_page` serves the second identical call from cache (transport called once).

- [ ] Red → implement → green. Commit `feat(prospector): sealed platform page cache and fetch-through`.

### Task 8: Holdings and limits

**Files:** Create `holdings.py`, `limits.py`, `tests/test_prospector_limits.py` (holdings tests live in
`test_prospector_search.py`).
```python
# holdings.py
@dataclass(slots=True)
class Held:
    kind: Literal["contact","account"]; record_id: str; account_id: str | None
    email: str | None = None; phone: str | None = None; name: str = ""; domain: str | None = None
async def held_contacts(ts, records: list[VendorContact]) -> dict[str, Held]    # identity -> Held; ≤ 2 queries
async def held_companies(ts, records: list[VendorCompany]) -> dict[str, Held]  # ≤ 2 queries
async def purchased(ts, fingerprints: list[str]) -> set[tuple[str, str]]       # {(fp, field)}
# limits.py
async def check_rate(tenant_id: str, user_id: str) -> None                     # ProspectorError 429 + Retry-After
async def daily_limit(ts) -> int        # entitlement quota on prospector.daily_rows, else the runtime default
async def rows_today(ts) -> int
async def ensure_budget(ts, rows: int) -> None                                 # ProspectorError 429
async def count_rows(ts, rows: int) -> None                                    # upsert today's row
```
Tests: the 31st search in a minute → 429 with `Retry-After`; a workspace at 2,490 rows asking for 25 → 429 with the
reset sentence; a plan entitlement quota of 10,000 on `prospector.daily_rows` raises the limit; `count_rows` adds
to the same row; the budget is enforced with `billing_enforcement="off"`.

- [ ] Red → implement → green. Commit `feat(prospector): workspace holdings, rate limit and daily row budget`.

### Task 9: Search (Total and Net new), schemas, router, the no-PII proof

**Files:** Create `cursor.py`, `presenter.py`, `search.py`, `schemas.py`, `nexus/api/routers/prospector.py`,
`tests/test_prospector_search.py`; modify `nexus/api/routers/__init__.py`.
```python
# schemas.py (response models declare NO raw PII field; spec §5)
class SavedRecordOut(BaseModel): kind; id; account_id | None; email | None; phone | None
class ProspectRowOut(BaseModel):
    row_key: str; api_page: int; api_page_size: int; saved: bool; record: SavedRecordOut | None = None
    first_name: str = ""; last_name: str = ""; title: str = ""; department: str = ""; seniority: str = ""
    company_name: str = ""; country: str = ""; state: str = ""; city: str = ""
    industry: str = ""; employee_size_range: str = ""
    email_masked: str = ""; phone_masked: str = ""; domain_masked: str = ""; linkedin_masked: str = ""
    has_email: bool = False; has_direct_phone: bool = False; has_linkedin: bool = False
class SearchIn(BaseModel):  # extra="forbid"
    view: Literal["contacts","companies"]; scope: Literal["total","net_new"] = "total"
    filters: SearchFilters = SearchFilters(); page: int = Field(1, ge=1, le=400)
    page_size: Literal[25, 50, 100] = 25; cursor: str | None = Field(None, max_length=600)
    include_count: StrictBool = False
class SearchOut(BaseModel):
    rows: list[ProspectRowOut]; page: int; page_size: int; has_more: bool; total: int | None
    next_cursor: str | None = None; scanned: int = 0; exhausted: bool = False; scan_capped: bool = False
# search.py
async def search(ts, *, user_id: str, body: SearchIn, client: InfoJoyClient | None = None) -> SearchOut
# cursor.py
def encode(tenant_id: str, data: dict) -> str; def decode(tenant_id: str, token: str) -> dict  # ProspectorError 400
```
Router (`require(Permission.manage_accounts)` everywhere; `ProspectorError` → `HTTPException(status, detail, headers)`):
`GET /prospector/filters?view=`, `POST /prospector/search`.

Tests (API, offline, a fake client injected through `search.set_client()` test seam):
- Total returns masked rows; `has_more` true when the page is full.
- **`test_no_unmasked_value_appears_in_any_search_response`**: for Total, Net new and `include_count`, the JSON text
  contains none of the fixture's emails, phones, domains, LinkedIn URLs, local parts, domain labels or slugs.
- A row whose email the workspace holds is `saved: true` with `record.email` = the workspace value; one query per kind (count statements with an `after_cursor_execute` listener).
- Net new hides held rows, fills the page from later API pages, stops at the scan cap with `scan_capped: true`, and `next_cursor` resumes after the last scanned row.
- A cursor from another tenant, or edited, → 400.
- Not configured → 400 with the Provider keys sentence; vendor 503 → 503; nothing cached.
- A rep may search (role check); `module.prospector` switched off → 402 with `switch_state`.
- The daily counter grew by the rows served; `prospector.search_rows` recorded only for vendor-fetched rows.

- [ ] Red → implement → green. Commit `feat(prospector): Total and Net new search with masked rows`.

### Task 10: Saved tab

**Files:** Create `saved.py`, `tests/test_prospector_saved.py`; add `POST /prospector/saved` to the router and
`SavedIn`/`SavedRowOut`/`SavedOut` to `schemas.py`.
```python
async def saved_page(ts, *, view: str, filters: SearchFilters, workspace: WorkspaceFilters,
                     page: int, page_size: int) -> SavedOut
```
Mapping per spec §7 (titles ILIKE any; seniority IN; company names ILIKE; domains IN normalised; company location
on `Account.country/region/custom_fields["city"]/postal_code`; person location on
`Contact.custom_fields["location"][...]`; employee/revenue ranges parsed "51-200", "10000+", "$1M-$10M";
industries ILIKE; tech stack via the quoted-text LIKE; has phone; email status IN (`none` = NULL/blank);
list membership; signal EXISTS within `signal_days` with `funding`→`kind="funding"`, `job_postings`→`kind="hiring"`).
Departments, SIC codes and Has LinkedIn on the Saved tab → 422 "not stored for workspace records".

Tests: each filter narrows a seeded book (5 accounts, 8 contacts, 2 lists, signals at 10 and 120 days); counts are
exact; pagination is stable; another tenant's records never appear; rows carry real values.

- [ ] Red → implement → green. Commit `feat(prospector): Saved tab over workspace records`.

### Task 11: Selection, quote, charges, writers, deliver

**Files:** Create `selection.py`, `pricing.py`, `charges.py`, `writers.py`, `deliver.py`, `hold.py`,
`tests/test_prospector_deliver.py`; add `POST /prospector/quote` and `POST /prospector/deliver`.
```python
# selection.py
class RowRef(BaseModel): api_page: int = Field(ge=1, le=400); api_page_size: int = Field(ge=1, le=500); row_key: str
class AllMatching(BaseModel): limit: int = Field(ge=1, le=1000)
class Selection(BaseModel):   # extra="forbid"; exactly one of rows / all_matching
    view; scope: Literal["total","net_new"]; filters: SearchFilters
    rows: list[RowRef] = Field([], max_length=1000); all_matching: AllMatching | None = None
@dataclass(slots=True)
class Resolved: records: list[tuple[str, VendorContact | VendorCompany]]; unavailable: int
async def resolve(ts, selection: Selection, *, client=None) -> Resolved
# pricing.py
CAPABILITY = {"email": "prospector.email", "phone": "prospector.phone", "company": "prospector.company"}
@dataclass(slots=True)
class Line: identity: str; fingerprint: str; record: VendorContact | VendorCompany; held: Held | None; fields: tuple[str, ...]
@dataclass(slots=True)
class Quote:
    lines: list[Line]; charges: dict[str, int]; already_paid: int; held: int; unavailable: int
    @property credits_by_field -> dict[str, int]
async def quote(ts, *, view: str, action: str, resolved: Resolved, include_phones: bool) -> Quote
# charges.py
async def preflight_all(ts, charges: dict[str, int]) -> None           # raise_if_blocked per capability
async def meter_all(ts, charges: dict[str, int], *, request_id: str, user_id: str) -> None
async def record_purchases(ts, lines: list[Line], *, request_id: str, links: dict[str, tuple[str|None, str|None]]) -> None
# writers.py
async def upsert_account(ts, *, name: str, domain: str, country: str = "", state: str = "", city: str = "",
                         industry: str = "", employee_size_range: str = "", linkedin_url: str = "",
                         owner_user_id: str | None) -> tuple[Account, bool]
async def upsert_contact(ts, account: Account, record: VendorContact, *, include_phone: bool) -> tuple[Contact, bool]
# deliver.py
ACTIONS = ("save", "list", "reveal", "company_list")
async def deliver(ts, *, user_id: str, selection: Selection, action: str, list_id: str | None,
                  include_phones: bool, request_id: str, client=None) -> DeliverOut
# hold.py
SOURCE = "sourcing:prospector"; RELEASED = frozenset({"valid", "catch_all", "risky"})
def held_until_verified(contact) -> bool
```
Rules (spec §9.1–9.2): held rows free and untouched; contacts view charges email (if present) and phone (only with
`include_phones`); companies view charges company; `company_list` free; the ledger removes already-paid fields;
`preflight_all` before writes; records + list items + ledger + meters inside `ts.session.begin_nested()`;
`list_id` must be a live list of the right kind (`lists.service`), `company_list` and companies-view `list` need an
account list, contacts-view `list` needs a contact list; `reveal` takes exactly one row; LinkedIn URLs through
`enrichment.linkedin.company_page_url`; contacts saved with `enrichment_source=hold.SOURCE`,
`custom_fields={"department":…, "location":{country,state,city,zip}}`.

Tests:
- Quote: 3 contacts (2 with email, 1 with phone) without phones → `{"prospector.email": 2}`; with phones → +1 phone; one already paid → excluded and counted; a held row → 0 and counted; companies view → `company` per row; `company_list` → 0.
- Deliver save: contacts + their accounts created once; a second identical request (same `request_id`) creates nothing and charges nothing; a new `request_id` for the same rows charges nothing (ledger).
- `billing_enforcement="on"` with a balance short of the quote → 402 and NOTHING written (no account, contact, list item, purchase, usage).
- `list` adds the created contacts to a contact list; an account list for contacts → 422; `company_list` adds companies free.
- Reveal: one row; response includes the record id; a subsequent search shows the row `saved` with real values.
- A non-company LinkedIn URL is dropped and reported; an existing account with the same normalised domain is reused.

- [ ] Red → implement → green. Commit `feat(prospector): quote and deliver (save, list, reveal, company list)`.

### Task 12: Export

**Files:** Create `export.py`; modify `nexus/api/csv_export.py` (add `safe_cell`); add `POST /prospector/export`;
create `tests/test_prospector_export.py`.
```python
def safe_cell(value: object) -> object   # csv_cell + prefix "'" when a str starts with = + - @ \t \r
async def export_rows(ts, *, user_id: str, selection: Selection, include_phones: bool,
                      request_id: str, client=None) -> tuple[str, list[str], list[list]]
```
Charged like deliver, ledger written, committed before the file is returned (`X-Row-Count` header, as the existing
exports). Held rows export the workspace's own values. Columns per spec §9.4.

Tests: columns and order; phones column only with `include_phones`; `=HYPERLINK(...)` company name exported as
`'=HYPERLINK(...)`; re-export with a new `request_id` charges 0; 402 writes nothing; existing exports unchanged
(their tests still pass).

- [ ] Red → implement → green. Commit `feat(prospector): charged, formula-safe CSV export`.

### Task 13: Campaign hold (engagement)

**Files:** Modify `nexus/engagement/sequences/service.py` (`enroll`), `nexus/engagement/sequences/candidates.py`
(`Candidate.held`), the candidates response schema and the campaign builder's candidate row in the frontend;
create `tests/test_prospector_campaign_hold.py`.
- `enroll`: after the `no_email` check, `if held_until_verified(contact): skipped(reason="unverified_prospector_email")`.
- `candidates`: `held=held_until_verified(c)`.
Tests: a `sourcing:prospector` contact with `email_status=None` is skipped with that reason; once `valid` it enrolls;
a CRM contact with `None` still enrolls (no change for existing contacts); candidates report `held`.

- [ ] Red → implement → green; run `python -m pytest tests -k "engagement or campaign" -q`. Commit `feat(engagement): hold Prospector emails out of campaigns until verified`.

### Task 14: Verify

**Files:** Create `verify.py`, `tests/test_prospector_verify.py`; add `POST /prospector/verify`; add the
`prospector_verify_contacts` job to `nexus/workers/tasks.py`.
```python
INLINE_MAX = 25
class VerifyIn(BaseModel): contact_ids: list[IdStr] = Field(min_length=1, max_length=1000)
async def verify_contacts(ts, *, user_id: str, contact_ids: list[str], verify=None) -> dict
    # {"mode": "inline", "results": [{contact_id, status, email_kept}], "charged": n}
    # or {"mode": "queued", "count": n}
async def handle_verify_job(payload: dict) -> dict
```
Free for `enrichment_source == hold.SOURCE` (D20); others `preflight` then one `verify.email` meter of their count.
Uses `reverify.fresh_verify()` and `apply_verdict()`; an `unknown` verdict keeps the address (D19); verifier calls
gathered with a semaphore of 5 and verdicts applied after the gather.

Tests: Prospector contacts verify free (no usage row); a CRM contact charges one `verify.email`; invalid removes the
address and remembers it; unknown keeps it; 26 ids → queued job with the ids; another tenant's id is ignored.

- [ ] Red → implement → green. Commit `feat(prospector): Verify, free for Prospector-delivered addresses`.

### Task 15: MCP tools

**Files:** Modify `nexus/mcp/gateway.py` (`("POST", "/api/prospector/search")`, `("POST", "/api/prospector/deliver")`),
`nexus/mcp/tools/catalog.py` (`prospector_search` scope read; `prospector_save` scope write,
`price_capability="prospector.email"`, `request_id`), `tests/test_mcp_tools.py`.
Tests: both tools listed for the right scopes only; the structural test passes (calls declared); a search call returns
masked rows (no fixture PII in the result text); a save call charges and returns counts and ids; descriptions name no
vendor.

- [ ] Red → implement → green. Commit `feat(mcp): Prospector search and save tools`.

### Task 16: Frontend

Invoke `impeccable` and `ui-ux-pro-max` first; follow `DESIGN.md`.

**Files:** create the `frontend/src/pages/prospector/` files and `frontend/src/lib/prospectorTypes.ts`; modify
`frontend/src/lib/api.ts` (`prospectorFilters`, `prospectorSearch`, `prospectorSaved`, `prospectorQuote`,
`prospectorDeliver`, `prospectorExport` via a private `downloadPost`, `prospectorVerify`), `app/nav.tsx` (after
Contacts: `{ to: "/prospector", label: "Prospector", icon: <CompassIcon />, capability: "module.prospector" }`),
`App.tsx` (route wrapped in `RequirePage page="/prospector"` + `RequireCapability capability="module.prospector"`),
`components/ui/icons.tsx` (`CompassIcon`), `nexus/features/pages.py` (`PageSpec("prospector", "/prospector",
"Prospector", "module.prospector")`).

Behaviour per spec §12: URL state; debounced (800 ms) auto-apply with `AbortController`; accordion sections with
`aria-expanded`/`aria-controls`, active counts, per-section and global clear; workspace-only sections disabled with
the reason off the Saved tab; masked cells with sr-only text; Saved badge + link; bulk bar with select-all (≤ 1,000);
confirm dialog with the quote, the phones toggle and the list picker; `WorkingIndicator` while delivering;
skeleton/empty/error/not-configured/budget states; `aria-live` result announcements.

Tests (`tests/test_prospector_ui.py`, source-reading like the repo's other UI tests): the route is guarded by
`module.prospector` and `RequirePage`; the nav item carries the capability; `ProspectRow` declares no `email`,
`phone`, `domain` or `linkedin` field (only `*_masked` and `has_*`); delivery uses `WorkingIndicator`; existing
structural tests (`test_page_visibility.py`, the route/nav capability test) pass.

- [ ] `npm --prefix frontend run typecheck` and `npm --prefix frontend run build` green.
- [ ] Verify in the browser preview (`preview_start`), light and dark, keyboard-only pass, 375px width.
- [ ] Commits: `feat(prospector-ui): API client and types`, `feat(prospector-ui): page, filters, table and bulk actions`, `feat(prospector-ui): nav, route and page visibility`.

### Task 17: RLS integration test and CLAUDE.md

**Files:** Create `tests_integration/test_prospector_rls.py` (the two tenant tables are enrolled by `apply_rls.py`
and isolate tenants; `prospector_pages` is readable by the app role); modify `CLAUDE.md` (a "Prospector" section:
decisions D1–D25 in brief, the masking guarantee, the cache and ledger, the hold gate, limits, migration head
`0072_prospector`).

- [ ] Commit `docs(prospector): CLAUDE.md section` and `test(prospector): RLS on the tenant tables`.

### Task 18: Full CI-equivalent on the exact commit (before any push)

- [ ] `git archive` HEAD → `python:3.11-slim`: `pip install -e ".[dev,migrate,metrics]"` (no constraints), `pytest -n 6
  --tmpfs /tmp -o timeout_method=signal` with coverage.
- [ ] Integration: `postgres:16-alpine` + `redis:7-alpine` on a Docker network, `.[dev,postgres,redis]`, `tests_integration/`.
- [ ] Frontend: `node:20` → `npm ci && npm run typecheck && npm run build`.
- [ ] gitleaks 8.24.3 over `f6baafc..HEAD`.
- [ ] Image build with `--build-arg SECURITY_REFRESH=<today>` + `aquasec/trivy:0.65.0` (fixable HIGH/CRITICAL,
  `.trivyignore.yaml`).
- [ ] Report results to the owner; push only on their go-ahead.

---

## Spec coverage check

| Spec section | Task |
|---|---|
| §2 D1 name, route, gate | 3, 16 |
| D2/D23 vendor cost, seed cards | 3 |
| D3–D6 charging, per field, charge once | 11, 12 |
| D7 masking | 5, 9 |
| D8 reveal = save | 11, 16 |
| D9 rep+ | 9–14 (router) |
| D10 sealed platform cache | 7 |
| D11/D21/D22 limits | 8 |
| D12 Net new capped scan | 9 |
| D13/D14 Saved tab, workspace-only filters | 4, 10 |
| D15 two location levels | 2, 4 |
| D16 one Phone section | 4 |
| D17 1,000 cap | 11 |
| D18 all plans | 3 |
| D19 hold + Verify | 13, 14 |
| D20 Verify free for Prospector | 14 |
| D24 company list from Contacts view | 11, 16 |
| D25 token via Provider keys, local deploy | 1, 2 |
| §9.4 formula-safe export | 12 |
| §10 failure handling | 6, 7, 9 |
| §13 MCP | 15 |
| §14 security checklist | 5–15 |
| §15 testing, RLS | every task, 17 |
| CI-equivalent | 18 |
