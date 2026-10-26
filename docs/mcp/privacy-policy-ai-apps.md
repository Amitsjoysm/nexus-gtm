# Privacy policy addition: AI assistants and connected apps

Text to add to the privacy policy at `https://infojoy.com/legal.html#privacy` so that it covers
what the AI app directories check before they list InfoJoy GTM. Written 2026-10-26 from what the
product does today (`nexus/mcp/`). Not legal advice: have counsel read it before publishing.

## Why it is needed

The current policy (MarketJoy, Inc.) does not mention InfoJoy GTM, AI assistants or connected
apps. Each directory reviews the policy a listing links to:

| Directory | What its review checks in the privacy policy |
|---|---|
| OpenAI (ChatGPT plugins) | Data categories, purposes of use, recipient categories, **retention timelines**, and user controls. A missing disclosure is a listed rejection reason, as is undisclosed collection of metadata such as IP addresses, timestamps or query patterns. |
| Anthropic (Claude directory) | A clear policy explaining data collection, usage and retention; collect only what the function needs; never take Claude's memory, chat history or conversation content, or build profiles from it. |
| Microsoft (MCP certification) | Privacy and terms links that stay accurate after certification. |

Every row below maps to one of those checks.

## Before you publish: check these are still true

The text states facts about the product. If any has changed, change the text, not just the code:

- [ ] Access tokens last 1 hour; refresh tokens up to 30 days and are replaced on every use
      (`oauth_access_token_ttl_s`, `oauth_refresh_token_ttl_days`). A sign-in link lasts 10
      minutes and a sign-in code 60 seconds.
- [ ] API keys and tokens are stored only as one-way hashes (`nexus/mcp/credentials.py`).
- [ ] The audit log records the tool, outcome and record identifiers of research and write
      actions, never the free text a model wrote (`nexus/mcp/server.py`, `_ids`).
- [ ] Revoked connections are kept with `revoked_at` set (hidden from the AI apps list, not
      deleted), and third-party text is cleaned and named as data in the server instructions
      (`nexus/mcp/safety.py`).
- [ ] Nothing purges connection or audit records automatically. **The 30-day deletion promise
      below is a commitment you make by hand** (on a request, or when a workspace closes) until a
      job does it. Change the number if 30 days is not one you can keep.
- [ ] Data from AI-app actions enters the "Help improve the AI" ledger only for workspaces that
      turned it on (the agents emit ledger events as they do for the app; `gtm.infojoy.com/data-use`
      describes it).
- [ ] hello@marketjoy.com is monitored (confirmed by the owner, 2026-10-26).

After the directories list InfoJoy GTM, **tell Anthropic and OpenAI before you change these
practices materially**: Anthropic's directory policy requires notice, and a change can trigger a
new review.

## Where to put it

As its own section inside the Privacy Policy on `legal.html`, ideally with its own anchor
(`<h3 id="ai-apps">`), so a reviewer who opens `legal.html#privacy` finds it under the privacy
heading. Everything between the two rules below is the text to paste; adjust the heading level to
match the page.

---

### AI assistants and connected apps (InfoJoy GTM)

InfoJoy GTM lets a signed-in user connect an AI assistant, such as Claude, ChatGPT, Microsoft
Copilot, Gemini, Cursor or VS Code, to their InfoJoy GTM workspace through our Model Context
Protocol server at `https://gtm.infojoy.com/mcp`. This section explains what happens to data when
you do. The rest of this Privacy Policy also applies to InfoJoy GTM.

