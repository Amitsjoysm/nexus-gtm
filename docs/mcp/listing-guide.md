# Listing InfoJoy GTM on the AI app marketplaces

Internal guide for the product owner. Follow it top to bottom: section 1 makes production ready,
section 2 prepares what every marketplace asks for, sections 3 to 6 are one marketplace each, and
section 7 is how to check it all works. Verified against the marketplaces' own documentation on
2026-10-13; their forms change, so read each form's own instructions as you go.

What is listed is one thing: the remote MCP server at `https://gtm.infojoy.com/mcp` (code:
`nexus/mcp/`, spec `docs/superpowers/specs/2026-10-13-mcp-server-design.md`). The Claude plugin in
`distribution/claude-plugin/` and the MCP Registry entry in `distribution/mcp-registry/` both point
at it.

---

## 0. Decide these first (each is written into files)

| Decision | Current value | Where it is written |
|---|---|---|
| Product name shown to customers | **InfoJoy GTM** | `nexus/mcp/brand.py`, `frontend/src/lib/brand.ts`, plugin `plugin.json` and `README.md`, `server.json` |
| Production address | `https://gtm.infojoy.com` | plugin `.mcp.json`, `README.md`, `server.json`, `docs/mcp/setup-guide.md` |
| Plugin licence | MIT (`distribution/claude-plugin/infojoy-gtm/LICENSE`) | `plugin.json` `license` and `LICENSE` |
| Privacy policy URL | `https://gtm.infojoy.com/privacy` (must exist before submitting) | plugin `README.md`, every submission form |
| Terms of service URL | e.g. `https://gtm.infojoy.com/terms` | ChatGPT form |
| Support contact | an address customers can write to | every submission form |
| GitHub owner for the plugin repo | e.g. `infojoy/infojoy-gtm-plugin` | plugin README install line (section 4) |
| MCP Registry namespace | `com.infojoy/gtm` (needs DNS on `infojoy.com`) | `server.json` |

If any value changes, search the files above and change every occurrence in one commit.

## 1. Make production ready

1. **Merge and deploy** `feat/mcp-server` (after review). Migration `0068_mcp` is additive; the
   deploy runs `alembic upgrade head` and `apply_rls.py` as usual. The four new tables are
   deliberately outside RLS.
2. **App public address**: Control plane, Configuration, Billing, **App public address** =
   `https://gtm.infojoy.com` (no path). Without it the OAuth metadata names whatever host a request
   arrived on, and Claude requires the resource address to match exactly.
3. **AI apps is on**: Control plane, Feature switches, `module.mcp` must be enabled (it is by
   default). Plans: no plan excludes it by default.
4. **Reachability**: nothing in front of the app (Caddy, a WAF, Azure Front Door) may block or
   rewrite `/mcp`, `/oauth/*` or `/.well-known/*`, and it must accept requests from Anthropic's
   egress range `160.79.104.0/21` and from OpenAI. Check from outside:

   ```
   curl -i https://gtm.infojoy.com/.well-known/oauth-protected-resource
   curl -i https://gtm.infojoy.com/.well-known/oauth-authorization-server
   curl -i -X POST https://gtm.infojoy.com/mcp -H "Content-Type: application/json" -d "{}"
   ```

   The first two answer JSON whose `resource` is exactly `https://gtm.infojoy.com/mcp` and whose
   `issuer` is `https://gtm.infojoy.com`; the third answers `401` with
   `WWW-Authenticate: Bearer resource_metadata="https://gtm.infojoy.com/.well-known/oauth-protected-resource"`.
5. **Timeouts**: Claude gives the token endpoint 10 seconds. Ours answers in milliseconds; make sure
   no proxy buffers it.
6. **Monitoring**: `nexus_mcp_requests_total`, `nexus_mcp_tool_calls_total` and
   `nexus_oauth_events_total` are on `/metrics`. Add a panel; a spike in `unauthorized` or
   `refused` is the first sign a client changed.

## 2. Prepare what every marketplace asks for

Not code. Have all of it before opening any form.

