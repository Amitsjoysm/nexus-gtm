# SDR Engagement Engine — Design

- **Date:** 2026-09-17
- **Branch:** `feat/sdr-engagement` (from `master` at `79e7c18`)
- **Status:** design agreed section by section with the product owner; awaiting written-spec review
- **Replaces:** today's Campaigns (`nexus/campaigns/`) and Cadences (`nexus/cadences/`) engines

---

## 1. Problem

An SDR's real day is: build a list of **people** → a personalised first email to each → send in
bulk → follow up on a schedule → keep the conversation going when someone answers, including
"try me in June", "back on the 24th", "not interested" and "remove me" → answer the interested
ones fast → report what worked.

The app today drafts and sends single emails. It does not run conversations. Measured against the
workflow (code as of `79e7c18`):

| Step | Today | Gap |
|---|---|---|
| List of contacts | `ProspectList` holds accounts; `ListItem.contact_id` optional | Contact-first lists |
| Personalised draft per contact | `CampaignService` drafts one email per **account**; the agent picks the contact | 50 contacts at 10 companies produce 10 emails |
| Bulk send | `run_send_phase` sends every approved target in one burst | No per-contact scheduling or timing control |
| Timed follow-ups | `CadenceService` exists; `cadence_enabled` and `automation_enabled` default **false** | Each follow-up is a new email, not a reply in the thread |
| Long follow-ups | `cadence_max_duration_days = 30` stops every enrollment at 30 days | "Next June" is impossible by construction |
| Reply detection | **None.** Nothing reads a mailbox; sent mail stores no Message-ID | A cadence stops "on reply" only if someone logs an `Outcome("replied")` by hand |
| Alert + draft a response | None | — |
| Unsubscribe, bounces, do-not-contact | None | Required for CAN-SPAM and for sending-domain reputation |
| Reporting | Sent/skipped counts only | No reply rate, positive rate, meetings |

## 2. Decisions

Every row was decided explicitly with the product owner; the rationale is recorded so a later
change can be weighed against what it gives up.

