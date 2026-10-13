# Use InfoJoy GTM from Claude, ChatGPT and other AI assistants

InfoJoy GTM works inside the AI assistants your team already uses. Ask Claude or ChatGPT what to
work on today, research an account, find the right people and draft outreach, or build a target
list, without leaving the conversation. Each connection acts as you, inside your workspace, with
only the permissions you give it.

This page is the public setup guide. Publish it at a stable public address (for example
`https://gtm.infojoy.com/docs/ai-apps`); the marketplaces link to it.

## Before you start

- You need an InfoJoy GTM account. Every member can connect their own apps, on every plan.
- Server address: `https://gtm.infojoy.com/mcp`
- In InfoJoy GTM, **AI apps** (in the left menu) shows the address, the steps for each app, and
  every app you have connected.

## Connect an app

### Claude (claude.ai, desktop and mobile)

1. In Claude, open **Settings**, then **Connectors**. Find **InfoJoy GTM** in the directory, or
   choose **Add custom connector** and paste the server address.
2. Choose **Connect**. Claude opens InfoJoy GTM.
3. Sign in if asked, tick what Claude may do, and choose **Allow access**.

### ChatGPT

1. Find **InfoJoy GTM** in the ChatGPT app directory and choose **Connect**. To add it by address
   instead: **Settings**, **Apps & Connectors**, turn on **Developer mode** under Advanced settings
   (on plans that include it), choose **Create**, paste the server address and pick **OAuth**.
2. ChatGPT opens InfoJoy GTM. Tick what it may do and choose **Allow access**.

### Claude Code

```
claude mcp add --transport http infojoy-gtm https://gtm.infojoy.com/mcp
```

Then run `/mcp` in Claude Code and choose InfoJoy GTM to sign in. Or install the plugin, which
also adds skills for inbox triage, account research and list building:

```
/plugin install infojoy-gtm
```

### Cursor

Add to `~/.cursor/mcp.json`:

```json
{ "mcpServers": { "infojoy-gtm": { "url": "https://gtm.infojoy.com/mcp" } } }
```

### VS Code

Add to `.vscode/mcp.json`:

```json
{ "servers": { "infojoy-gtm": { "type": "http", "url": "https://gtm.infojoy.com/mcp" } } }
```

Both editors open InfoJoy GTM to sign in the first time a tool is used.

### Scripts and model APIs

Create an API key in InfoJoy GTM under **AI apps**, **Create API key**. Give it only the
permissions the script needs and an expiry. The key is shown once: store it as a secret.

Send it as a bearer token. With OpenAI's Responses API, add InfoJoy GTM as a remote MCP tool:

```json
{
  "type": "mcp",
  "server_label": "infojoy-gtm",
  "server_url": "https://gtm.infojoy.com/mcp",
  "headers": { "Authorization": "Bearer YOUR_API_KEY" },
  "require_approval": "never"
}
```

Other model APIs that support remote MCP tools take the same address and header.

## Permissions

You choose these for each connection, and can change them later in **AI apps**:

| Permission | What the app can do |
|---|---|
| Read | See accounts, contacts, signals, lists, campaigns, your inbox and the credit balance |
| Research | Find contacts, find similar companies, enrich contacts, research accounts, draft emails. Spends your workspace's credits at the same prices as the app |
| Write | Add accounts, create and fill lists, add people to campaigns, complete inbox tasks, save emails to your own Drafts folder |

A connection can never do more than your own role allows, and it never sends an email, launches a
campaign, deletes anything, or changes billing, members or settings.

## Credits

Research actions cost the same credits as in the app; the app checks the balance before spending
anything, and answers "not enough credits" instead of running. Connecting an app costs nothing.

## Revoke access

In InfoJoy GTM, open **AI apps** and choose **Revoke** next to the app or key. It stops working at
once. Admins and owners can see and revoke every connection in the workspace under **Everyone**.

## Your data

When an app reads InfoJoy GTM data through a connection (company and contact details, signals,
drafts), that data is sent to the app and the AI model behind it, under that app's terms. InfoJoy
GTM records which tools each connection used, and research and write actions appear in your
workspace's audit log. See the privacy policy at `https://gtm.infojoy.com/privacy`.

## Troubleshooting

- **"Couldn't reach the server"**: check the address is exactly `https://gtm.infojoy.com/mcp`.
- **The app says it isn't allowed to do something**: change the connection's permissions in
  **AI apps**.
- **"Not enough credits"**: add credits in **Billing**, or ask an admin.
- **"AI apps are switched off"**: an administrator has paused AI apps for your workspace or plan.
- **Sign-in link expired**: start connecting again from the app; links last 10 minutes.
