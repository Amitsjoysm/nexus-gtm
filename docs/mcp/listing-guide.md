# Listing InfoJoy GTM's MCP server with every major AI provider

Internal guide for the product owner. Section 0 answers "plugin or connector?" platform by
platform; sections 1 to 4 put the server on production and prepare what every portal asks for;
sections 5 to 13 are one directory each; section 14 is the listing repository; section 15 is
upkeep. Checked against each provider's own documentation on **2026-10-23**. Portals change
often: read each form's own instructions as you go, and treat anything marked *preview* as subject
to change.

What is listed is one thing: the remote MCP server at `https://gtm.infojoy.com/mcp` (code:
`nexus/mcp/`, spec `docs/superpowers/specs/2026-10-13-mcp-server-design.md`). It speaks Streamable
HTTP (the transport every provider below requires), signs users in with its own OAuth 2.1 server
(PKCE, Dynamic Client Registration, Client ID Metadata Documents), and also takes per-user API
keys for developer tools. Every package in `distribution/` points at that one address.

---

## 0. Plugin or connector? Platform by platform

It depends on the platform, and on two of them (Anthropic, OpenAI) the answer is "the server is
the connector, and a plugin carries it". Everything a portal reads from a file is built in
`distribution/`; everything else is typed into the portal from section 4.