| # | Decision | Rejected alternatives and why |
|---|---|---|
| D1 | **Replies are read through OAuth: Gmail API and Microsoft Graph only.** | IMAP with app passwords (Microsoft 365 blocks it; weaker threading); an external outreach platform (second tool for the customer, our AI depends on their APIs). |
| D2 | **Campaign mail is also sent through the same OAuth connection.** Existing app-password (SMTP) mailboxes remain for one-off sends from the email composer only. | SMTP send + OAuth read (two connections per SDR, Microsoft is retiring password SMTP, self-managed threading). |
| D3 | **Colleagues pause on any reply.** When anyone at a company replies, follow-ups to their colleagues in any active campaign pause for the SDR to decide. | Fully independent sequences (spam perception, emailing a company after its buyer said no); one person at a time (months to cover a buying committee). |
| D4 | **First emails are reviewed; follow-ups send automatically** if they pass the quality checks. Any campaign can switch on "review every touch". | Review everything (400 approvals for 100 contacts × 4 steps); sample review (a bad draft reaches a buyer). |
| D5 | **Follow-ups are written just before they send**, from the whole conversation plus anything new. | All at launch (stale by week 3, credits spent on people who already replied); templates (not personal). |
| D6 | **Long-horizon follow-ups are fully automatic** — out-of-office pauses and resumes; a confident "later, with a timeframe" schedules a re-engagement that sends on the date — **but a clear "no" is never followed up.** | SDR confirmation for every "later" (rejected by owner in favour of automation, bounded by D7 and D8). |
| D7 | **A clear "no" marks the person do-not-contact everywhere** (reason `declined`) until an SDR or manager lifts it; lifting is logged. **Unsubscribe is always permanent.** | This campaign only (they get cold-emailed again next quarter); 12-month expiry (nobody consciously decides to re-contact). |
| D8 | **A mixed or uncertain reply goes to the SDR and nothing sends** until they decide. Only replies confidently read as "later, with a timeframe" schedule themselves. | The date wins (a soft no gets emailed); refusal wins (loses "no budget this quarter, try in April"). |
| D9 | **No open or click tracking.** Measure sent, bounced, replied, positive, meetings. | Tracking pixels and rewritten links hurt deliverability; opens are unreliable (Apple Mail pre-loads images). |
| D10 | **No sending limits.** A **warning** when a mailbox passes **50 emails in a day** — at launch/approval and as a badge on the mailbox. Provider-imposed limits pause that mailbox until they reset. | Safe default caps with ramp-up (rejected by owner); workspace-wide policy. |
| D11 | **Opt-out line + one-click unsubscribe header** (`List-Unsubscribe`, `List-Unsubscribe-Post`, RFC 8058). | Reply-to-opt-out only (fails Gmail/Yahoo bulk-sender rules at volume); nothing (CAN-SPAM risk). |
| D12 | **Alerts:** the mailbox owner is alerted immediately for replies that need action; everything else lands in the reply desk and the daily digest; managers may route positive replies to a team channel. | Everything alerts (noise); desk only (slow response to interested buyers). |
| D13 | **Rewrite and replace** today's Campaigns and Cadences. Old **tables are kept** as read-only history (migrations are additive-only); old **code paths are removed**. | New engine alongside indefinitely; extend the old account-first engine in place. |
| D14 | **Migrate in-flight sequences at cutover** to the same step and next-send date. A sequence whose owner has no connected Gmail/Microsoft mailbox is paused with a banner. | End them and relaunch; hold the release until none are running. |
| D15 | **The AI always knows the current date and time** and **every email in the conversation**, plus every other conversation with the same person. | — (owner requirement) |
| D16 | **Exactly one `Re:`** in a subject, and **every reply and follow-up goes into the same thread** — the thread of the latest email in the conversation. | — (owner requirement) |
| D17 | **Every draft is hyper-personalised, enforced by a check**: at least one specific, sourced fact about the person or their company. | — (owner requirement) |
| D18 | **Credit estimate at launch; launch is refused if the balance cannot cover the full worst-case sequence.** Applies in `shadow` and `on` billing modes; skipped only when billing is `off` and for unlimited plans. A campaign that runs out mid-way pauses and alerts. | First emails + buffer; expected cost (both risk running dry mid-sequence); warn-only in shadow (would stop nobody in production today). |
| D19 | **Timing is under the user's control per campaign:** first emails immediately or at a scheduled date/time; follow-ups automatic (N business days) or manual per step (offset + time of day + allowed weekdays); per-contact overrides (move, send now, hold). Times are the contact's local time by default. | — (owner requirement) |
| D20 | **OAuth app credentials are configured in the Superadmin panel**, with guided setup steps for the owner. Secrets live in the encrypted provider-keys store; non-secret settings in runtime settings. Claude never enters credentials. | Environment variables only (needs a redeploy). |
| D21 | **No mocks.** Nothing fake ships in the product; releases are verified against real mailboxes; **CI also runs a live suite** against real Gmail and Microsoft test mailboxes. | Recorded provider responses in CI (rejected by owner). |
| D22 | **The AI drafts responses to human replies; the SDR sends.** Nothing is ever sent automatically to a person who wrote to us. Unsubscribes, bounces and out-of-office are handled without sending. | Auto-send for simple intents (wrong answer to a live buyer). |

## 3. Architecture

New package `nexus/engagement/`. Each unit has one job, a small interface, and can be understood
and tested on its own.

| Unit | Responsibility | Depends on |
|---|---|---|
| `mailboxes/` | OAuth connect/refresh/revoke per SDR mailbox; provider adapters implementing `MailProvider` | `network/oauth.py` (PKCE + signed state), `core/crypto.py` (sealing), provider-keys store |
| `messages/` | The message log: persist every in/out email; thread bookkeeping; subject normalisation | — |
| `sequences/` | Campaigns, steps, enrollments; the state machine; the due-step claim; timing modes | `messages/`, `drafting/`, `sending/`, `billing` |
| `drafting/` | Context pack; first-email batch drafting; just-in-time follow-ups; personalisation and quality checks; signature and opt-out footer | `agents/messaging.py`, `agents/email_quality.py`, `agents/email_style.py`, `outreach/signature.py`, `personalization/` |
| `sending/` | Build MIME; pre-send checks; send via provider; idempotency and reconciliation; provider-limit pause; volume warning | `mailboxes/`, `messages/`, `suppression/` |
| `replies/` | Notification intake + polling fallback; matching; privacy filter; deterministic auto-reply/bounce detection; AI classification; date resolution; actions | `mailboxes/`, `messages/`, `sequences/`, `suppression/`, `alerts/` |
| `suppression/` | Do-not-contact list; signed unsubscribe links; one-click endpoint; lift with audit | `models/audit.py` |
| `desk/` | Reply desk queries, decisions, AI-drafted responses | `replies/`, `drafting/`, `sending/` |
| `reporting/` | Funnel per campaign / step / SDR | `messages/`, `replies/`, `outcomes/` |
| `cutover/` | One-off migration from the old engine, with dry run | all of the above |

