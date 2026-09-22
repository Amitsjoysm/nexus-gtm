# Live engagement tests: test mailboxes and CI secrets

The engagement engine has no fake mail provider (D21). Its adapters are proven against one real
Gmail mailbox and one real Microsoft 365 mailbox in the `live-engagement` CI job and locally with
`pytest tests_live/engagement -n0 -q -rs`. The owner sets these up once; Claude never handles the
credentials.

## 1. Two test mailboxes

- A Gmail (or Google Workspace) address used for nothing else, e.g. `nexus.live.gmail@…`.
- A Microsoft 365 (work or school) address used for nothing else, e.g. `nexus-live@…onmicrosoft.com`.
  A free Microsoft 365 developer tenant works.

Add the Gmail address as a **test user** on the Google OAuth consent screen
(docs/engagement/setup-google.md step 3).

## 2. Keep the Gmail test mailbox empty

The app requests only `gmail.readonly` and `gmail.compose`, which cannot delete mail, so the tests
cannot clean Gmail up themselves. In the Gmail test mailbox: **Settings → Filters → Create a new
filter**, Subject `[nexus-live`, **Create filter**, tick **Delete it**, **Create filter**. Test mail
still arrives (the history API reports trashed messages) and Trash empties itself after 30 days.

The Microsoft test deletes its own messages at the end of each run.

## 3. Register the token helper's redirect URI

On both apps add `http://localhost:8765/callback` as a redirect URI (Google: OAuth client →
Authorised redirect URIs; Azure: Authentication → Web → Add URI). Only the helper below uses it.

## 4. Get a refresh token for each mailbox

On your own machine, with the app credentials exported:

```bash
export NEXUS_LIVE_GOOGLE_CLIENT_ID=... NEXUS_LIVE_GOOGLE_CLIENT_SECRET=...
python scripts/engagement_live_token.py google       # sign in as the Gmail TEST mailbox
export NEXUS_LIVE_MICROSOFT_CLIENT_ID=... NEXUS_LIVE_MICROSOFT_CLIENT_SECRET=... NEXUS_LIVE_MICROSOFT_TENANT=common
python scripts/engagement_live_token.py microsoft    # sign in as the Microsoft TEST mailbox
```

Each prints a refresh token once. Paste it straight into the GitHub secret, then clear the terminal.
While the Google app is in *Testing*, Gmail refresh tokens expire after 7 days; rerun the helper when
the live job reports `invalid_grant`, or publish the app.

## 5. GitHub Actions secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `NEXUS_LIVE_REDIRECT_BASE` | the base URL registered on both apps, e.g. `https://localhost` |
| `NEXUS_LIVE_GOOGLE_CLIENT_ID` / `NEXUS_LIVE_GOOGLE_CLIENT_SECRET` | the Google OAuth client |
| `NEXUS_LIVE_MICROSOFT_CLIENT_ID` / `NEXUS_LIVE_MICROSOFT_CLIENT_SECRET` / `NEXUS_LIVE_MICROSOFT_TENANT` | the Azure app |
| `NEXUS_LIVE_GMAIL_ADDRESS` / `NEXUS_LIVE_GMAIL_REFRESH_TOKEN` | the Gmail test mailbox |
| `NEXUS_LIVE_M365_ADDRESS` / `NEXUS_LIVE_M365_REFRESH_TOKEN` | the Microsoft test mailbox |

Later phases add the ledger store connection strings (phase 06) and an LLM key (phase 09).

## 6. Run

Actions → CI → Run workflow (the `live-engagement` job), or locally with the same variables exported:
`pytest tests_live/engagement -n0 -q -rs`. A skipped test names the variable it is missing.