| Platform | What the listing is called there | What is submitted | Built here | Gated on (owner) |
|---|---|---|---|---|
| **Anthropic**: claude.ai, desktop, mobile, Cowork, Claude Code | Two listings, paired: an **MCP connector** (the remote server, by URL) and a **plugin** ("Plugin bundle": skills plus the connector reference). Anthropic asks for both when you run the server. | Connector: the URL and listing fields, in the developer portal. Plugin: a folder in a GitHub repository, public before it goes live. | `distribution/plugins/infojoy-gtm/` and `.claude-plugin/marketplace.json`; passes `claude plugin validate` (Claude Code 2.1.309) with no directory findings | A paid Claude plan (Owner on Team/Enterprise); production deploy; reviewer workspace; the public listing repository; GitHub connected on claude.ai |
| **OpenAI**: ChatGPT | A **plugin** "With MCP" (the earlier "app" submission is replaced by plugins) | A ZIP: `plugin.json`, `mcp.json`, skills, icons | `distribution/openai/`; `scripts/build.py` makes `dist/infojoy-gtm-openai-1.0.0.zip`; valid against the Agent Plugins 1.0.0 schema; 5 positive and 3 negative test cases | Verified OpenAI organization; demo video; reviewer workspace; domain check token |
| **Microsoft**: Copilot Studio, Microsoft 365 Copilot, Copilot chat, Azure Foundry, Cowork | A **certified MCP server**, through the Partner Center offer type "Apps and Agents for M365 and Copilot" (*preview*). Without certification any customer can still add it as an MCP **tool** in Copilot Studio. | A ZIP: Teams v1.30 manifest with an `agentConnectors` entry, `mcptools.json`, `intro.md`, color and outline icons; OAuth client in Azure Key Vault | `distribution/microsoft/`; `scripts/build.py` makes `dist/infojoy-gtm-microsoft-1.0.0.zip`; manifest valid against the Teams v1.30 schema; `mcptools.json` generated from the tool catalogue | Partner Center seller account with business verification, enrolled in the Microsoft 365 and Copilot program; Azure Key Vault; a confidential OAuth client |
| **Google**: Gemini Enterprise, the Gemini app | A **custom MCP server** (a data store) that each customer adds. No public directory submission. | Nothing | The public guide covers it | Nothing |
| **Google**: Gemini CLI | An **extension**, listed in the extensions gallery automatically | A public GitHub repository with `gemini-extension.json` at its root and the `gemini-cli-extension` topic | `distribution/gemini-extension.json`, `distribution/GEMINI.md` | The public listing repository |
| **MCP Registry**, and what reads it: GitHub's MCP registry, VS Code's MCP gallery, other directories | A **server entry** (`server.json`) | `mcp-publisher publish` | `distribution/server.json`, fixed (its description was 137 characters against the schema's 100, so publishing would have been refused); valid against the 2025-12-11 schema | A DNS TXT record on `infojoy.com`; running `mcp-publisher` |
| **Mistral**: Vibe | A **connector**: custom by URL (workspace admin), or the curated directory by partnership | An email to Mistral's partnerships team | The public guide covers custom setup | A partnership contact |
| **Perplexity** | A **custom connector** a user adds; no public submission | Nothing | The public guide covers it | Nothing |
| **xAI**: Grok | A **custom connector** on grok.com; "remote MCP tools" in the API | Nothing | The public guide covers it | Nothing |
| **Cursor, VS Code, Claude Code, model APIs** | Server **configuration** (`mcp.json`, `claude mcp add`, an API tool entry) | Nothing | The public guide covers it | Nothing |

**A separate public repository is the right home for the listings.** Anthropic reads plugins only
from a GitHub repository that must be public before the listing goes live, the Gemini CLI gallery
lists only public repositories, and the MCP Registry recommends naming one. This repository holds
the product's source and must stay private. So `distribution/` is laid out exactly as that
repository and exported to it (section 14); it is prepared locally at
`C:\Users\Amit Singh\Projects\infojoy-gtm-mcp` with its own git history, and is not on GitHub.

### Built on branch `feat/mcp-listings` (2026-10-23)

- **A public setup guide** at `https://gtm.infojoy.com/help/ai-apps`, rendered on the server from
  `nexus/mcp/help/ai-apps.md` (`nexus/mcp/guide.py`), with a `#support` section. Every listing
  uses it as the documentation and support address, and the OAuth discovery documents now point
  their `resource_documentation` / `service_documentation` at it instead of the signed-in
  Settings page. Before this there was no public documentation page at all: `/privacy`, `/terms`
  and `/docs/ai-apps` on `gtm.infojoy.com` all fell through to the app's catch-all and landed on
  the dashboard or the login screen.
- **Legal links**: `https://infojoy.com/legal.html#privacy` and `#terms` (the owner's choice,
  2026-10-23), in every manifest. The plugin README used `https://gtm.infojoy.com/privacy`, which
  is not a page.
- **Icons** (`distribution/assets/`, made by `scripts/listing_icons.py` from the brand mark): 512
  square light and dark, the Teams 192 color and 32 outline pair, and the Claude plugin's
  `.claude-plugin/icon.png`.
- **Packages** for OpenAI and Microsoft, the Gemini CLI extension, the fixed registry entry, and a
  checker (`distribution/scripts/check.py`) that enforces every limit below and runs in this
  suite (`tests/test_mcp_distribution.py`) and in the listing repository's CI.

## 1. Where things stand (2026-10-23)

Tested end to end on the local stack on 2026-10-21, which runs the same MCP code as production:

| Check | Result |
|---|---|
| Discovery (`/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`) | correct on local and on **production** (`gtm.infojoy.com`) |
| Unauthenticated `POST /mcp` | `401` with `WWW-Authenticate: Bearer resource_metadata=...`, on local and production |
| OAuth sign-in (DCR, PKCE S256, consent, code exchange, refresh rotation, replay refused) | every step passes |
| Official MCP client v2.3.0, both protocol dialects | PASS |
| Every tool, called for real | works; refusals (no mailbox, unknown id, provider down) come back as sentences |
| Public guide `/help/ai-apps` | built on `feat/mcp-listings`; not deployed |
| **Staging** `staging-gtm.infojoy.com` | **did not resolve (NXDOMAIN)** on 2026-10-21. The app is healthy at `gtm-staging-app.mangowater-46a1ec4f.eastus2.azurecontainerapps.io`; add the DNS record (section 2) before testing cloud clients on staging |

If an AI app "doesn't work", the usual causes, in order:

1. **A local or private address.** Claude, ChatGPT, Copilot, Gemini and Perplexity connect from
   their own servers on the internet: `localhost`, `:8099` or a VPN address cannot work for them.
2. **The address does not match the App public address.** OAuth names `{app_base_url}/mcp` as the
   resource; a client that connected through any other host or port fails the resource check
   after sign-in.
3. **Something in front of the app** (WAF, Front Door, a proxy) blocking or rewriting `/mcp`,
   `/oauth/*`, `/.well-known/*` or `/help/*`, or blocking the provider's egress addresses.
4. **AI apps switched off**: Control plane, Feature switches, `module.mcp`.

### What the tools cover

36 tools since 2026-10-19. A connection sees only the ones its permissions allow: Read shows 11,
all three show 36, under the ~40 tools some clients cap across every connected server.

| Permission | Tools |
|---|---|
| Read (free) | `search`, `fetch` (account, contact, list, campaign, reply, call, person, populate), `list_signals`, `list_inbox`, `list_lists`, `list_campaigns`, `get_credit_balance`, `prospector_search`, `get_workspace_overview`, `list_queue` (alerts, replies, calls), `search_network` |
| Research (spends credits, priced from the rate cards) | `find_contacts`, `find_similar_companies`, `enrich_contact`, `research_account`, `draft_email`, `enrich_account`, `verify_email`, `find_phone`, `refresh_account`, `find_similar_people`, `add_icp_companies`, `draft_reply`, `create_campaign_draft` |
| Write | `create_account`, `create_list`, `add_to_list`, `remove_from_list`, `add_contacts_to_campaign`, `complete_inbox_task`, `save_email_draft`, `prospector_save`, `acknowledge_alert`, `save_reply_draft`, `queue_call`, `log_call_outcome` |

Never from an AI app (a test refuses any tool path that reaches them): sending email, approving or
launching a campaign, dialing, deleting or archiving records, changing owners, billing, settings,
members, integrations.

## 2. Put it on production

1. **Merge `feat/mcp-listings` after review** (the public guide and the discovery documentation
   links; no migration). Run the full CI-equivalent in `CLAUDE.md` on the exact merge commit.
2. **Deploy** through the usual pipeline.
3. **App public address**: Control plane, Configuration, Billing, **App public address** =
   `https://gtm.infojoy.com` (an origin, no path). Without it the OAuth metadata names whatever
   host a request came in on, and Claude and ChatGPT require the resource to match exactly.
   Already right on production on 2026-10-23: `check.py --online` found the protected-resource
   `resource` exactly `https://gtm.infojoy.com/mcp` and the 401 challenge in place.
4. **`module.mcp` enabled** (Control plane, Feature switches; on by default, on no plan).
5. **Staging DNS**: create `staging-gtm.infojoy.com` (CNAME to the staging container app host,
   then bind the custom domain and certificate on `gtm-staging-app`) and set staging's App public
   address to `https://staging-gtm.infojoy.com`.
6. **Reachability**: nothing in front of the app may block or rewrite `/mcp`, `/oauth/*`,
   `/.well-known/*` or `/help/*`. Allow the providers' egress (Anthropic publishes
   `160.79.104.0/21`; OpenAI, Microsoft and Google connect from their clouds, so do not IP-filter
   these paths at all unless a provider publishes its range). No buffering proxy on `/oauth/token`
   (Claude allows 10 seconds).

