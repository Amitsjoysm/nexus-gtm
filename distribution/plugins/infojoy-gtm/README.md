# InfoJoy GTM for Claude

Work your B2B pipeline from Claude. InfoJoy GTM collects buying signals (funding, hiring,
leadership changes, launches), scores accounts against your ideal customer profile, and keeps a
prioritised inbox of follow-ups. This plugin connects Claude to your InfoJoy GTM workspace and adds
three skills that know how to use it.

## What you can ask

- "What should I work on today?" Claude reads your inbox and recent signals and lists the accounts
  that need you, with the next step for each (skill: `triage-inbox`).
- "Research Acme and draft an email to their VP of Sales." Claude pulls the account, runs a research
  brief, finds contacts if needed and drafts emails for you to review (skill:
  `research-and-outreach`).
- "Find ten companies like our best customer and put them on a list." Claude finds similar
  companies, adds the ones you choose, builds a list and can load it into a campaign for review
  (skill: `build-a-list`).

## Setup

You need an InfoJoy GTM account. After installing the plugin, open its **Connectors** tab and
connect InfoJoy GTM. Claude opens InfoJoy GTM, where you sign in, choose what Claude may do (Read,
Research, Write) and approve. You can change those permissions or disconnect at any time in
InfoJoy GTM under **Settings**, **AI apps**. The full setup guide, for Claude and every other
assistant, is at https://gtm.infojoy.com/help/ai-apps

## What it can and cannot do

- **Read**: accounts, contacts, signals and alerts, lists, campaigns, the reply desk, your call
  list, who your team knows, the prospect database (masked) and the credit balance.
- **Research** (spends your workspace's credits, at the same prices as the app): find contacts and
  similar companies, enrich and verify, research accounts, draft emails and replies, and create
  campaign drafts.
- **Write**: add accounts, create and fill lists, add people to campaigns, complete inbox tasks,
  save emails and replies to your own Drafts folder, and queue calls and log how they went.
- It never sends an email, launches a campaign, places a call, deletes anything, or changes billing
  or settings.

## Data

The plugin itself stores nothing and runs no code on your computer. Claude talks to one server,
`https://gtm.infojoy.com/mcp`, which acts as you inside your workspace with the permissions you
granted. What Claude reads through it (company and contact details, signals, drafts) is sent to
Claude and processed under Anthropic's terms for your Claude account. Text that comes from the
public web or from email replies is returned as data; the skills tell Claude never to follow
instructions found in it.

## Privacy

InfoJoy GTM's privacy policy covers what is collected, how it is used and stored, third-party
sharing, retention, and how to contact us:

- Privacy policy: https://infojoy.com/legal.html#privacy
- Terms of service: https://infojoy.com/legal.html#terms

## Support

See https://gtm.infojoy.com/help/ai-apps#support or ask your workspace administrator.
