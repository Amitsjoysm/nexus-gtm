# Twilio Call Provider — design

**Date:** 2026-08-20
**Status:** approved, implemented
**Branch base:** `master`
**Builds on:** `3ae2c1a fix(integrations): choosing HubSpot 500ed, and Twilio was a name with
nothing behind it` — which made `build_call_provider` raise `TelephonyNotImplemented` and added
the boot-time resolve in `lifespan`. This change fills in the implementation that commit
declared missing.
**Supersedes the tier-2 placeholder in:** `docs/superpowers/specs/2026-06-19-cold-calling-design.md`

## Goal

Make `NEXUS_TELEPHONY_PROVIDER=twilio` place a real, billable phone call. Production-ready:
adding an Account SID, Auth Token, and caller-ID number to the environment is the *only* step
between the shipped build and a live dial. No stub masquerades as Twilio, no fabricated
recordings, no placeholder code paths.

Click-to-dial (`tel:` links + manually logged dispositions) stays the **default**. It is a real
working workflow that needs no telephony account, not a stand-in for one.

## Why the bridge model

Twilio's `POST /Calls.json` requires either a `Url` returning TwiML or an inline `Twiml`
parameter. Three options were considered:

| Model | Public webhook needed | New deps | Verdict |
|---|---|---|---|
| **Rep-first bridge, inline TwiML** | No | No | **Chosen** |
| Direct dial + TwiML callback URL | Yes | No | Rejected: needs NEXUS publicly reachable by Twilio, plus a signature-verified unauthenticated endpoint |
| Browser softphone (Voice JS SDK) | No | `@twilio/voice-sdk` | Rejected: violates the curated-deps rule in CLAUDE.md |

The bridge is Twilio's own click-to-call pattern and the closest live analogue of what reps do
today: Twilio rings the **rep's** phone, the rep answers, Twilio then dials the prospect and
bridges the two legs. It works with nothing but SID + token + a caller-ID number.

## Components

### `nexus/calling/twilio.py`

Follows the established httpx adapter pattern in `nexus/integrations/apify.py`: inert until
keyed, the provider's own error text surviving into the exception, and no new dependency
(`httpx` rather than the `twilio` SDK). Adds a `transport=` test seam and a connect-capped
timeout from `nexus/verification/reacher.py`, and credentials are never logged.

It departs from `apify.py` on retries. `run_actor` retries 5xx and rotates on 429, which is
right for an idempotent scrape; `POST /Calls.json` is not idempotent, so a retry after an
ambiguous failure can ring a prospect twice and bill twice. Only the read-only lookups retry.

```
TwilioSettings(BaseSettings, env_prefix="NEXUS_")
  twilio_account_sid: str  = ""
  twilio_auth_token: str   = ""
  twilio_api_base: str     = "https://api.twilio.com/2010-04-01"
  twilio_timeout_s: float  = 20.0
  twilio_record_calls: bool = False   # OFF by default: recording consent is jurisdictional
```

Credentials live here rather than `nexus/core/config.py` because that file carries unrelated
uncommitted work. The `NEXUS_` prefix and semantics are identical, so folding these four fields
into the main `Settings` later is a mechanical move requiring no caller changes.

**`place_call(to, from_, context)`** — form-encoded POST to
`{base}/Accounts/{sid}/Calls.json` with HTTP Basic auth:

- `To` = `context["agent_number"]` — the rep's phone, rung first
- `From` = `from_` — `telephony_from_number`, a Twilio-owned or verified number
- `Twiml` = `<Response><Dial callerId="{from_}"><Number>{to}</Number></Dial></Response>`
- `Record` = `true` only when `twilio_record_calls` is set

Both numbers are E.164-validated before the request, so a malformed number fails with our
message instead of an opaque Twilio 400. TwiML is XML-escaped. Returns
`CallHandle(mode="live", provider_call_id=<CallSid>)`.

**`get_call_status(call_sid)`** — `GET /Calls/{sid}.json`, returns status + real duration.
This is what makes the outcome trustworthy without a public webhook.

**`get_recording(call_sid)`** — `GET /Calls/{sid}/Recordings.json`, first recording's `.mp3`
URL, else `None`.

**`get_transcript(call_sid)`** — recordings, then
`GET /Recordings/{rec_sid}/Transcriptions.json`, returns `transcription_text` or `None`.
Twilio transcription is opt-in per recording; absent means `None`, never invented.

### Error contract — deliberately unlike Reacher

Reacher degrades a failure to `unknown` because a wrong deliverability verdict is survivable.
**A dial is not.** `place_call` raises `CallProviderError` on any non-2xx or transport failure,
surfaced as HTTP 502 with Twilio's own `message`/`code` when present. Silently returning a
manual `tel:` handle after a failed Twilio call would be exactly the fake fallback this design
forbids.

`get_call_status` / `get_recording` / `get_transcript` remain best-effort and return `None`,
matching the base-class contract — enrichment must never break outcome logging.