Then, from the listing repository: `python scripts/check.py --online`. It opens the privacy and
terms anchors, the public guide and its `#support` anchor, the website and the registry icon,
checks the protected-resource `resource` is exactly `https://gtm.infojoy.com/mcp`, and that an
unauthenticated `POST /mcp` answers 401 with `resource_metadata`. A page must carry its own
anchor, not only answer 200: the app answers any unknown path with its shell page and a 200. On
2026-10-23, before this branch is deployed, it passes everything except the guide.

## 3. Prove it works on production

- **Official client, both dialects** (own virtual environment; spends nothing, changes nothing):
  create a Read-only API key in Settings, AI apps, then `pip install "mcp==2.3.0"` and
  `python scripts/mcp_conformance.py --url https://gtm.infojoy.com/mcp --key nxk_...`. Expect PASS.
- **MCP Inspector**: `npx @modelcontextprotocol/inspector`, transport Streamable HTTP, URL
  `https://gtm.infojoy.com/mcp`, header `Authorization: Bearer <key>`; list tools, call
  `get_workspace_overview`.
- **One OAuth client**: Claude, Settings, Connectors, Add custom connector, paste the URL, Connect,
  approve. The consent screen must show the right app name and redirect host, and the connection
  must appear in InfoJoy GTM, Settings, AI apps. Revoke it there and confirm Claude asks to sign in
  again. Every directory below asks you to confirm you called every tool this way first.