`MailProvider` interface:

```python
class MailProvider(Protocol):
    async def send(self, mailbox, mime: bytes, *, thread_ref: ThreadRef | None) -> SentRef
    async def fetch_changes(self, mailbox, cursor: str | None) -> tuple[list[InboundMessage], str]
    async def get_message(self, mailbox, provider_message_id: str) -> InboundMessage
    async def find_sent(self, mailbox, *, ref_header: str) -> SentRef | None   # reconciliation
    async def search_sent(self, mailbox, *, to: str, subject: str, around: datetime) -> SentRef | None  # cutover
    async def renew_notifications(self, mailbox) -> datetime                   # new expiry
```

Two implementations: `GmailProvider` (Gmail API) and `GraphProvider` (Microsoft Graph). There is no
third, fake implementation (D21).

**End-to-end flow**

1. SDR connects a mailbox (Google or Microsoft).
2. SDR builds a campaign: contacts → steps and timing → first-email drafts → review queue → launch.
3. Launch runs the credit gate (D18) and shows the volume warning (D10).
4. The worker claims due steps, drafts follow-ups just in time, runs pre-send checks, sends through
   the SDR's mailbox, and logs the message.
5. Google/Microsoft notify us of new mail (a poll every few minutes is the fallback).
6. Each new message is matched to a conversation or discarded (privacy filter), classified, and the
   action rules run: suppress, pause, snooze, re-engage, alert, draft a response.
7. The SDR works the reply desk; reporting updates from the log.

## 4. Data model

Migration `0057_engagement` — **tables only, additive.** Every table carries `tenant_id`, so
`scripts/apply_rls.py` enrols it without manual policy work. Nothing is dropped; the old
`campaigns`, `campaign_targets`, `cadences`, `cadence_steps`, `cadence_enrollments` and
`cadence_touches` tables remain as read-only history.

| Table | Columns (beyond id, tenant_id, timestamps) | Constraints / indexes |
|---|---|---|
| `mailbox_connections` | `owner_user_id`, `provider` (`google`\|`microsoft`), `email`, `display_name`, `tokens` (sealed), `scopes`, `status` (`connected`\|`needs_reauth`\|`revoked`\|`error`), `last_error`, `sync_cursor`, `notifications_expire_at`, `paused_until` (provider limit), `signature` | unique `(tenant_id, email)` |
| `sequence_templates` | `name`, `description`, `steps` (JSON list of step specs), `created_by_user_id`, `legacy_cadence_id` | — |
| `engagement_campaigns` | `name`, `owner_user_id`, `mailbox_connection_id`, `status` (`draft`\|`reviewing`\|`active`\|`paused`\|`completed`), `pause_reason`, `review_every_touch`, `first_send_mode` (`on_approval`\|`scheduled`), `first_send_at`, `timezone_mode` (`contact`\|`sdr`), `source_list_id`, `credit_estimate` (JSON), `legacy_campaign_id` | index `(tenant_id, status)` |
| `engagement_steps` | `campaign_id`, `step_index`, `channel` (`email`\|`call`), `angle`, `timing_mode` (`auto`\|`manual`), `delay_business_days`, `send_time_local`, `allowed_weekdays` | unique `(campaign_id, step_index)` |
| `engagement_enrollments` | `campaign_id`, `contact_id`, `account_id`, `mailbox_connection_id`, `status`, `status_reason`, `current_step_index`, `next_action_at`, `snoozed_until`, `current_thread_id`, `started_at`, `finished_at`, `legacy_enrollment_id` | unique `(campaign_id, contact_id)`; index `(status, next_action_at)` |
| `engagement_threads` | `mailbox_connection_id`, `provider_thread_id`, `contact_id`, `account_id`, `enrollment_id`, `base_subject`, `last_message_at` | unique `(mailbox_connection_id, provider_thread_id)` |
| `engagement_messages` | `thread_id`, `enrollment_id`, `direction` (`out`\|`in`), `status` (`draft`\|`approved`\|`queued`\|`sent`\|`failed`\|`bounced`\|`received`), `step_index`, `provider_message_id`, `rfc_message_id`, `in_reply_to`, `references`, `ref_header` (our hidden reference), `from_addr`, `to_addrs`, `cc_addrs`, `subject`, `body_text`, `quality_problems` (JSON), `sent_at`, `received_at`, `error` | unique `(mailbox_connection_id, provider_message_id)`; **partial unique `(enrollment_id, step_index)` where `direction = 'out'`** |
| `reply_classifications` | `message_id`, `category`, `confidence`, `date_phrase`, `resolved_date`, `reasoning`, `action_taken`, `decision` (`reengage`\|`block`\|`close`\|`meeting`), `decided_by_user_id`, `decided_at`, `suggested_response`, `status` (`open`\|`done`) | unique `(message_id)` |
| `do_not_contact` | `email` (normalised), `contact_id`, `reason` (`unsubscribed`\|`declined`\|`bounced`\|`manual`), `source_message_id`, `created_by_user_id`, `lifted_at`, `lifted_by_user_id`, `lift_note` | unique `(tenant_id, email)` where `lifted_at IS NULL` |

