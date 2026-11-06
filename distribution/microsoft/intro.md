# InfoJoy GTM

InfoJoy GTM is a go-to-market workspace for B2B sales teams: buying signals, accounts scored
against your ideal customer profile, a prioritised inbox of follow-ups, lists and campaigns. This
MCP server lets Microsoft Copilot, Copilot Studio agents and Azure Foundry work in a user's InfoJoy
GTM workspace as that user.

## Publisher

InfoJoy. Website: https://infojoy.com/gtm.html. Setup guide and support:
https://gtm.infojoy.com/help/ai-apps

## Prerequisites

- An InfoJoy GTM account. Every member of a workspace can connect, on every plan.
- Each user signs in with InfoJoy GTM (OAuth 2.0, authorization code with PKCE) and chooses what
  the connection may do: Read, Research and Write.

## Supported operations

36 tools in three permission groups. A connection lists only the tools its permissions allow.

- **Read** (11): search and fetch accounts, contacts, lists, campaigns, replies, calls and people;
  list signals, inbox tasks, lists, campaigns and queues; the workspace overview; the credit
  balance; the team's network; the prospect database (masked).
- **Research** (13, spends the workspace's credits at the app's prices): find contacts and similar
  companies or people, enrich accounts and contacts, verify emails, find phone numbers, refresh an
  account, research an account, draft emails and replies, add companies matching the ideal customer
  profile, create campaign drafts.
- **Write** (12): add accounts, create and change lists, add contacts to campaigns, complete inbox
  tasks, acknowledge alerts, save emails and replies to the user's own Drafts folder, queue calls
  and log their outcome, save prospects.

Every tool carries a `title` and `readOnlyHint`; write and research tools also carry
`destructiveHint: false`. The tool definitions are in `mcptools.json`.

## Authentication

OAuth 2.0 authorization code flow with PKCE (S256), refresh tokens rotated on every use.

- Authorization URL: `https://gtm.infojoy.com/oauth/authorize`
- Token URL (also used to refresh): `https://gtm.infojoy.com/oauth/token`
- Scopes: the three permission scopes and `offline_access`, as published in
  `https://gtm.infojoy.com/.well-known/oauth-authorization-server` (`scopes_supported`)
- Client authentication: `client_secret_basic` or `client_secret_post`

The client id and secret are held in Azure Key Vault and are never part of this package.

## Known issues and limitations

- Nothing can be sent, launched, dialed or deleted through this server: email sending, campaign
  approval and launch, calling, deleting or archiving records, ownership, billing, settings,
  members and integrations stay in the InfoJoy GTM app.
- Research tools spend workspace credits. When the balance is too low the tool answers "not enough
  credits" and nothing runs.
- A connection can never do more than the signed-in user's role allows in their workspace.
- Each connection is limited to a number of tool calls per minute; calls over it are refused with
  `Retry-After`.
- An administrator can pause AI apps for a workspace or a plan; tools then answer with that
  message until AI apps are switched back on.