- **Public pages on the production domain**: privacy policy (data collected, how it is used and
  stored, third-party sharing including "data you read through a connected AI app is sent to that
  app and its AI provider", retention, contact), terms of service, and the setup guide
  (`docs/mcp/setup-guide.md`, published as a page).
- **Support contact**: a monitored email address or help page.
- **Icon**: square PNG, at least 512x512, readable small; plus light and dark logo variants for
  ChatGPT.
- **Screenshots**: at least three of InfoJoy GTM answering in Claude or ChatGPT (ChatGPT requires
  them; Claude only for MCP Apps with UI, which we do not ship).
- **A demo video** for ChatGPT: about a minute, connect, approve, ask "what should I work on
  today?", research an account, draft an email.
- **A reviewer workspace on production**: a dedicated account with **no MFA** (reviewers cannot
  receive codes), real populated data (accounts with signals, contacts, an inbox, a list, a
  campaign), credits on the balance, and the owner role. Write its credentials only into the
  submission forms' private reviewer fields, never into a file or ticket.
- **Descriptions** (reuse everywhere):
  - Name: InfoJoy GTM
  - One-liner (200 characters max): "B2B buying signals, account research, contacts and outreach
    drafts from your InfoJoy GTM workspace, acting as you."
  - Long description: adapt the plugin README's first three sections.
  - Categories: Sales, Productivity (or the nearest the form offers).

## 3. Anthropic Connectors Directory (Claude on web, desktop, mobile, Cowork, Claude Code)

Portal: `https://claude.ai/directory/manage` (any paid Claude plan; on Team/Enterprise an Owner).

1. Test first as a custom connector: in Claude, Settings, Connectors, Add custom connector, paste
   `https://gtm.infojoy.com/mcp`, Connect, approve on the consent screen, then call every tool
   once from a chat (a prompt per tool is fine). The portal asks you to confirm this.
2. **Submit new**, choose **MCP connector**, and fill the steps:
   - **Connection**: Universal URL, `https://gtm.infojoy.com/mcp`.
   - **Tools**: synced from the server. All 18 have a title and `readOnlyHint`/`destructiveHint`;
     7 show as read-only and 11 as write. Nothing should be flagged.
   - **Listing**: name, one-liner, description, categories, documentation URL (the setup guide),
     privacy policy URL, support contact, icon, URL slug (permanent).
   - **Use cases**: triage signals and inbox; research an account and draft outreach; build target
     lists. Requirement: an InfoJoy GTM account. Reads and writes data.
   - **Company**: InfoJoy, website, primary contact.
   - **Authentication**: **OAuth with client ID metadata documents**. Our server advertises both
     values Claude checks (`client_id_metadata_document_supported: true` and `none` among token
     auth methods), and Dynamic Client Registration is also available as a fallback. No
     Anthropic-held credentials are needed. Claude's callback
     `https://claude.ai/api/mcp/auth_callback` and Claude Code's loopback redirects already work.
   - **Data handling**: the API is our own; no health data; no sponsored content.
   - **Test & launch**: reviewer workspace credentials and steps: "Connect, sign in with the
     reviewer account, Allow access with all three permissions, then ask: What should I work on
     today? / Research <a seeded account> / Find contacts at <account> / Draft an email to
     <contact> / Find 5 companies like <account> and put them on a list."
   - **Compliance**: seven acknowledgements. Relevant facts: no financial transactions through
     tools (credits are spent only on research we already sell in-app), no media generation, tool
     results mark third-party text as data (prompt-injection), no conversation data collection.
3. Track status in the portal. Escalations: `mcp-review@anthropic.com`.

## 4. Anthropic plugin directory (adds the three skills on claude.ai, Cowork and Claude Code)

1. Create a **public GitHub repository** (e.g. `infojoy/infojoy-gtm-plugin`) and copy into it the
   contents of `distribution/claude-plugin/`: the root `.claude-plugin/marketplace.json` and the
   `infojoy-gtm/` folder. It can stay private while validating.
2. Add the install line the validator asks for to `infojoy-gtm/README.md` under Setup:
   `/plugin install infojoy-gtm --marketplace infojoy/infojoy-gtm-plugin` (your owner/repo).
3. Validate locally with a current Claude Code (2.1.275 or later; older releases reject
   `displayName`):
   `npx @anthropic-ai/claude-code@latest plugin validate ./infojoy-gtm` and `... validate .`.
   Both pass today on 2.1.296.
4. In the portal, **Submit new**, **Plugin bundle**: repository `infojoy/infojoy-gtm-plugin`,
   plugin path `infojoy-gtm`, then **Validate**, connect GitHub when asked, answer data handling
   (reads personal data of business contacts; sends data only to its declared connector; keeps
   nothing; not for under-18s), four acknowledgements, choose the GitHub push webhook, **Submit for
   review**.
5. Submit the connector (section 3) too: Anthropic pairs the plugin with the connector listing.
6. Raise `version` in `plugin.json` on every change.

Before the listing is live, anyone can install it as a self-hosted marketplace:
`/plugin marketplace add infojoy/infojoy-gtm-plugin` then `/plugin install infojoy-gtm`.

## 5. ChatGPT app directory

1. **Verify the organisation** on the OpenAI platform (a hard gate for submitting).
2. **Test in Developer mode**: ChatGPT, Settings, Apps & Connectors, Advanced settings, Developer
   mode on, Create, URL `https://gtm.infojoy.com/mcp`, authentication OAuth, then connect and try
   every tool.
3. Open the app submission form (OpenAI platform dashboard; "Submitting apps to the ChatGPT app
   directory" in the Apps SDK docs). It saves drafts.
4. **Domain verification**: the form shows a token and the path it checks
   (`/.well-known/openai-apps-challenge` today). Paste the token into Control plane, Configuration,
   Access & security, **ChatGPT domain check token**. Check with
   `curl https://gtm.infojoy.com/.well-known/openai-apps-challenge`, then continue in the form. If
   the form names a different path, tell engineering: it is one route in `nexus/api/routers/mcp.py`.
   No mTLS in front of that path.
5. **OAuth login inside the form**: sign in with the reviewer account (no MFA) and Allow access with
   all three permissions, so OpenAI captures working tokens.
6. **Test cases** (paste these):

   Positive:
   1. "What should I work on today?" Expected: a short list of accounts from the inbox and recent
      signals, each with a reason and a next step.
   2. "Research <seeded account>." Expected: a research brief; mentions that it used credits.
   3. "Find contacts at <seeded account>." Expected: new contacts with titles, saved to the account.
   4. "Draft an email to <seeded contact>." Expected: a subject and body, stated as not sent.
   5. "Find 5 companies like <seeded account> and put them on a list called Q4 targets." Expected:
      candidates, then a new list holding the chosen companies.

   Negative:
   1. "Send this email now." Expected: it explains it cannot send, and offers to save it to Drafts.
   2. "Delete the account <name>." Expected: it explains deleting is not available from the app.
   3. With a read-only connection: "Add Acme as an account." Expected: it says the connection may
      not add records and that permissions are changed in InfoJoy GTM, AI apps.
7. **Listing**: name, short and long descriptions, category, logos (light and dark), at least
   three screenshots, website, support URL, privacy policy and terms (on the same domain), the
   demo video.
8. Submit. Review is manual; only one version per organisation can be in review at a time.

## 6. Official MCP Registry (VS Code, GitHub Copilot and registry-driven directories)

1. Install the publisher CLI (`brew install modelcontextprotocol/tap/mcp-publisher`, or a release
   binary from `github.com/modelcontextprotocol/registry`).
2. Generate a key pair locally and add the DNS TXT record on `infojoy.com` that
   `mcp-publisher login dns --domain infojoy.com --private-key-file key.pem` asks for
   (`v=MCPv1; k=ed25519; p=<public key>`). Keep the private key out of git.
3. From `distribution/mcp-registry/`: `mcp-publisher validate` (if it names a newer `$schema`,
   update `server.json` to the URL it gives), then `mcp-publisher publish`.
4. Confirm: `curl "https://registry.modelcontextprotocol.io/v0/servers?search=com.infojoy/gtm"`.
5. Raise `version` in `server.json` whenever the listing changes.

Other directories (Cursor's, Smithery, Glama, PulseMCP) mostly index the registry or GitHub; once
the registry entry exists, claim or confirm those listings if they appear.

## 7. Check everything works

- **Unit and integration tests**: `pytest tests/test_mcp_*.py` (184 tests, all offline).
- **Protocol conformance**: install the optional extra and run the official client against a
  local server: `pip install -e ".[mcp-conformance]"` then
  `pytest tests/test_mcp_conformance.py -n0`.
- **MCP Inspector** against production with an API key (`npx @modelcontextprotocol/inspector`,
  transport Streamable HTTP, URL `https://gtm.infojoy.com/mcp`, header
  `Authorization: Bearer <key>`): list tools, call `get_credit_balance`.
- **Each client once** with OAuth: Claude custom connector, ChatGPT developer mode, Claude Code
  (`/mcp`), and VS Code. Each should show the consent screen with the right app name and redirect
  host, list tools filtered by the ticked permissions, and appear on the AI apps page.
- **Revoke** one connection from AI apps and confirm the client is asked to sign in again.

## After listing

- Both directories review continuously. Keep tool names, titles and annotations stable; adding a
  tool is fine, renaming one breaks saved workflows.
- A change of production address means: new `app_base_url`, new URLs in the plugin and registry,
  and resubmitting the connector URL in both portals.
- Incidents: Control plane, Feature switches, `module.mcp` to `maintenance` stops every tool call
  with the message you write, while connections stay in place.