Enrollment `status` values: `active`, `awaiting_review`, `paused` (reasons: `colleague_replied`,
`out_of_office`, `needs_decision`, `mailbox_disconnected`, `out_of_credits`, `manual`), `snoozed`,
`stopped` (reasons: `replied`, `declined`, `unsubscribed`, `bounced`, `manual`), `completed`.

`CallTask` gains a nullable `engagement_enrollment_id`; the old `cadence_enrollment_id` column stays.

**Retention:** message bodies live as long as the contact. Contact deletion and the existing erasure
path delete that contact's `engagement_messages` bodies explicitly (not via cascade, matching the
`people` erasure rule), so behaviour is identical on Postgres and SQLite.

## 5. Sending

**Message construction.** Both providers receive a complete RFC 5322 MIME message (Gmail
`users.messages.send` with `raw`; Graph `sendMail` with MIME content), because both need the
unsubscribe headers and Graph's JSON API does not allow setting them. Plain text only (D9, and the
existing signature rule). Headers:

- `Message-ID`: generated by us and stored before sending.
- `In-Reply-To` / `References`: the latest message in the conversation and the full chain (D16).
- `Subject`: `normalize_subject()` strips every leading reply/forward prefix — `Re:`, `RE:`, `Fwd:`,
  `FW:`, `AW:`, `SV:`, `Antw:`, `Re[2]:`, `Re: RE:` stacks, surrounding whitespace — and follow-ups
  and responses prefix exactly one `Re: ` (D16).