## 4. Prepare what every portal asks for

Have all of it before opening any form. What is built is in `distribution/`; the rest is yours.

| Item | Status |
|---|---|
| Documentation URL | `https://gtm.infojoy.com/help/ai-apps` (after the deploy) |
| Support | `https://gtm.infojoy.com/help/ai-apps#support`, which names **hello@marketjoy.com** (confirmed by the owner, 2026-10-26). Portals asking for a support email get the same address; to change it, edit `nexus/mcp/help/ai-apps.md`. |
| Privacy policy, terms | `https://infojoy.com/legal.html#privacy`, `#terms`. **Needs your legal review before submitting**: the policy (MarketJoy, Inc., undated; the terms say 22 May 2023) names neither InfoJoy GTM nor AI apps, and Anthropic and OpenAI reviewers check that it covers what a connected app receives. The section to add, ready to paste after counsel reads it, with the facts to re-check and a one-sentence Terms exception (the terms forbid "intelligent agents" on the Website): `docs/mcp/privacy-policy-ai-apps.md`. |
| Website | `https://infojoy.com/gtm.html` |
| Icons | Built (`distribution/assets/`). The 32px outline is mechanically derived: a designer may want to redraw it. |
| Names and descriptions | Below; they are in the manifests already |
| Reviewer workspace | Yours to create: `docs/mcp/reviewer-workspace.md` |
| Demo video | Yours to record: `docs/mcp/demo-video.md` (required by ChatGPT) |
| Screenshots | At least three of InfoJoy GTM answering in Claude or ChatGPT; record them with the video |
| Test cases | Five positive, three negative, in `distribution/openai/plugin.json` (`review.test_cases`); reuse them in every portal's test instructions |

**Names and descriptions** (every limit is enforced by `check.py`):