**How a connection works.** You connect from the AI assistant. It sends you to InfoJoy GTM, where
you sign in and choose what the connection may do: Read (see workspace data), Research (run
actions that spend your workspace's credits, such as finding contacts or drafting an email) and
Write (add or change records, such as creating a list or saving a draft to your own Drafts
folder). You sign in on our page, so the AI assistant never receives your InfoJoy GTM password; it
receives an access token limited to the permissions you chose. A connection can never do more
than your own role in the workspace allows. It cannot send email, launch a campaign, place a call,
delete records, or change billing, members or settings.

**What we collect when you connect an app.**

- *The connection*: the app's name and the web addresses it registered, the user and workspace it
  belongs to, the permissions granted, and when it was created, last used and revoked.
- *Requests the app sends*: the tool it asked for and the inputs needed for that one task, for
  example search words, the identifier of an account or contact, or a short instruction for a
  draft. They are processed exactly as the same action in the InfoJoy GTM app, and kept only where
  that action keeps its result (a list you created, a draft you saved, research you ran).
- *Activity records*: for actions that spend credits or change data, the workspace audit log
  records which tool ran, when, its outcome, and the identifiers of the records it acted on. It
  does not record the text the assistant wrote. For every request we also keep counts of tool use
  and technical logs (time, address requested and result) for security and reliability; our
  hosting provider's logs may also record the network (IP) address a request came from.

**What we do not collect.** We do not receive or store your conversations with the AI assistant,
its memory, chat history or summaries, or files you share with it: only the requests the assistant
sends us to carry out a task. We never ask an AI assistant for payment card details, health
information, government identifiers or passwords. We do not use AI-app activity for advertising.

**How we use it.** To carry out each request as you, within your permissions and role; to check
and charge credits at the same prices as the app; to apply rate limits and keep the service
secure; to let you and your workspace administrators see and manage connected apps; and to
investigate misuse and fix faults. If your workspace has turned on "Help improve the AI with this
workspace's data" (see https://gtm.infojoy.com/data-use), actions run through an AI app are
treated like the same actions in the app under that setting. If it is off, they are not used to
improve our AI.

**Who receives it.**

- *The AI app you connected, and the AI provider behind it.* Whatever the app reads through the
  connection (for example company and contact details, buying signals, drafts and replies) is sent
  to that app and its AI model provider, and is processed under their terms and privacy policy, not
  ours. You choose which apps to connect.
- *Our service providers*, under the same terms as for the rest of InfoJoy GTM: hosting, the AI
  models that write drafts and research, and the data and verification services that power
  research actions.
- *Your workspace administrators*, who can see every connection in the workspace and its activity.

We do not sell this information.

**How long we keep it.**

- Access tokens expire after 1 hour. Refresh tokens expire after at most 30 days, are replaced
  each time they are used, and stop working as soon as a connection is revoked. Sign-in links
  expire after 10 minutes and sign-in codes after 60 seconds.
- Connection records and audit records are kept while the workspace exists, so administrators can
  review what each connection did. A revoked connection's record is kept, marked revoked.
- Records an app created or changed (lists, drafts, accounts) are kept like any other workspace
  data, until you or an administrator delete them.
- We delete connection and audit records within 30 days of a verified deletion request or of the
  workspace being closed, unless the law requires us to keep them longer.

**Your choices.**

- Choose what each connection may do when you connect, and change it at any time in InfoJoy GTM
  under Settings, AI apps.
- Revoke a connection or API key there; it stops working at once. Workspace administrators can
  revoke any connection in the workspace.
- To access, correct or delete your information, contact us at hello@marketjoy.com. To stop an AI
  app keeping data it already received, use that app's own settings or contact its provider.

**Security.** Connections use OAuth 2.1 with PKCE over HTTPS. API keys and tokens are stored only
as one-way hashes, so they cannot be read back from our database; a refresh token used twice
revokes the whole sign-in. Every request is checked against the user's current role and the
connection's permissions. Text that comes from the public web or from email replies is cleaned of
hidden characters, and our server tells the AI app which fields hold such third-party text so it
is treated as data, not as instructions.

**Children.** InfoJoy GTM is a business service and is not directed to anyone under 18.

**Contact.** Questions about this section or about an AI app connection: hello@marketjoy.com, or
the postal address in the Contact section of this policy.

---

## Also needed in the Terms of Service

The Terms on the same page say the Website may not be accessed by "intelligent agents" except
through the Service's own search tools. A reviewer reading them next to an AI-app listing may take
that as forbidding exactly what the listing offers. Ask counsel to add a sentence like this to that
clause:

> This restriction does not apply to an AI assistant or other application that a user of InfoJoy
> GTM connects to their own account through our official connection (the Model Context Protocol
> server at gtm.infojoy.com/mcp), which acts only with the permissions that user grants.

## After publishing

1. Update the "Last updated" date on the privacy policy (it has none today).
2. Check the anchor opens the section: `legal.html#privacy` (and `#ai-apps` if you added it).
3. From the listing repository: `python scripts/check.py --online`.
4. If you gave the section its own anchor and want listings to land on it, change the privacy links
   in `distribution/` (one value, enforced by `scripts/check.py` and
   `tests/test_mcp_distribution.py`) before the first submission; after submission, a link change
   needs a new review on ChatGPT and Microsoft.
