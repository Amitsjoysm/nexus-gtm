# NEXUS GTM — SDR Handbook

*The practical guide to running your day on NEXUS. Read once end-to-end, then keep it open as a reference.*

---

## 1. What NEXUS does for you

NEXUS is an AI Go‑To‑Market workbench. Instead of you hunting through tabs — CRM, LinkedIn, news, a sequencer, a spreadsheet of ICP rules — NEXUS watches your territory, scores every account against *your* ideal customer profile, turns fresh buying signals into a prioritized **daily task list**, drafts grounded outreach with AI, and (after your one‑click approval) sends it from your own mailbox and logs it to your CRM.

You stay in control of three things that matter: **who** you target, **what** the message says, and **whether** it sends. NEXUS does the research, scoring, drafting, sequencing, and busywork around those decisions.

**The loop you'll live in:** Signal → Fit score → Inbox task → AI draft → Approve → Send → CRM sync → Outcome → smarter scoring.

---

## 2. First 15 minutes (one‑time setup)

1. **Sign in** and confirm you're in the right **workspace** (top‑left switcher). Each workspace is a fully isolated book of business — accounts, contacts, and signals never leak between them.
2. **Define your ICP** under **Relevance** (Section 8). This is the single most important setup step: it drives every Fit score.
3. **Connect a sending mailbox** under **Settings → Sending mailboxes** (Section 11). Add your Gmail/Outlook with an app password and send a test. Nothing sends until you do this *and* approve each message.
4. (If you're an admin) confirm **CRM auto‑sync** is on under **Settings → CRM auto‑sync** so won accounts and activity flow to HubSpot automatically.
5. Skim the **Dashboard** so you recognize where things live.

---

## 3. Your daily rhythm

| When | Screen | What you do |
|---|---|---|
| Start of day | **Inbox** | Clear the prioritized task list (signals that fired overnight). Triage with the keyboard. |
| Mid‑morning | **Accounts** | Work your highest‑Fit accounts; run AI research + draft outreach. |
| Throughout | **Approvals** | Review, redraft, and approve AI‑drafted sends. |
| As needed | **Orchestrator** | Kick off discovery (find net‑new accounts/contacts) or a multi‑step AI run. |
| End of day | **Dashboard / Cadences** | Check what advanced, what's awaiting you, log outcomes. |

---

## 4. The Inbox — your prioritized day

The Inbox is the heart of the SDR workflow. Every actionable buying signal on your accounts becomes a **task**, ranked by priority and freshness.

- **What's in a task:** the account, what happened (e.g. *"Brex: Funding round"*), the underlying headline, a deliverability cue for the buyer's email, and an AI **suggested action**.
- **Keyboard‑first triage** (you can clear dozens fast):
  - `J` / `K` (or ↑/↓) — move between tasks
  - `E` — complete the selected task (in the **Completed** view, `E` re‑opens it)
  - `D` / `Enter` — open the account
- **Open / Completed toggle:** switch to **Completed** to find anything you closed by mistake and **Mark incomplete** to pull it back. Nothing is ever lost.
- **Why a task is here:** only relevant signals on accounts that clear your Fit bar surface — you won't see generic news noise.

> Pro tip: run the Inbox to zero each morning. A task you complete from a strong signal is the best‑timed outreach you'll send all day.

---

## 5. Accounts — research, score, act

The **Accounts** table is your territory, enriched and scored.

- **Columns:** Fit score (0–100, color‑coded), Industry, Location, Employees, LinkedIn, and row actions.
- **Filters:** narrow by industry, location, source, and minimum Fit. Search by name/domain.
- **Row actions:** **Push to CRM** (sync this account + contacts to HubSpot now) and **Remove** (archive it out of your working list).
- **Add accounts** manually, or let the **Orchestrator** discover them for you.

### Account 360 (click any account)
- **Overview:** firmographics, Fit breakdown, recent signals, contacts.
- **AI Actions tab** — the workhorse:
  - **Research** — a grounded brief on the account (facts + sources), used to ground every message.
  - **Messaging** — draft a personalized cold email grounded in research + your value props.
  - **Q&A** — ask anything about the account; answers cite the research.
  - **Contact recommendation** — who to reach and why.
- **Find contacts** — source net‑new, real people at the account (name, title, verified email).
- **Find lookalikes** — given this account's firmographics, surface similar companies to add to your pipeline.

> Everything the AI writes is **grounded** — tied to retrieved facts. Ungrounded drafts are flagged and the system refuses to send them. That's by design: no generic spam goes out under your name.

---

## 6. Contacts — the people

The **Contacts** screen lists every person across the workspace with their account context.

- **Email status:** `valid` / `risky` / `unknown` / `invalid`, or **unverified** until checked. Use **Verify** on a row to re‑check deliverability before you send.
- **LinkedIn** link, title, seniority, and a **Push to CRM** path.
- Filter by status and search across name/title/email/account.

---

## 7. Signals — why now

**Signals** is the raw feed of buying events NEXUS detected on your accounts (funding, hiring, product news, etc.), each with a strength and source. Signals are what create Inbox tasks and re‑score accounts. You'll mostly consume signals through the Inbox, but the feed is here when you want the full picture.

---

## 8. Relevance — teach NEXUS your ICP

**Relevance** is where you define what a good account looks like. It drives every Fit score and grounds every AI message.

- **Ideal Customer Profile:** target **industries**, **countries**, employee **min/max**, and **required tech**.
- **Value propositions:** what you sell and the pains it solves — the AI uses these to personalize outreach.
- **Product context:** background the agents reference when researching and writing.
- **Fit weighting (sliders):** dial how much **Industry / Company size / Geography / Tech stack** each count toward the score. Positions are relative; the readout shows each dimension's share. Hit **Reset to defaults** to return to 35/30/15/20. Experiment here when scores don't match your gut.
- **Auto‑learned weighting:** as you log won deals, NEXUS nudges the weights toward the traits your wins share. Your explicit slider settings always take priority.

> If your Inbox feels off (wrong accounts surfacing), tune Relevance first — it's the lever behind everything.

---

## 9. Orchestrator — AI runs & discovery

The **Orchestrator** runs multi‑step AI workflows as a planned sequence of agents (research → scoring → compose → send), pausing at the **approval gate** before anything leaves.

- **Discovery:** describe an ICP and let NEXUS surface net‑new **companies** or **contacts** that match — then add them to your accounts in one step.
- **Chat:** converse with the orchestrator to scope a run.
- **CSV import:** bulk‑load accounts/contacts.
- **Run console:** watch a run progress live; results stream in.

Any account can be sent into the orchestrator to run the full research‑to‑draft pipeline automatically.

---

## 10. Approvals — the human gate (your most important screen)

Every AI‑drafted send **parks here** for your sign‑off. Nothing reaches a prospect without it.

For each draft you can:
- **Read** the subject, body, grounding facts, a **Grounded/Ungrounded** badge, and the recipient's deliverability.
- **Edit draft** — tweak subject/body by hand.
- **Redraft with AI** — type instructions ("make it two sentences, lead with the funding signal, mention SOC 2") and the messaging agent regenerates the draft, grounded in the same research. Iterate until it's right.
- **Send from** — pick which connected mailbox it goes out from (when you have more than one).
- **Approve & send** — it goes to the sequence and sends from your mailbox.
- **Reject** — with an optional **reason** the account owner will see.

Two hard gates protect your reputation: an **ungrounded** draft or an **undeliverable** address is refused even if you click approve — fix the research or the contact, or reject.

---

## 11. Settings — automation, CRM, mailboxes

- **Continuous automation:** when on, the worker keeps accounts fresh (re‑scores stale ones) and advances cadences each tick. Each workspace opts in here.
- **CRM auto‑sync:** shows live status — how many accounts are up to date vs. pending — and pushes scored accounts, contacts, and activity to HubSpot as they change.
- **Sending mailboxes (SMTP):** add **multiple** Gmail/Outlook mailboxes (label, from‑name, app password), mark a **default**, and **send a test** per mailbox. Approved outreach sends from the mailbox you pick at the gate. Verified/Unverified badges tell you which are ready.

---

## 12. Campaigns & Cadences — outreach at scale

- **Campaigns:** select a set of accounts, NEXUS drafts personalized outreach for each, you approve, and it sends — with live progress.
- **Cadences:** multi‑touch sequences. Enroll contacts, and the engine advances each touch on schedule (with stop conditions). Turn on **review each touch** to keep the approval gate on every step, or let approved cadences run.

---

## 13. Plays, Lists, Alerts, Analytics

- **Plays:** automation rules ("when X signal fires on a Fit ≥ 70 account, do Y"). Plays can also surface new accounts.
- **Lists:** saved segments (e.g. "Fintech, 100–1000 employees, US, Fit ≥ 60") that feed campaign targeting.
- **Alerts:** high‑priority notifications routed to you.
- **Analytics / Dashboard:** live activity feed, KPIs, and manager **attribution** + **funnel** views tying outreach to outcomes.

---

## 14. Working across workspaces (and why your data is safe)

The top‑left switcher moves you between **workspaces (tenants)**. Each is a hard isolation boundary: accounts, contacts, signals, drafts, and mailboxes in one workspace are invisible to another. This is enforced in two independent layers — application‑level scoping **and** database Row‑Level Security — so your book of business is never exposed to another team or customer.

---

## 15. Quick reference — keyboard & gotchas

- **Inbox:** `J/K` move · `E` complete/reopen · `D`/`Enter` open account.
- **Nothing sends without (a) a connected mailbox and (b) your approval.** If a "send" seems to do nothing, check Settings → Sending mailboxes.
- **A draft won't approve?** It's ungrounded or the email is undeliverable — run research / fix the contact, or reject.
- **Wrong accounts in your Inbox?** Tune **Relevance** (ICP + Fit sliders).
- **Want it in your CRM now?** Use **Push to CRM** on the account, or rely on auto‑sync.
- **Closed a task by mistake?** Inbox → **Completed** → **Mark incomplete**.

---

*You bring judgment on who, what, and whether. NEXUS brings the research, scoring, drafting, and the busywork in between. Run your Inbox to zero, keep Relevance sharp, and let the approvals gate protect your name.*