| Field | Text | Limit |
|---|---|---|
| Name | InfoJoy GTM | 30 (ChatGPT, Teams) |
| Short | Signals, research and outreach | 30 (ChatGPT `shortDescription`) |
| One-liner (Anthropic) | B2B buying signals, account research, contacts and outreach drafts from your InfoJoy GTM workspace, acting as you. | 200 |
| Registry description | B2B buying signals, account research, contacts and outreach drafts, acting as you. | 100 |
| Long | ChatGPT `longDescription` in `distribution/openai/plugin.json`; Teams `description.full` | 2,000 (Anthropic), 4,000 (ChatGPT, Teams) |
| Categories | Sales; Productivity (ChatGPT's manifest uses Productivity) | 1-5 (Anthropic) |
| Use cases (Anthropic) | Triage signals and the inbox; research accounts and draft outreach; build lists and campaign drafts. Requires an InfoJoy GTM account. Reads and writes. | |

## 5. Anthropic: an MCP connector, and a plugin paired with it

Portal: `https://claude.ai/directory/manage`. **Pro, Max, Team or Enterprise** (Free cannot); on
Team and Enterprise an **Owner** submits, or a member given the Directory permission. The listing
belongs to the organization you submit from, and for a plugin the first organization to submit a
repository folder holds it: submit from the one that should own it long term.

**First, the connector** (Submit new, **MCP connector**):

1. Test as a custom connector (section 3) and call every tool once; the portal asks you to confirm.
2. **Connection**: `https://gtm.infojoy.com/mcp`, one URL for all users.
3. **Tools**: synced from the server, grouped read-only (11) and write (25). Every tool has a
   `title` and `readOnlyHint`/`destructiveHint`, so nothing is flagged.
4. **Listing**: name, one-liner, description, categories (section 4), documentation URL, privacy
   policy URL, support contact, icon (`distribution/assets/icon.png`), URL slug (**permanent**:
   `infojoy-gtm` suggested).
5. **Use cases**, **Company** (InfoJoy, `https://infojoy.com/gtm.html`, a primary contact).
6. **Authentication**: OAuth, users sign in **Required**, method **client ID metadata documents**.
   The server advertises `client_id_metadata_document_supported: true` and `none` among token auth
   methods; Dynamic Client Registration is also available. Claude's callback
   `https://claude.ai/api/mcp/auth_callback` and Claude Code's loopback redirects already work.
   No Anthropic-held credentials are needed.
7. **Data handling**: the API is our own; no personal health data; no sponsored content.
8. **Test & launch**: the reviewer account (entered here only) and the steps: connect, sign in,
   Allow with all three permissions, then the five positive prompts.
9. **Compliance**: seven acknowledgements. Facts: credits are spent only on research already sold
   in-app (no financial transactions through tools); no AI media generation; tool results mark
   third-party text as data (prompt injection); no conversation data collection; public
   documentation exists.
10. **Review and submit**. Anthropic scans it and lists it as a **Community** connector; some get
    a person's review. Escalations: `mcp-review@anthropic.com`. Verified status is decided by
    Anthropic during review; there is no separate application.

**Then, the plugin** (Submit new, **Plugin bundle**), once the listing repository is on GitHub:

1. Connect GitHub on claude.ai in the same organization; your GitHub account needs push access.
   The repository may stay private while validating (install the Claude GitHub App and agree to
   the source upload) and must be public before the listing goes live.
2. **Source**: the repository, **Plugin path** `plugins/infojoy-gtm`, branch `main` (or a tag).
   **Validate**; it runs more than `claude plugin validate`. Locally, from the repository root:
   `npx @anthropic-ai/claude-code@latest plugin validate ./plugins/infojoy-gtm` and
   `... plugin validate .` (the marketplace). Both pass today. Older Claude Code builds reject
   `displayName` and `privacyPolicyUrl`; the directory does not.
3. **Listing details** come from `plugin.json` and the README. The listing **icon** is taken once,
   from `.claude-plugin/icon.png`, the first time the plugin is saved or submitted.
4. **Data handling**: the plugin reads no personal data itself and stores nothing; it sends data
   only to its declared connector; it keeps nothing; not intended for people under 18.
5. **Compliance**: the contact email and four acknowledgements.
6. **Review and submit**: keep **GitHub push webhook** (you need admin on the repository). A person
   reviews a new listing; select **Publish** when it passes. Later versions are picked up from the
   tracked branch: raise `version` in `plugin.json` with every release.
7. **Pair** the plugin with the connector in the portal (both from the same organization). Both
   point at the same URL, so people who add both see one set of tools.
8. Once the repository's owner is known, add the install line the validator suggests to the
   plugin README: `/plugin install infojoy-gtm --marketplace <owner>/infojoy-gtm-mcp`.

MCPB packages and desktop extensions are no longer accepted.

## 6. OpenAI: a ChatGPT plugin "With MCP"

1. **Eligibility**: an organization owner, or a member with **Apps Management Write**; individual
   or business **verification** done in organization settings (needed to publish under InfoJoy).
2. **Test in Developer mode**: ChatGPT, Settings, Apps & Connectors, Advanced settings, Developer
   mode on, Create, URL `https://gtm.infojoy.com/mcp`, OAuth; try every tool.
3. **Build the ZIP**: from the listing repository, `python scripts/build.py --demo-url <video>`
   (`dist/infojoy-gtm-openai-1.0.0.zip`). It holds the manifest with every listing field and the
   test cases, `mcp.json` with the one server, the three skills and the icons; no headers, no
   secrets, no `test_credentials`.
4. Platform, **Plugins**, **Upload new or existing plugin**, choose the verified identity, upload,
   choose **With MCP**. Fix any **Metadata & Skills** findings and re-upload.
5. **Domain verification**: the portal shows a token. Paste it into Control plane, Configuration,
   Access & security, **ChatGPT domain check token**; it is served as plain text at
   `https://gtm.infojoy.com/.well-known/openai-apps-challenge`. Check with
   `curl https://gtm.infojoy.com/.well-known/openai-apps-challenge`, then continue.
6. **Authentication**: OAuth with **Dynamic Client Registration** or **CIMD** (both supported).
   Nothing goes in `mcp.json` for either.
7. **Review details**: the reviewer account (no MFA, no magic links, no private network), the test
   cases (pre-filled from the manifest), the video URL, release notes.
8. **Submit for review** and the policy attestations. One review per plugin at a time; appeal by
   replying to the rejection email. After approval you choose when to **Publish plugin**.
   Server-side tool changes are scanned daily (or press **Rescan**); new tools are held until
   approved. Metadata, assets or skill changes need a new ZIP and review. Changing the server URL
   needs OpenAI support.

## 7. Microsoft: certified MCP server

Two separate things:

- **Any customer can add the server themselves, no certification**: Copilot Studio, an agent,
  Tools, Add a tool, Model Context Protocol, server URL `https://gtm.infojoy.com/mcp`,
  authentication OAuth 2.0. Copilot Studio shows a per-connector callback
  (`https://global.consent.azure-apim.net/redirect/<connector-name>`): register exactly that URL on
  an InfoJoy GTM client (below) and paste its client id and secret back.
- **Certification** (listed for every tenant, *preview*): Partner Center offer type **Apps and
  Agents for M365 and Copilot**. The legacy Power Platform connector route closes on
  **2026-10-31**; this package is for the new route.

Microsoft does **not** support Dynamic Client Registration, so certification needs a confidential
client:

1. Create it (superadmin with `providers.manage`; there is no screen yet):
   ```
   POST https://gtm.infojoy.com/api/admin/oauth-clients
   Authorization: Bearer <superadmin session token>
   {"name": "Microsoft Copilot", "redirect_uris": ["https://global.consent.azure-apim.net/redirect/<connector-name>"]}
   ```
   The response shows `client_id` (`nxc_...`) and `client_secret` (`nxs_...`) **once**. The token
   endpoint accepts `client_secret_basic` and `client_secret_post`. Ask Microsoft for the redirect
   URL their validation uses if the form does not show one.
2. **Partner Center**: a seller account with business verification, enrolled in the Microsoft 365
   and Copilot program (verified publisher; we own the endpoint).
3. **Azure Key Vault** in our tenant. The package names `https://infojoy-gtm-mcp-kv.vault.azure.net/`;
   if that name is taken, use another and build with `--key-vault https://<name>.vault.azure.net/`.
   Case-sensitive secrets: `ClientId`, `ClientSecret`, `AuthorizationUrl`
   (`https://gtm.infojoy.com/oauth/authorize`), `TokenUrl` (`https://gtm.infojoy.com/oauth/token`),
   `RefreshUrl` (the token URL) and `Scopes` (`nexus.read nexus.research nexus.write
   offline_access`). Grant the Microsoft application service principal
   `8e91e74f-afe9-41cd-8c3f-17a9562a74ea` **Key Vault Secrets User**.
4. **Package**: `python scripts/build.py` (`dist/infojoy-gtm-microsoft-1.0.0.zip`): the v1.30
   manifest (app id `dbe10578-80dc-5d99-981a-b4dccff7bab8`, derived from the server URL, keep it
   for every resubmission), `mcptools.json`, `intro.md` with "Known issues and limitations", the
   color and outline icons. ASCII only. Microsoft's certification page shows a `version` of "1.30"
   and a `developer.contactInfo` block; the v1.30 schema has neither (`manifestVersion` is 1.30,
   `version` is ours), so the support emails go in the Partner Center offer instead.
5. Submit the offer; automated validation, then a functional and safety review (evaluation
   evidence speeds it up: the conformance run and the test cases). Resubmit when tools or metadata
   change: `python scripts/export_mcp_tools.py` here regenerates `mcptools.json` and a test fails
   until you do.

## 8. Google: Gemini Enterprise, the Gemini app, Gemini CLI

Google has **no public directory submission** for remote MCP servers; customers add the server.

- **Gemini Enterprise** (*preview*): Google Cloud console, Gemini Enterprise, **Data stores**,
  **Create data store**, **Custom MCP Server**, URL `https://gtm.infojoy.com/mcp`, **OAuth 2.0**:
  create a client as in section 7 with the redirect URL the form shows, and enter its id, secret,
  and our authorize and token URLs. Then the data store's **Actions** tab, **Reload custom
  actions**, enable the tools (none are on by default), and connect the data store to the app.
- **The Gemini app**: connected apps take a custom MCP server URL where the account is eligible.
- **Gemini CLI** (Google moved unpaid and Google One users to Antigravity CLI on 2026-06-18):
  `gemini mcp add --transport http infojoy-gtm https://gtm.infojoy.com/mcp`, or the
  **extension**: the listing repository's root `gemini-extension.json` (server under `httpUrl`;
  a plain `url` would mean SSE) and `GEMINI.md`. The gallery crawls public repositories carrying
  the GitHub topic **`gemini-cli-extension`** daily and lists those that pass validation; there is
  no form and no review. Users install with `gemini extensions install <repository URL>`.

