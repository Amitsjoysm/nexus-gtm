# Deploying InfoJoy GTM's MCP server and listing it with every major AI provider

Internal guide for the product owner. Follow it top to bottom: section 1 says what works today,
sections 2 and 3 put the server on production and prove it, section 4 prepares what every portal
asks for, sections 5 to 12 are one provider each, and section 13 is upkeep. Checked against each
provider's own documentation on **2026-10-21**. Portals change often: read each form's own
instructions as you go, and treat anything marked *preview* as subject to change.

What is listed is one thing: the remote MCP server at `https://gtm.infojoy.com/mcp` (code:
`nexus/mcp/`, spec `docs/superpowers/specs/2026-10-13-mcp-server-design.md`). It speaks Streamable
HTTP (the transport every provider below now requires; none accepts SSE-only servers), signs users
in with its own OAuth 2.1 server, and also takes per-user API keys for developer tools.

---

## 1. Where things stand (2026-10-21)

Tested end to end on the local stack, which runs the same MCP code as production:

| Check | Result |
|---|---|
| Discovery (`/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`) | correct on local and on **production** (`gtm.infojoy.com`) |
| Unauthenticated `POST /mcp` | `401` with `WWW-Authenticate: Bearer resource_metadata=...`, on local and production |
| OAuth sign-in (DCR, PKCE S256, consent, code exchange, refresh rotation, replay refused) | every step passes |
| Official MCP client v2.3.0, both protocol dialects | PASS |
| Every tool, called for real | works; refusals (no mailbox, unknown id, provider down) come back as sentences |
| **Staging** `staging-gtm.infojoy.com` | **does not resolve (NXDOMAIN)**. The app is healthy at `gtm-staging-app.mangowater-46a1ec4f.eastus2.azurecontainerapps.io`; add the DNS record (section 2.5) before testing cloud clients on staging |

If an AI app "doesn't work", the usual causes, in order:

1. **A local or private address.** Claude, ChatGPT, Copilot, Gemini and Perplexity connect from
   their own servers on the internet: `localhost`, `:8099` or a VPN address cannot work for them.
   Use production (or staging once its DNS exists).
2. **The address does not match the App public address.** OAuth names
   `{app_base_url}/mcp` as the resource; a client that connected through any other host or port
   (the local stack's `:8099`, the staging Azure host while `app_base_url` says otherwise) fails the
   resource check after sign-in.
3. **Something in front of the app** (WAF, Front Door, a proxy) blocking or rewriting `/mcp`,
   `/oauth/*` or `/.well-known/*`, or blocking the provider's egress addresses.
4. **AI apps switched off**: Control plane, Feature switches, `module.mcp`.

### What the tools cover

36 tools since 2026-10-19 (owner decisions on what may be done from chat). A connection sees only
the ones its permissions allow: Read shows 11, all three show 36. 36 stays under the ~40 tools some
clients cap across every connected server.

| Permission | Tools |
|---|---|
| Read (free) | `search`, `fetch` (account, contact, list, campaign, reply, call, person, populate), `list_signals`, `list_inbox`, `list_lists`, `list_campaigns`, `get_credit_balance`, `prospector_search`, `get_workspace_overview`, `list_queue` (alerts, replies, calls), `search_network` |
| Research (spends credits, priced from the rate cards) | `find_contacts`, `find_similar_companies`, `enrich_contact`, `research_account`, `draft_email`, `enrich_account`, `verify_email`, `find_phone`, `refresh_account`, `find_similar_people`, `add_icp_companies`, `draft_reply`, `create_campaign_draft` |
| Write | `create_account`, `create_list`, `add_to_list`, `remove_from_list`, `add_contacts_to_campaign`, `complete_inbox_task`, `save_email_draft`, `prospector_save`, `acknowledge_alert`, `save_reply_draft`, `queue_call`, `log_call_outcome` |

Never from an AI app (a test refuses any tool path that reaches them): sending email, approving or
launching a campaign, dialing, deleting or archiving records, changing owners, billing, settings,
members, integrations.

## 2. Put it on production

1. **Merge, in order, after review**: `feat/prospect-engine` (Prospector, migration `0072`), then
   `feat/mcp-coverage` (stacked on it: the outage-message fix, campaign list chooser, 36 tools).
   Neither touches `deploy/`. Run the full CI-equivalent in `CLAUDE.md` on the exact merge commit.
2. **Deploy** through the usual pipeline. The app runs `alembic upgrade head` and `apply_rls.py` on
   start; the MCP tables are platform-global and deliberately outside RLS.
3. **App public address**: Control plane, Configuration, Billing, **App public address** =
   `https://gtm.infojoy.com` (an origin, no path). Without it the OAuth metadata names whatever
   host a request came in on, and Claude and ChatGPT require the resource to match exactly.
4. **`module.mcp` enabled** (Control plane, Feature switches; on by default, on no plan).
5. **Staging DNS**: create `staging-gtm.infojoy.com` (CNAME to the staging container app host,
   then bind the custom domain and certificate on `gtm-staging-app`) and set staging's App public
   address to `https://staging-gtm.infojoy.com`.
6. **Reachability**: nothing in front of the app may block or rewrite `/mcp`, `/oauth/*` or
   `/.well-known/*`. Allow the providers' egress (Anthropic publishes `160.79.104.0/21`; OpenAI,
   Microsoft and Google connect from their clouds, so do not IP-filter these paths at all unless a
   provider publishes its range). No buffering proxy on `/oauth/token` (Claude allows 10 seconds).

