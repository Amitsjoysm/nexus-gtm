# Signal dates & window, credits names, telephony connections — design and plan

> TDD per task (failing test, run, implement, run, commit). Decisions recorded with the product
> owner on 2026-09-23.

## What was wrong (measured before designing)

1. **The signal day filter filtered on the wrong date.** The top-bar window sends `max_age_days`
   and the server compares it with `signal_events.occurred_at` — but no source ever set that
   column. `RawSignal.occurred_at` defaults to `utcnow`, and none of the 11 construction sites
   passes one, so every signal is dated the moment it was collected. Measured on the local
   database: 1,099 of 1,112 signals dated within ten minutes of collection, and none older than
   the day collection began. Every window showed the same list. RSS `pubDate`, job-board posting
   dates, SEC `filed_at` and Hacker News `created_at` were all read and thrown away.
2. **Credits "Who used them" showed a user id.** `CreditUserRowOut` carries `user_id` and
   `credits` only. The screen was written to show `email` and falls back to the id — always.
3. **Telephony could only be configured by environment variables.** Calling works end to end
   (rep-first bridge), but no screen lets a superadmin or a workspace connect Twilio, and
   `calling.minutes` (priced at 4 credits/min) is metered nowhere.

## Decisions (product owner, 2026-09-23)

| Question | Decision |
|---|---|
| A signal whose source gives no date | Date first found, labelled "found" in the UI. Nothing disappears. |
| Where the window applies | Signals, Dashboard, Account page (already) **plus Inbox, Alerts, AI drafts and research**. NOT scoring or plays. |
| Superadmin switch | Platform-wide. ON: each user picks a window, starting from the superadmin default. OFF: no picker; the superadmin window applies to everyone, enforced server-side. |
| Window options | Weekly 7, Fortnightly 14, Monthly 30, Quarterly 90, Half-yearly 180, Yearly 365, All time. |
| Twilio | Both, like CRM: a workspace's own Twilio when connected, else the platform account. |

## Design

### Signal dates
- `RawSignal.dated`: `"event"` when `occurred_at` is when it happened, `"found"` when it is only
  when we found it. Job boards and website changes are `"event"` at collection time — they report
  a state observed now, which is the true date.
- Column `signal_events.dated` and `company_signals.dated` (migration `0057`, nullable; NULL is a
  legacy row and reads as `"found"`). Carried through `IngestionService.ingest`, the shared crawl
  and fan-out.
- `nexus/ingestion/dates.py`: one parser (ISO 8601, RFC 2822, `YYYY-MM-DD`, relative "3 days
  ago"), `date_from_url`, and a clamp (future → now, implausibly old → unknown).
- RSS reads `pubDate`/`published`/`updated`/`dc:date`. `SearchHit.published_at` is filled where
  the provider returns one (Exa, Brave, Serper); web news and dorks fall back to a date in the URL.
- **Consequence, stated up front:** scoring already decays a signal to zero over 90 days by its
  `occurred_at`. With true dates, old news found recently stops counting as fresh intent — the
  decay working as written, not the window applied to scoring.

### The window
- Runtime settings `signal_window_user_choice` (bool, default on) and `signal_window_default`
  (default `all` — a strict no-op until a superadmin picks one).
- `nexus/ingestion/window.py`: `effective_days(requested)` — choice off → the default, enforced;
  choice on → what the client asked for. `ai_window_days()` — always the platform default, since
  a draft has no top bar.
- `GET /signals/window` for every member; the frontend context reads it, hides the picker when
  choice is off.
- `/signals`, `/inbox`, `/alerts` filter through `effective_days`. Inbox and Alerts hide rows whose
  source signal is older than the window; rows with no signal always show.
- Messaging, call script, Q&A and the call brief drop signals outside `ai_window_days()`, and
  `signal_facts` states each date so a model never calls an old round "recent". Scoring untouched.
- `scripts/repair_signal_dates.py` (dry run by default) re-dates stored rows from a URL date, the
  SEC key, or a re-read of the company feed.

### Credits names
- `by_user` rows gain `name` and `email`, resolved through workspace membership. A user who has
  left reads "Former member" rather than an id.

### Telephony
- Workspace: `IntegrationConnection(kind="telephony", provider="twilio")`, sealed
  `{account_sid, auth_token}`, caller ID in a new `config` JSON column (migration `0058`) because
  a caller ID is not a secret and the screen must show it.
- Platform: provider key `twilio` in `ACsid:authtoken` form, probed against the Twilio account
  API; `telephony_provider` and `telephony_from_number` join the runtime catalog.
- `resolve_call_provider(ts)`: test seam → workspace connection → platform (managed key, then env)
  → click-to-dial. Dial, disposition and status all use it.
- Minutes are charged only on the platform account: preflight before the dial, the measured
  minutes at disposition. A workspace on its own Twilio pays Twilio directly.

## Tasks

| # | Task | Pinned by |
|---|---|---|
| 1 | Credits by-user rows carry name and email | `tests/test_credit_usage_names.py` |
| 2 | Signal dates: column, parser, every source, search hits, ingest, API, UI label | `tests/test_signal_dates.py` |
| 3 | Window policy: settings, `/signals/window`, enforced on signals, inbox, alerts | `tests/test_signal_window.py` |
| 4 | Window in AI drafts, research and the call brief; dates in prompts | `tests/test_signal_window_ai.py` |
| 5 | Frontend: policy-driven picker, Inbox and Alerts follow the window, "found" label | typecheck, build, source tests |
| 6 | Repair script for stored signal dates | `tests/test_repair_signal_dates.py` |
| 7 | Telephony: workspace connection, platform key, resolver, metering, screens | `tests/test_telephony_connections.py` |
| 8 | CLAUDE.md | — |

Verification: each task's tests; ruff; frontend typecheck and build; the full suite; the running
app in the browser.
