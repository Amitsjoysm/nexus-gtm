# InfoJoy GTM for AI assistants

InfoJoy GTM is a go-to-market workspace for B2B sales teams: buying signals, accounts scored
against your ideal customer profile, a prioritised inbox, lists and campaigns. Its remote MCP
server, `https://gtm.infojoy.com/mcp`, lets Claude, ChatGPT, Microsoft Copilot, Gemini, Cursor,
VS Code and model APIs work in your workspace as you, with the permissions you choose.

This repository holds what the AI app directories list: the Claude plugin, the ChatGPT plugin, the
Gemini CLI extension, the MCP Registry entry and the Microsoft certification package. It contains
no server code and no credentials.

**Setup guide, permissions and support:** https://gtm.infojoy.com/help/ai-apps

## Connect

| Assistant | How |
|---|---|
| Claude (claude.ai, desktop, mobile, Cowork) | Settings, Connectors: find InfoJoy GTM, or Add custom connector with the server address and keep Sign in now and Claude's published identity. The InfoJoy GTM plugin adds three skills on top. |
| Claude Code | Add this repository as a plugin marketplace with `/plugin marketplace add`, then `/plugin install infojoy-gtm@infojoy`. Or `claude mcp add --transport http infojoy-gtm https://gtm.infojoy.com/mcp`. |
| ChatGPT | Find InfoJoy GTM in the ChatGPT directory, or add the server address in Developer mode. |
| Gemini CLI | `gemini extensions install` with this repository's address, or `gemini mcp add --transport http infojoy-gtm https://gtm.infojoy.com/mcp`. |
| Microsoft Copilot Studio | Tools, Add a tool, Model Context Protocol, the server address, OAuth 2.0. |
| Cursor, VS Code, model APIs | See the setup guide. |

Every assistant signs in with InfoJoy GTM. Nothing can send an email, launch a campaign, place a
call or delete a record from an assistant; research spends the workspace's credits at the app's
prices.

## What is in here

| Path | Listed as | Where |
|---|---|---|
| `plugins/infojoy-gtm/` and `.claude-plugin/marketplace.json` | Plugin (skills plus the connector) | Anthropic's directory (claude.ai, Cowork, Claude Code); a Claude Code marketplace |
| `openai/` | Plugin ("With MCP") | ChatGPT's plugin directory; `scripts/build.py` makes the ZIP |
| `gemini-extension.json`, `GEMINI.md` | Extension | Gemini CLI extensions gallery |
| `server.json` | Server entry | The MCP Registry, and the directories that read it (GitHub, VS Code and others) |
| `microsoft/` | Certified MCP server | Microsoft Partner Center: Copilot Studio, Microsoft 365 Copilot, Azure Foundry; `scripts/build.py` makes the ZIP |
| `assets/` | Icons for every portal | |

The remote server itself is listed as a **connector** in Anthropic's directory and is entered in
each portal by its address; it needs no file here.

## Check and build

```
python scripts/check.py            # every directory's limits, offline
python scripts/check.py --online   # also open every public link and the live server
python scripts/build.py            # dist/: the ChatGPT and Microsoft ZIPs
```

Standard library only. Raise `version` in the files you change before a new submission.

## Privacy and terms

- Privacy policy: https://infojoy.com/legal.html#privacy
- Terms of service: https://infojoy.com/legal.html#terms

## License

MIT, for the files in this repository. Using InfoJoy GTM requires an account and its terms.