## 9. Mistral: Vibe (formerly Le Chat)

A workspace **administrator** adds connectors (Work, Connectors): from Mistral's curated directory
or **custom** by server URL; ours resolves to OAuth through the discovery documents. To be in the
curated directory, contact Mistral's partnerships team with the materials in section 4.

## 10. Perplexity

Paid plans (Pro, Max, Enterprise; Enterprise governed by workspace admins): Settings,
**Connectors**, **+ Custom connector**, Remote, Streamable HTTP, OAuth. If Perplexity reports a
client registration error, create a client as in section 7 with the redirect URL it shows. No
public directory submission is offered (checked 2026-10-23: only third-party guides).

## 11. xAI: Grok

- **grok.com**: Connectors, New connector, **Custom**, the server URL; OAuth.
- **xAI API** ("remote MCP tools"): the server URL plus `Authorization: Bearer <API key>`.

## 12. The MCP Registry and the directories built on it

GitHub's MCP registry and VS Code's MCP gallery (`chat.mcp.gallery.enabled`, `@mcp` in
Extensions) read the official registry; there is no separate GitHub or VS Code submission.

1. Install `mcp-publisher` (a release binary from `github.com/modelcontextprotocol/registry`, or
   `brew install mcp-publisher`).
2. Generate an ed25519 key pair locally and add the DNS TXT record on `infojoy.com` that
   `mcp-publisher login dns --domain infojoy.com --private-key-file key.pem` asks for
   (`v=MCPv1; k=ed25519; p=<public key>`). The listing repository ignores `*.pem` and the
   publisher's `.mcpregistry_*` token files; keep the key out of git anyway.