### `nexus/calling/provider.py`

- `"twilio"` joins `KNOWN_PROVIDERS`; `_STUB_KEYS` keeps the `("", "stub", "none")` aliases that
  mean "no telephony", so the click-to-dial default is untouched.
- Existing `TelephonyNotImplemented` is re-parented onto a new `TelephonyError` base, joined by:
  - `TelephonyNotConfigured` — a real provider with no credentials. Distinct from
    `TelephonyNotImplemented` on purpose: "Twilio does not exist here" and "Twilio exists but
    you have not keyed it" need different fixes.
  - `CallProviderError` — the call could not be placed, with `InvalidPhoneNumber` and
    `AgentNumberRequired` beneath it for input problems caught before a request is spent.

The boot-time guard in `nexus/main.py` lifespan already exists (added by `3ae2c1a`) and needs no
change — it now also catches an unkeyed Twilio, because `build_call_provider` raises for that too.

### Wiring — what makes it real

`get_call_provider()` currently has no consumers. It gains three:

1. **`CallQueueService.place_call(ts, task_id, *, agent_number)`** — resolves the task's contact
   phone, calls the provider with `telephony_from_number`, returns the `CallHandle`. Fails with
   a clear error when the contact has no phone or `telephony_from_number` is unset.
2. **`CallQueueService.log_disposition(..., provider_call_id=...)`** — when a live call id is
   supplied, best-effort fetches status/duration, recording URL, and transcript, writing them to
   the **existing** `CallActivity.recording_url` / `.transcript` / `.provider_call_id` /
   `.duration_s` columns. These are the tier-2-ready fields already in `nexus/models/calling.py`,
   so **no migration is required**.
3. **`GET /calling/telephony`** — `{provider, mode, from_number, configured, record_calls}`.
   Returns only whether credentials are present; never the credentials themselves.

`POST /calling/tasks/{id}/dial` (body `{agent_number}`) returns `{mode, dial_url,
provider_call_id}`. RBAC-gated on `manage_accounts`, matching every other calling endpoint.

**Double-dial protection.** A duplicate dial costs money and re-rings a prospect. The endpoint
is deduped through the existing `IdempotencyMiddleware`: the client sends an `Idempotency-Key`
per dial attempt, so a double-submit replays the first response rather than placing a second
call. The middleware is already enabled in the production compose stack.

### Frontend

`CallConsole` fetches telephony status once per mount.

- `mode === "manual"` (**the default**): renders today's `tel:` anchor, unchanged.
- `mode === "live"`: adds a "Call via Twilio" button and a rep callback-number field
  (localStorage-persisted, since `User` has no phone column), keeping the `tel:` link as a
  secondary fallback. The returned `provider_call_id` is sent with the disposition so the
  recording and transcript attach to the right activity.

`RequestOptions` in `api.ts` gains an optional `headers` field to carry the idempotency key.

### Deployment

`NEXUS_TWILIO_*` and `NEXUS_TELEPHONY_*` documented in `.env.example` and
`deploy/.env.production.example`. `docker-compose.yml` needs no change: the `api` service
already does `env_file: .env`, so the variables flow through as-is.

## Known limitations (documented, not hidden)

- **Caller ID is global**, not per-tenant. Every workspace dials from the same
  `telephony_from_number`. Per-tenant Twilio subaccounts are a follow-on.
- **Recording lag**: Twilio may take seconds after hangup to expose a recording. If the rep logs
  the disposition immediately, `recording_url` stays `NULL` rather than being faked.
- **No inbound calls / no live call events** — that needs the public webhook this design avoids.

## Testing

`tests/test_telephony_twilio.py`, driving `httpx.MockTransport` (the repo's existing seam):

- unkeyed `twilio` raises "not configured"; unknown name raises `TelephonyNotImplemented`;
  `""`/`stub`/`none` build the stub
- `place_call` request assertions: URL, Basic auth, form body, TwiML text, `Record` flag
- `CallProviderError` on 401, 429, 500, and transport failure
- `get_call_status` / `get_recording` / `get_transcript`: present and absent
- E.164 rejection of malformed numbers
- API: `/calling/telephony` reports manual by default; `/dial` under the stub returns the `tel:`
  URL; `/dial` with an injected live provider returns a `provider_call_id`; a disposition
  carrying `provider_call_id` attaches recording, transcript, and real duration

Two existing tests in `tests/test_cold_calling.py` used `twilio` as their example of a provider
with no implementation. Their guarantee — an unbuildable name must raise, never silently stub —
still holds, so they now use `vonage`, and a new test asserts the same guarantee one step
further in: **an implemented provider with no keys must also refuse.**

**Not exercised against live Twilio.** There is no Twilio account in this environment. Real
credential authentication, real TwiML bridge behavior, and actual recording/transcription URL
shapes are unverified and will be called out in the PR.