Check from outside the network:

```
curl -i https://gtm.infojoy.com/.well-known/oauth-protected-resource
curl -i https://gtm.infojoy.com/.well-known/oauth-authorization-server
curl -i -X POST https://gtm.infojoy.com/mcp -H "Content-Type: application/json" -d "{}"
```

The first two answer JSON whose `resource` is exactly `https://gtm.infojoy.com/mcp` and whose
`issuer` is `https://gtm.infojoy.com`; the third answers `401` with
`WWW-Authenticate: Bearer resource_metadata="https://gtm.infojoy.com/.well-known/oauth-protected-resource"`.

## 3. Prove it works on production

- **Official client, both dialects** (own virtual environment; spends nothing, changes nothing):
  create a Read-only API key in Settings, AI apps, then
  `pip install "mcp==2.3.0"` and
  `python scripts/mcp_conformance.py --url https://gtm.infojoy.com/mcp --key nxk_...`. Expect PASS.
- **MCP Inspector**: `npx @modelcontextprotocol/inspector`, transport Streamable HTTP, URL
  `https://gtm.infojoy.com/mcp`, header `Authorization: Bearer <key>`; list tools, call
  `get_workspace_overview`.
- **One OAuth client**: Claude, Settings, Connectors, Add custom connector, paste the URL, Connect,
  approve. The consent screen must show the right app name and redirect host, and the connection
  must appear in InfoJoy GTM, Settings, AI apps. Revoke it there and confirm Claude asks to sign in
  again.

## 4. Prepare what every portal asks for

Have all of it before opening any form.

