# DRAFT: a privacy policy section for AI apps

**Not legal advice and not published.** A starting point for the owner's counsel, written because
every directory reviews the privacy policy a listing links to, and the current policy at
`https://infojoy.com/legal.html#privacy` names neither InfoJoy GTM nor connected AI apps. The facts
below are what the product does today (`nexus/mcp/`); counsel decides the wording, where it goes
in the policy, and whether the Terms' restriction on "intelligent agents" needs a matching
exception for apps a customer connects to their own workspace.

Anthropic's directory requires a policy to cover data collection, usage and storage, third-party
sharing, retention and contact information; a listing with an incomplete policy is rejected.

---

## Connected AI apps (InfoJoy GTM)

InfoJoy GTM lets a signed-in user connect AI assistants, such as Claude, ChatGPT, Microsoft
Copilot, Gemini, Cursor or VS Code, to their workspace through our Model Context Protocol server
at gtm.infojoy.com/mcp.

**What we collect.** When a user connects an app we record the connection: the app's name and
the addresses it registered, the user and workspace, the permissions the user granted (Read,
Research, Write), and when it was created and last used. For research and write actions we record
in the workspace's audit log which tool ran, when, its outcome and the identifiers of the records
it acted on; for every call we count tool use, without its content. We do not receive or store the
user's conversation with the AI app, only the requests the app sends to us.

**How it is used.** To carry out the request as the user, within their role and the permissions
they granted; to enforce rate limits and credit balances; to secure the service and investigate
misuse; and to show the user and their administrators which apps are connected.

**Sharing with the AI app and its provider.** When an app reads data through a connection (for
example company and contact details, buying signals, drafts and replies), that data is sent to the
app the user connected and to the AI model provider behind it, and is processed under that app's
terms and privacy policy, not ours. Users and administrators choose which apps to connect and can
revoke any connection at any time in Settings, AI apps.

**Retention.** Connection records are kept while the connection exists and for [period, counsel to
set] after it is revoked. Access tokens expire after one hour; refresh tokens last up to 30 days,
are replaced on every use, and are revoked with the connection. Audit records follow the
workspace audit log's retention of [period, counsel to set].

**Your choices and contact.** A user can disconnect an app, or change what it may do, at any time;
workspace administrators can see and revoke every connection in the workspace. Questions:
[support address].