3. From the listing repository's root: `mcp-publisher validate`, then `mcp-publisher publish`
   (it reads `server.json`). A remote server is listed by its `remotes` entry; no package needed.
4. Confirm: `curl "https://registry.modelcontextprotocol.io/v0/servers?search=com.infojoy/gtm"`.
5. Raise `version` in `server.json` whenever the listing changes; a published version is final.
   Once the repository is public, add `"repository": {"url": "...", "source": "github"}`.

Smithery, Glama, PulseMCP and Cursor's directory mostly index the registry or GitHub; claim the
listings once they appear.

## 13. Developer tools and model APIs

Nothing to submit. The public guide gives the commands and files: Claude Code
(`claude mcp add --transport http infojoy-gtm https://gtm.infojoy.com/mcp`, or the plugin), Cursor
and VS Code (`mcp.json`), model APIs (OpenAI Responses `type: "mcp"`, Anthropic's MCP connector)
with an API key.

## 14. The listing repository

`distribution/` is the source of truth, and the public repository is its export:

| Path | For |
|---|---|
| `.claude-plugin/marketplace.json`, `plugins/infojoy-gtm/` | Anthropic's plugin directory; a Claude Code marketplace |
| `openai/` | ChatGPT (`scripts/build.py` adds the skills and icons into the ZIP) |
| `gemini-extension.json`, `GEMINI.md` | the Gemini CLI gallery (must be at the root) |
| `server.json` | the MCP Registry (`mcp-publisher` reads it from the root) |
| `microsoft/` | Microsoft certification |
| `assets/` | every icon |
| `scripts/check.py`, `scripts/build.py`, `.github/workflows/check.yml` | checks and packages, also in CI there |

To publish it (your go-ahead; nothing here is on GitHub yet):

1. Choose the owner: an `infojoy` organization reads best in every directory. Create an **empty
   public** repository `infojoy-gtm-mcp` (no README, no license: the export has both).
2. `cd C:\Users\Amit Singh\Projects\infojoy-gtm-mcp`, `git remote add origin <url>`,
   `git push -u origin main`.
3. In the repository's About, add the topics `gemini-cli-extension`, `mcp`, `model-context-protocol`
   and the website `https://gtm.infojoy.com/help/ai-apps`.
4. Add the plugin README's install line (section 5, step 8) and `repository` in `server.json`
   (section 12, step 5); raise nothing yet, nothing has been published.
5. Later changes: edit `distribution/` here (tests keep it in step with the server), then
   `python scripts/export_listing_repo.py --to "C:/Users/Amit Singh/Projects/infojoy-gtm-mcp"`,
   read `git status` there, commit and push. Anthropic picks up the tracked branch by itself;
   ChatGPT and Microsoft need a new ZIP and review; the registry needs a new `version` and
   `mcp-publisher publish`.

## 15. After listing

- Keep tool names, titles and annotations stable: adding a tool is fine (ChatGPT holds new tools
  until approved; Microsoft wants a resubmission with a regenerated `mcptools.json`), renaming one
  breaks saved workflows.
- Watch `nexus_mcp_requests_total`, `nexus_mcp_tool_calls_total` and `nexus_oauth_events_total` on
  `/metrics`; a spike in `unauthorized` or `refused` is the first sign a client changed.
- A new production address means a new `app_base_url`, new URLs in every package, and a
  resubmission in every portal (ChatGPT needs support to change it).
- Incidents: Control plane, Feature switches, `module.mcp` to `maintenance` stops every tool call
  with your message while connections stay in place.

### Sources (checked 2026-10-23)

- Anthropic: [Submit a connector](https://claude.com/docs/connectors/building/submission), [Submit your plugin](https://claude.com/docs/plugins/submit), [Plugin pre-submission checklist](https://claude.com/docs/plugins/pre-submission-checklist), [Publish to the directory](https://claude.com/docs/directory/publish)
- OpenAI: [Upload and submit your plugin](https://developers.openai.com/apps-sdk/deploy/submission)
- Microsoft: [MCP server certification (preview)](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-certification), [Build plugins for Copilot Cowork](https://learn.microsoft.com/en-us/microsoft-365/copilot/cowork/cowork-plugin-development), the [Teams v1.30 manifest schema](https://developer.microsoft.com/json-schemas/teams/v1.30/MicrosoftTeams.schema.json)
- Google: [Gemini CLI extensions](https://geminicli.com/docs/extensions/), [Releasing extensions](https://geminicli.com/docs/extensions/releasing/), [Configuration (`httpUrl`)](https://geminicli.com/docs/reference/configuration/), [Custom MCP server in Gemini Enterprise](https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server)
- MCP Registry: [modelcontextprotocol/registry](https://github.com/modelcontextprotocol/registry), the [2025-12-11 server schema](https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json); GitHub: [the GitHub MCP Registry](https://github.blog/ai-and-ml/github-copilot/meet-the-github-mcp-registry-the-fastest-way-to-discover-mcp-servers/)
- Mistral: [MCP connectors (Vibe)](https://docs.mistral.ai/vibe/work/connectors/mcp-connectors.md)
- Perplexity and xAI have no public submission process; their custom-connector steps come from
  their apps' settings screens and third-party setup guides, so confirm in the app.