- `List-Unsubscribe: <https://…/u/{signed-token}>, <mailto:…>` and
  `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (D11).
- `X-Nexus-Ref`: our message id, used only for reconciliation.

The body ends with the SDR's signature, then one plain opt-out line: *Not relevant? Just reply "no",
or unsubscribe: {link}* (D11).

**Threading.** Gmail sends carry the stored `threadId`; Graph sends reply to the latest message of
the conversation so it stays in the same conversation. If the person last wrote in a different
thread (§6 rule 3), `current_thread_id` moves there and every later follow-up continues there (D16).

**Pre-send checks** run immediately before each send, not only at scheduling time:

1. Recipient is not on `do_not_contact`.
2. No inbound message arrived in the conversation since the step was scheduled.
3. No colleague pause applies to the enrollment.
4. Mailbox is `connected` and not `paused_until` a future time.
5. The draft passes the quality and personalisation checks (a failing follow-up is held for
   review, never sent — D17).
6. The step's credit charge can be covered (D18).

**Idempotency.** An outbound message row is written as `queued` before the provider call, protected
by the partial unique index on `(enrollment_id, step_index)`. On success it becomes `sent` with the
provider ids. On timeout or an unknown result, `find_sent(ref_header=…)` searches the Sent folder for
our `X-Nexus-Ref` before any retry, so nothing is sent twice.

**Provider limits (D10).** A provider quota or rate-limit response sets `paused_until` on that
mailbox to the provider's reset time (or a conservative back-off when none is given). Due steps stay
due and resume; nothing is lost or duplicated.

**Volume warning (D10).** `sent_today(mailbox)` counts outbound messages since local midnight in the
mailbox owner's timezone. Launch and approval show "This will send about N emails from {mailbox}
today. Above 50 a day raises the chance of being marked as spam." The mailbox card shows a warning
badge while today's count exceeds 50. Warnings never block.

## 6. Reply ingestion

**Intake.** Gmail `users.watch` publishes to a Pub/Sub topic whose push subscription calls our
endpoint; Graph change-notification subscriptions call our webhook (validation token handshake).
Both only say "something changed" — the worker then pulls changes from the stored cursor (Gmail
`history.list`, Graph delta query). A scheduled job renews expiring watches and subscriptions. The
same pull also runs every few minutes as the fallback when a notification is missed.

**Matching** — first rule that matches wins:

1. **Known provider thread** (`engagement_threads`) → that conversation.
2. **Reply headers** (`In-Reply-To` / `References`) name a stored `rfc_message_id` → that
   conversation. Catches clients that break provider threading and replies to forwards.
3. **Same person, different thread** — the sender's normalised address belongs to a contact with an
   active or finished enrollment → linked to that person's most recent conversation; effects apply
   to **every** active enrollment for that person. Also catches replies to emails sent by the old
   engine.
4. **Colleague** — the sender's domain belongs to an account with an active enrollment →
   classified `referral` by default; colleague pause; SDR alerted.
5. **No match** → discarded **without storing** subject or body (privacy filter). Only mail
   belonging to our conversations or known contacts is persisted.

Duplicate notifications are harmless: the unique `(mailbox_connection_id, provider_message_id)`
rejects the second copy. A reply that arrives months after a sequence finished still reopens the
conversation and alerts.

**Deterministic detection before the AI** (cheap, reliable, and not left to a model):

- **Bounce:** a delivery status notification (`multipart/report; report-type=delivery-status`, or a
  mailer-daemon sender) whose embedded original carries our `Message-ID` → message `bounced`, address
  added to `do_not_contact` (`bounced`), enrollment `stopped/bounced`.
- **Auto-reply:** `Auto-Submitted` other than `no`, `X-Autoreply`, `X-Autorespond`,
  `Precedence: auto_reply|bulk|junk` → the auto-reply path; the AI is used only to pull out a return
  date.

**Classification** (AI, structured output, given the context pack in §7):

| Category | Meaning | Action |
|---|---|---|
| `interested` | Wants to talk or learn more | Stop the enrollment; colleague pause; alert owner; draft a response |
| `question` | Asks something | Stop; colleague pause; alert; draft a response |
| `referral` | Points to someone else / has left | Stop; colleague pause; alert; draft a response |
| `later` | Not now, with a timeframe | If `confidence ≥ threshold` and a date resolves unambiguously: snooze and schedule a re-engagement that sends on the date (D6). Otherwise → `unclear` |
| `out_of_office` | Away | Pause the enrollment; resume the business day after the return date (default +7 days when no date is found); no alert (D6) |
| `declined` | Clear no | Stop; `do_not_contact` (`declined`) everywhere; colleague pause; digest (D7) |
| `unsubscribe` | Remove me / stop emailing | Stop; `do_not_contact` (`unsubscribed`), permanent; digest (D7, D11) |
| `other_auto` | Other automated mail (ticket system, "received") | Ignore; stays in the thread |
| `unclear` | Mixed refusal + timeframe, or below the confidence threshold | Pause (`needs_decision`); alert owner; nothing sends until decided (D8) |

A reply containing refusal language together with a timeframe is always `unclear`, never `later`
(D8). The confidence threshold is a runtime setting (`engagement_reply_confidence_threshold`,
default `0.8`, validated to `0.5–0.99`); it governs `later`, `declined` and `unsubscribe`, the three
categories whose actions run without a human. A `declined` or `unsubscribe` reading below it becomes
`unclear`.

The out-of-office default of 7 days when no return date is found is a design choice made here, not
yet confirmed by the owner (flagged for spec review).

**Date resolution** is code, not the model (D15). The model returns only the phrase
("next June", "after Q3", "back on the 24th", "in two weeks"). `resolve_date(phrase, received_at,
tz)` interprets it against the reply's received time in the contact's timezone:

- A month name → the first business day of the next occurrence of that month.
- "Next quarter" / "after Q3" → the first business day of that quarter.
- "In N weeks/months" → that offset from `received_at`, moved to a business day.
- A day of month ("the 24th") → the next occurrence of that day on or after `received_at`.
- Anything that resolves more than one way, or not at all → `unclear`.

## 7. What the AI sees, and personalisation

Every draft, follow-up, classification and suggested response is built from a **context pack**:

- **Now:** current UTC time; the contact's local date, time and weekday; the SDR's local time;
  whether today is a business day for the contact. Contact timezone resolves contact → account
  country → SDR.
- **This conversation:** every message in order with direction and timestamp.
- **Everything else with this person:** other conversations across campaigns and mailboxes — the
  most recent in full, older ones summarised to fit the model's budget.
- **History:** prior classifications, snoozes, declines, SDR decisions.
- **Company and person:** research brief, live signals, value props, workspace style and samples,
  person-level personalisation (headline, recent posts), title, seniority.
- **Instructions:** the step's angle, the structure rule, the signature rule.

The existing drafting pipeline (`MessagingAgent`, `STRUCTURE_RULE`, `check_draft` with one
regeneration) is reused. `check_draft` gains a **personalisation rule** (D17): the draft must use at
least one specific fact present in the context pack about the person or their company. A first
email failing after regeneration is flagged "not personalised" in the review queue. A follow-up
failing it is held for review instead of sending.

Known limit: the LinkedIn personalisation actor currently returns `full-permission-actor-not-approved`.
Until it is approved, personalisation draws on title, seniority, account signals and research.

## 8. Scheduling and timing

- **Business days:** Monday–Friday in the contact's timezone (the campaign's `timezone_mode` can
  switch to the SDR's timezone).
- **First emails:** `on_approval` — become due as each is approved; or `scheduled` — due at
  `first_send_at`.
- **Follow-ups:** `auto` — `delay_business_days` after the previous outbound email, at the same local
  time of day as that email (no send window is imposed, per D10); or `manual` —
  `delay_business_days` + `send_time_local` + `allowed_weekdays`.
- **Per-contact overrides:** move the next step to a date/time; send now; hold.
- **No throughput caps** (D10): every due step is eligible as soon as it is due.
- **Claiming:** `SELECT … WHERE status='active' AND next_action_at <= now ORDER BY next_action_at
  LIMIT n FOR UPDATE SKIP LOCKED` on Postgres, so concurrent workers never take the same enrollment.
- **Snooze and re-engage (`later`):** the enrollment becomes `snoozed` until the resolved date. At
  `snoozed_until` one re-engagement email is drafted with the full context pack (including the reply
  that asked for the delay) and sent in the same thread. Afterwards the enrollment continues with any
  steps that had not yet sent, each after its delay; if none remain, it completes.
- **Out-of-office:** the enrollment is `paused/out_of_office` until the business day after the return
  date, then resumes the step it was on. No extra email is added.
- **Colleague pause:** colleagues' enrollments are `paused/colleague_replied`. They appear in the reply
  desk next to the reply that caused the pause, with resume or stop per colleague; they never resume
  on their own.

## 9. The SDR experience

- **Settings → Mailboxes:** Connect Google / Connect Microsoft; status; today's sent count with the
  over-50 badge; existing app-password mailboxes listed as "one-off sends only".
- **Settings → Do-not-contact:** address, reason, source email; managers lift with a note (audited).
- **Campaigns** (replaces today's page):
  1. *Contacts* — from a list, filters or CSV; an account list expands to contacts with title and
     seniority pickers.
  2. *Sequence* — a template or custom steps, timing modes, sending mailbox, review every touch.
  3. *Draft* — first emails with progress (`WorkingIndicator`).
  4. *Review queue* — per-contact draft, quality and personalisation flags, inline edit, approve,
     approve all that pass, regenerate, remove.
  5. *Launch* — credit estimate and gate, volume warning.
- **Campaign detail:** per-contact step, next action time, status and reason; pause/resume/stop per
  contact or for the campaign; per-contact timing overrides.
- **Reply desk** (new):
  - *Needs action* — interested, question, referral, unclear, plus colleagues paused by a reply.
    The conversation next to account context; an editable AI draft with **Send** (same thread),
    **Save to Drafts**, **Regenerate**; decisions: re-engage on a date, block, close, meeting booked;
    for paused colleagues, resume or stop each.
  - *Scheduled* — re-engagements and out-of-office pauses, with editable or cancellable dates.
  - *Handled* — declined, unsubscribed, bounced.
  - Reps see their own mailboxes; managers see the team and can reassign.
- **Contact and account pages:** a cross-campaign conversation timeline and a do-not-contact badge.
- **Cadences** becomes **Sequence templates**. **Approvals** remains for orchestrator run approvals
  only.

## 10. Credits and billing

- Charges: `ai.email_draft` per draft (existing), `outreach.email_send` per send (existing), and
  two new capabilities — `ai.reply_classify` per classified reply and `ai.reply_draft` per suggested
  response. Both new capabilities ship with rate cards that pass `rates.validate_rate()` (an unpriced
  capability silently runs free). Starting prices are proposed from measured COGS and confirmed by
  the owner.
- **Launch estimate:** `contacts × steps × (draft + send) + contacts × (classify + reply_draft)` —
  the worst case, plus the likely cost shown for information.
- **Gate (D18):** launch is refused when the balance cannot cover the worst case. The check reuses
  `billing/entitlements.preflight` and `_can_cover`, so the price shown is the price charged. It
  applies in `shadow` and `on`; it is skipped when billing is `off` and for `unlimited` plan
  classes.
- **Running out:** a charge that cannot be covered pauses the campaign (`out_of_credits`) and alerts
  the owner and workspace admins; topping up and resuming continues from where it stopped.
- **Module gates:** `module.campaigns` continues to gate the new Campaigns and reply desk;
  `module.cadences` gates sequence templates.

## 11. Alerts and reporting

**Alerts** use the existing routing (`nexus/alerts/`, owner scope from migration `0056`). New
categories, derived into `ALERT_CATEGORIES`:

- `reply_interested`, `reply_question`, `reply_referral`, `reply_needs_decision` — immediate to the
  mailbox owner (D12).
- `reply_scheduled`, `reply_out_of_office`, `reply_bounced`, `reply_unsubscribed`,
  `reply_declined` — reply desk and daily digest.
- `campaign_out_of_credits`, `mailbox_needs_reauth` — immediate to owner and admins.

Managers route positive replies to a team channel through the existing channel rules.

**Reporting:** per campaign, the funnel contacts → sent → bounced → replied → positive → meetings;
reply rate per step; category breakdown; per-SDR time to first response. "Meeting booked" from the
reply desk also writes `Outcome("meeting")`; sends and replies write `Outcome("sent"/"replied")`, so
dashboards and account tiering keep working.

## 12. OAuth app configuration (Superadmin)

- **Secrets** — Google OAuth client secret and Microsoft client secret — go into the provider-keys
  store (`nexus/providers/`): sealed, never returned (last four characters only), with a **Test**
  that distinguishes an invalid client from a valid client not yet authorised by a user.
- **Non-secret settings** go into runtime settings with validators: Google client id, Google Cloud
  project and Pub/Sub topic, Microsoft client id, Microsoft tenant (`common` by default), public
  webhook base URL, classification confidence threshold.
- **Guided setup** is delivered with the task that needs it, as numbered steps for the owner:
  - Google Cloud: project → OAuth consent screen (external, test users) → scopes → OAuth client
    (web, redirect URI) → Pub/Sub topic + push subscription → verification and CASA submission.
  - Azure: app registration → redirect URI → Graph delegated permissions → client secret →
    publisher verification.
  The owner creates the accounts and enters the keys in the panel; Claude does not handle
  credentials.
- Until configured, mailbox connection shows a plain "not configured" state — never a stand-in.

## 13. Cutover and dependency map

**Consumers of the old engine** (from the code at `79e7c18`) and what changes:

| Consumer | Change |
|---|---|
| `nexus/campaigns/`, `nexus/cadences/`, `api/routers/campaigns.py`, `api/routers/cadences.py` | Removed; replaced by `nexus/engagement/` and its routers |
| `api/routers/outcomes.py` | Reads/writes outcomes against new enrollments |
| `workers/tasks.py` (`run_campaign`, `advance_cadences`), `workers/scheduler.py` | Replaced by engagement jobs: `advance_engagement`, `sync_mailboxes`, `renew_mail_notifications` |
| `calling/service.py`, `models/calling.py` | Call steps link `engagement_enrollment_id` |
| `ingestion/tiering.py` | "In an active cadence = hot" reads active engagement enrollments |
| `orchestration/tools.py` (`setup_cadence`) | Creates a sequence template / engagement campaign; `SendMessageTool` stays for orchestrator runs |
| `models/__init__.py` | Registers the new models |
| `runtime_config/catalog.py` | `cadence_enabled`, `cadence_batch_size`, `cadence_max_duration_days`, `campaign_sourcing_enabled`, `campaign_sourced_min_send_confidence` removed from the catalog (stored rows are skipped, per the catalog rule); engagement settings added |
| `billing/catalog.py`, `billing/rates.py` | New capabilities and rate cards; module gates as §10 |
| Frontend: `App.tsx`, `app/nav.tsx`, `components/layout/AppShell.tsx`, `lib/api.ts`, `lib/types.ts`, `lib/display.ts`, `pages/CampaignsPage.tsx`, `pages/CadencesPage.tsx`, `pages/CallsPage.tsx`, `pages/ListsPage.tsx`, `pages/AccountsPage.tsx`, `pages/ContactsPage.tsx`, `pages/SettingsPage.tsx`, `components/discovery/ResultsPanel.tsx` | Rewired to the new flow |
| 7 test files referencing the old engine | Replaced by engagement tests |

**Migration script** `scripts/migrate_engagement.py` — idempotent, `--dry-run` prints a report
without writing:

| Old | New |
|---|---|
| `Cadence` + `CadenceStep` | `sequence_templates` |
| Active `CadenceEnrollment` | `engagement_enrollments` at the same step and `next_action_at`; mailbox = the campaign creator's connected mailbox, else `paused/mailbox_disconnected` with a banner |
| Sent cadence touches / campaign targets | `engagement_messages` (`out`, `sent`), thread recovered by `search_sent(to, subject, around)` in the mailbox's Sent folder; not found → the next follow-up starts a new thread |
| Campaign `awaiting_approval` | `engagement_campaigns` in `reviewing` with drafts carried into the review queue |
| Completed / cancelled campaign | Left in the old tables; listed read-only as "Legacy" |
| `CallTask.cadence_enrollment_id` | `engagement_enrollment_id` populated |
| `Outcome` rows | Unchanged |

The dry-run report lists: sequences to move, sequences that will pause for lack of a mailbox, old
emails found and not found in Sent folders, and campaigns moving to review.

## 14. Testing (no mocks, D21)

- **Pure logic, tested directly on real data:** `normalize_subject`, `resolve_date`, the matching
  rules, business-day and timezone arithmetic, the enrollment state machine, the credit estimate,
  the deterministic bounce and auto-reply detectors (fed real DSN and auto-reply samples).
- **Live provider suite** (`tests_live/engagement/`, its own CI job, requires OAuth secrets for one
  Gmail and one Microsoft 365 test mailbox): connect/refresh; send; thread follow-ups; exactly one
  `Re:`; unsubscribe headers present; reply from the other mailbox detected and matched; reply in a
  new thread matched by sender; out-of-office; bounce to a non-existent address; duplicate
  notification ignored; send-timeout reconciliation; cutover thread recovery from Sent. Mail flows
  only between the test mailboxes; each run tags messages with a run id and cleans up.
- **Live classification suite:** real AI calls over a table of real-world replies including mixed
  and unclear ones, asserting categories and resolved dates.
- **Existing suite conventions for everything else:** RLS binding guard and migration replay for the
  new tables; Postgres integration test for the `SKIP LOCKED` claim with two concurrent workers;
  credit gate across `off` / `shadow` / `on` / unlimited; source-reading UI tests.
- **Release verification:** every flow in §9 exercised end to end against real mailboxes before
  cutover.
- **Before any push:** the full CI-equivalent suite on the exact commit, plus the live job.

Trade-offs accepted with D21: the live jobs are slower, depend on Google and Microsoft being up,
need CI secrets, and incur small AI and provider costs per run.

## 15. Rollout

1. **Start external approvals now** (§12): Google verification and CASA; Microsoft publisher
   verification. Longest lead time; they gate external Gmail customers.
2. **Release A — dark:** migration `0057`, `nexus/engagement/`, mailbox connection UI and Superadmin
   configuration. Campaign features stay off. SDRs connect mailboxes so the cutover can attach them.
   Today's Campaigns keep running.
3. **Release B — cutover:** run `migrate_engagement.py --dry-run` and review the report with the
   owner → run for real → verify counts → remove old code paths → switch the UI and nav.

## 16. Risks

| Risk | Mitigation |
|---|---|
| Google verification/CASA delays external Gmail customers | Start now; Microsoft and internal test users are unaffected |
| No sending limits damage sender reputation (D10) | Over-50 warning at launch and on the mailbox; provider limits pause the mailbox |
| The AI misreads a reply | Deterministic bounce/auto-reply detection first; confidence threshold; mixed replies go to the SDR (D8); a clear no blocks (D7) |
| Reading SDR inboxes is sensitive | Unmatched mail is never stored (§6 rule 5); bodies deleted with the contact (§4) |
| Cutover disrupts running outreach | Dry-run report reviewed first; idempotent migration; unmailboxed sequences pause visibly rather than fail |
| Person-level personalisation provider blocked | Personalisation falls back to title, account signals and research; the check still requires a specific fact |
| AI cost per reply | Classification and response drafts are priced capabilities with rate cards |

## 17. Not in scope

Open/click tracking (D9); LinkedIn or SMS steps; automatic meeting detection from calendars; mailbox
rotation across multiple SDR mailboxes; warm-up services; A/B testing of steps; SMTP/IMAP sending or
reading for campaigns (D1, D2).