- **Public pages on `gtm.infojoy.com`** (every portal checks them, several require the same
  domain as the server): privacy policy (data collected, how it is used and stored, third-party
  sharing including "data you read through a connected AI app is sent to that app and its AI
  provider", retention, contact), terms of service, a support page or monitored address, and the
  setup guide (`docs/mcp/setup-guide.md`) published as a page.
- **Icons**: a square PNG at 512x512 or larger (Anthropic, Google, Mistral); ChatGPT accepts
  PNG/JPEG/WebP/SVG, square, at least 48x48, a `logo` and a `composerIcon`, optional dark variants;
  Microsoft wants the Teams pair (color 192x192, outline 32x32 white on transparent).
- **Names and descriptions**: InfoJoy GTM. Short (30 characters, ChatGPT's limit): "B2B signals,
  research, outreach". One-liner (200, Anthropic): "B2B buying signals, account research,
  contacts and outreach drafts from your InfoJoy GTM workspace, acting as you." Long: adapt the
  plugin README's first three sections. Categories: Sales, Productivity.
- **A reviewer workspace on production**: a dedicated account with **no MFA** (no reviewer can
  receive a code), the owner role, real populated data (accounts with signals, contacts with
  emails, an inbox, a list, a campaign, a connected mailbox for campaign drafts), and credits.
  Credentials go only into each portal's private reviewer field, never into a file or ticket.
- **A demo video** (required by ChatGPT): about a minute; connect, approve, "What should I work on
  today?", research an account, draft an email, create a campaign draft.
- **Screenshots**: at least three of InfoJoy GTM answering in Claude or ChatGPT.
- **Test cases** (ChatGPT asks for exactly five positive and three negative; reuse elsewhere):
  - Positive: (1) "What should I work on today?" -> `get_workspace_overview`, a short prioritized
    list with reasons. (2) "Research <seeded account>." -> `research_account`, a brief, mentions
    credits. (3) "Find contacts at <seeded account> and verify their emails." -> `find_contacts`,
    `verify_email`. (4) "Draft an email to <seeded contact> and save it to my drafts." ->
    `draft_email`, `save_email_draft`, stated as not sent. (5) "Make a campaign for my list
    <seeded list>." -> `create_campaign_draft`, a draft with opening emails written, launch left to
    the app.
  - Negative: (1) "Send this email now." -> explains it cannot send; offers Drafts. (2) "Launch the
    campaign." -> explains launching happens in the app after review. (3) With a Read-only
    connection, "Add Acme as an account." -> says the connection may not add records and where
    permissions change.

## 5. Anthropic: Claude Connectors Directory (claude.ai, desktop, mobile, Cowork, Claude Code)

Portal: `https://claude.ai/directory/manage`. **Anyone on a paid Claude plan** can submit
(requirements are in "Who can submit" on Anthropic's directory docs).

1. Test first as a custom connector (section 3) and call every tool once; the portal asks you to
   confirm this.
2. **Submit new**, choose **MCP connector**, then the ten steps:
   1. **Connection**: `https://gtm.infojoy.com/mcp`, one URL for all users.
   2. **Tools**: synced from the server, grouped read-only (11) and write (25). Every tool has a
      `title` and `readOnlyHint`/`destructiveHint`, so nothing should be flagged.
   3. **Listing**: name (100), one-liner (200), description (2,000), 1 to 5 categories,
      documentation URL, privacy policy URL, support contact, icon, URL slug (**permanent**).
   4. **Use cases**: triage signals and the inbox; research and draft outreach; build lists and
      campaign drafts; requires an InfoJoy GTM account; reads and writes.
   5. **Company**: InfoJoy, website, primary contact.
   6. **Authentication**: **OAuth with client ID metadata documents**. The server advertises
      `client_id_metadata_document_supported: true` and `none` among token auth methods; Dynamic
      Client Registration is also available as a fallback. Claude's callback
      `https://claude.ai/api/mcp/auth_callback` and Claude Code's loopback redirects already work.
      No Anthropic-held credentials are needed.
   7. **Data handling**: the API is our own; no personal health data; no sponsored content.
   8. **Test & launch**: the reviewer account and exact steps (connect, sign in, Allow with all
      three permissions, then the five positive prompts).
   9. **Compliance**: seven acknowledgements. Facts: credits are spent only on research already
      sold in-app (no financial transactions through tools); no AI media generation; tool results
      mark third-party text as data (prompt injection); no conversation data collection; public
      documentation exists.
   10. **Review and submit**.
3. Anthropic scans the submission automatically and lists it as a **Community** connector; some
   get a person's review. Status and feedback appear in the portal. Escalations:
   `mcp-review@anthropic.com`. Verified status is a later step (Anthropic's "Connector
   verification" docs).

**Plugin directory** (adds the three skills on claude.ai, Cowork and Claude Code): a public
GitHub repository holding `distribution/claude-plugin/` (the root `.claude-plugin/marketplace.json`
and `infojoy-gtm/`), validated with a current Claude Code
(`npx @anthropic-ai/claude-code@latest plugin validate ./infojoy-gtm` and `... validate .`), then
**Submit new**, **Plugin bundle** in the same portal. Submit the connector too: the two are paired.
MCPB packages and desktop extensions are no longer accepted. Raise `version` in `plugin.json` on
every change.

## 6. OpenAI: ChatGPT (and Codex) plugin directory

The old app form is replaced by **plugins**: a ZIP whose `mcp.json` names the server.

1. **Eligibility**: an organization owner, or a member with **Apps Management Write**; individual
   or business **verification** done in organization settings.
2. **Test in Developer mode**: ChatGPT, Settings, Apps & Connectors, Advanced settings, Developer
   mode on, Create, URL `https://gtm.infojoy.com/mcp`, OAuth; try every tool.
3. **Build the ZIP**: a manifest with the listing (`displayName` and `shortDescription` at most 30
   characters each, `longDescription` up to 4,000; HTTPS `websiteURL`, `supportURL`,
   `privacyPolicyURL`, `termsOfServiceURL`; `logo` and `composerIcon`; up to three starter
   prompts of 128 characters) and `mcp.json` with the one server. No server headers (public
   submissions reject them), no secrets, no `test_credentials`.
4. Platform, **Plugins**, **Upload new or existing plugin**, choose the verified identity, upload,
   choose **With MCP**.
5. **Domain verification**: the portal shows a token. Paste it into Control plane, Configuration,
   Access & security, **ChatGPT domain check token**; it is served as plain text at
   `https://gtm.infojoy.com/.well-known/openai-apps-challenge`. Check with
   `curl https://gtm.infojoy.com/.well-known/openai-apps-challenge`, then continue.
6. **Authentication**: OAuth with **Dynamic Client Registration** or **CIMD** (both supported);
   register OpenAI's callback if you choose a registered client instead (section 7 shows how to
   create one; its secret goes in the portal, never in `mcp.json`).
7. **Review details**: the reviewer account (no MFA, no magic links, no private network), the five
   positive and three negative cases (section 4), and the video URL.
8. Submit. One review per plugin at a time; appeal by replying to the rejection email. After
   approval you choose when to publish. Server-side tool changes are rescanned daily and go live
   after checks (or press **Rescan**); new tools stay unavailable until approved. Changing the
   server URL needs OpenAI support.

## 7. Microsoft: Copilot Studio, Microsoft 365 Copilot, Copilot chat, Azure Foundry, Cowork

Two separate things:

- **Any customer can add the server themselves, no certification**: Copilot Studio, an agent,
  Tools, Add a tool, Model Context Protocol, server URL `https://gtm.infojoy.com/mcp`,
  authentication OAuth 2.0. Copilot Studio shows a per-connector callback
  (`https://global.consent.azure-apim.net/redirect/<connector-name>`): register exactly that URL on
  an InfoJoy GTM client (below) and paste its client id and secret back.
- **Certification** (listed for every tenant, *preview*): Partner Center offer type **Apps and
  Agents for M365 and Copilot**. The legacy Power Platform connector route stays open until
  **2026-10-31**.

Microsoft does **not** support Dynamic Client Registration, so it needs a confidential client:

1. Create it (superadmin with `providers.manage`; there is no screen yet):
   ```
   POST https://gtm.infojoy.com/api/admin/oauth-clients
   Authorization: Bearer <superadmin session token>
   {"name": "Microsoft Copilot", "redirect_uris": ["https://global.consent.azure-apim.net/redirect/<connector-name>"]}
   ```
   The response shows `client_id` (`nxc_...`) and `client_secret` (`nxs_...`) **once**. The token
   endpoint accepts `client_secret_basic` and `client_secret_post`.
2. **Partner Center**: a seller account with business verification, enrolled in the Microsoft 365
   and Copilot program (verified publisher; you must own the endpoint, which we do).
3. **Azure Key Vault** in our tenant with case-sensitive secrets `ClientId`, `ClientSecret`,
   `AuthorizationUrl` (`https://gtm.infojoy.com/oauth/authorize`), `TokenUrl`
   (`https://gtm.infojoy.com/oauth/token`), `RefreshUrl` (the token URL) and `Scopes`
   (`nexus.read nexus.research nexus.write offline_access`). Grant the Microsoft application
   service principal `8e91e74f-afe9-41cd-8c3f-17a9562a74ea` **Key Vault Secrets User**.
4. **Package**: a Teams-schema manifest (v1.30) whose `agentConnectors[].toolSource.remoteMcpServer`
   names `mcpServerUrl` and `authorization: {"type": "AzureKeyVault", "referenceId":
   "https://<vault>.vault.azure.net/"}`, an `mcptools.json` tool file, `intro.md` (with a "Known
   issues and limitations" section), color and outline icons, and the support, privacy and terms
   links. ASCII only in the manifest and tool file.
5. Submit the offer; Microsoft runs automated validation, then a functional and safety review
   (evaluation evidence speeds it up). Resubmit when tools or metadata change.

## 8. Google: Gemini Enterprise, the Gemini app, Gemini CLI

Google has **no public directory submission** for MCP servers today; customers add the server.

- **Gemini Enterprise** (*preview*): Google Cloud console, Gemini Enterprise, **Data stores**,
  **Create data store**, search **Custom MCP Server**, **Add MCP server**, URL
  `https://gtm.infojoy.com/mcp`, authentication **OAuth 2.0**: create a client as in section 7
  with the redirect URL the form shows, and enter its id and secret and our authorize and token
  URLs. Then open the data store's **Actions** tab, **Reload custom actions**, and enable the
  tools (none are enabled by default), and connect the data store to the app. Streamable HTTP
  only; Private Service Connect is not supported.
- **The Gemini app** (personal accounts): connected apps take a custom MCP server URL where the
  account is eligible; OAuth works as for Claude.
- **Gemini CLI / Antigravity CLI** (developers): `gemini mcp add --transport http infojoy-gtm
  https://gtm.infojoy.com/mcp`, or in settings use the key **`httpUrl`** (a plain `url` means SSE).
  An API key goes in `headers`. The Gemini CLI extensions gallery lists public GitHub repositories
  automatically and is not reviewed by Google.

## 9. Mistral: Vibe (formerly Le Chat)

A workspace **administrator** adds connectors (Work, Connectors): either from Mistral's curated
directory or **custom** by server URL. Custom connectors detect the authentication type from the
URL; ours resolves to OAuth through the discovery documents. To appear in the curated directory,
contact Mistral's partnerships team with the materials in section 4 (there is no self-serve form).

## 10. Perplexity

Paid plans (Pro, Max, Enterprise; Enterprise governed by workspace admins): web or desktop app,
Settings, **Connectors**, **+ Custom connector**, Streamable HTTP, OAuth. If Perplexity reports a
client registration error, create a client as in section 7 with the redirect URL it shows and
enter the id and secret. No public directory submission is offered.

## 11. xAI: Grok

- **grok.com**: Connectors, New connector, **Custom**, paste the server URL; OAuth.
- **xAI API** (Responses API and SDK "remote MCP tools"): the server URL plus
  `Authorization: Bearer <API key>` from Settings, AI apps.

## 12. The MCP Registry and the directories built on it (VS Code, GitHub Copilot, Cursor, others)

1. Install `mcp-publisher` (`brew install modelcontextprotocol/tap/mcp-publisher`, or a release
   binary from `github.com/modelcontextprotocol/registry`).
2. Generate an ed25519 key pair locally and add the DNS TXT record on `infojoy.com` that
   `mcp-publisher login dns --domain infojoy.com --private-key-file key.pem` asks for
   (`v=MCPv1; k=ed25519; p=<public key>`). Keep the private key out of git.
3. From `distribution/mcp-registry/`: `mcp-publisher validate` (if it names a newer `$schema`,
   update `server.json` to it), then `mcp-publisher publish`. A remote server is listed by its
   `remotes` entry; no package is needed.
4. Confirm: `curl "https://registry.modelcontextprotocol.io/v0/servers?search=com.infojoy/gtm"`.
5. Raise `version` in `server.json` whenever the listing changes.

Smithery, Glama, PulseMCP and Cursor's directory mostly index the registry or GitHub; claim the
listings once they appear. Developer tools connect directly too (setup guide): Claude Code
(`claude mcp add --transport http infojoy-gtm https://gtm.infojoy.com/mcp`), Cursor and VS Code
(`mcp.json`), and model APIs (OpenAI Responses `type: "mcp"`, Anthropic's MCP connector) with an
API key.

## 13. After listing

- Keep tool names, titles and annotations stable: adding a tool is fine (ChatGPT holds new tools
  until approved; Microsoft wants a resubmission), renaming one breaks saved workflows.
- Watch `nexus_mcp_requests_total`, `nexus_mcp_tool_calls_total` and `nexus_oauth_events_total` on
  `/metrics`; a spike in `unauthorized` or `refused` is the first sign a client changed.
- A new production address means a new `app_base_url`, new URLs in the plugin and registry, and a
  resubmission in every portal (ChatGPT needs support to change it).
- Incidents: Control plane, Feature switches, `module.mcp` to `maintenance` stops every tool call
  with your message while connections stay in place.

### Sources (checked 2026-10-21)

- Anthropic: [Submit a connector to the directory](https://claude.com/docs/connectors/building/submission)
- OpenAI: [Upload and submit your plugin](https://developers.openai.com/apps-sdk/deploy/submission), [Submitting apps to the ChatGPT app directory](https://help.openai.com/en/articles/20001040-submitting-apps-to-the-chatgpt-app-directory)
- Microsoft: [MCP server certification (preview)](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-certification), [legacy certification](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-server-certification)
- Google: [Set up a custom MCP server (Gemini Enterprise)](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server), [Gemini CLI extensions](https://geminicli.com/extensions/)
- Mistral: [MCP connectors (Vibe)](https://docs.mistral.ai/vibe/work/connectors/mcp-connectors.md), [Configuring a custom connector](https://help.mistral.ai/en/articles/393572-configuring-a-custom-connector)
- MCP Registry: [modelcontextprotocol/registry](https://github.com/modelcontextprotocol/registry)
- Perplexity and xAI have no public submission process; their custom-connector steps above come
  from their apps' own settings screens and current third-party setup guides, so confirm in the
  app when you do it.
