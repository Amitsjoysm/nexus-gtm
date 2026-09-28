# Mailbox OAuth: redirect URIs and scopes for every environment

One Google OAuth client and one Microsoft app registration serve every environment. Register
**every row** below in both; each environment then sends the redirect built from its own **Runtime
settings → Mailboxes & engagement → Public base URL**, which must match one registered row exactly
(no trailing slash).

The path is fixed by `nexus/engagement/config.py` and must not change once registered:
`/api/engagement/mailboxes/oauth/{google|microsoft}/callback`.

| Environment | Public base URL | Gmail redirect URI | Outlook / Microsoft 365 redirect URI |
|---|---|---|---|
| Production | `https://gtm.infojoy.com` | `https://gtm.infojoy.com/api/engagement/mailboxes/oauth/google/callback` | `https://gtm.infojoy.com/api/engagement/mailboxes/oauth/microsoft/callback` |
| Staging | `https://staging-gtm.infojoy.com` | `https://staging-gtm.infojoy.com/api/engagement/mailboxes/oauth/google/callback` | `https://staging-gtm.infojoy.com/api/engagement/mailboxes/oauth/microsoft/callback` |
| Staging (Azure host) | `https://gtm-staging-app.mangowater-46a1ec4f.eastus2.azurecontainerapps.io` | `https://gtm-staging-app.mangowater-46a1ec4f.eastus2.azurecontainerapps.io/api/engagement/mailboxes/oauth/google/callback` | `https://gtm-staging-app.mangowater-46a1ec4f.eastus2.azurecontainerapps.io/api/engagement/mailboxes/oauth/microsoft/callback` |
| Local | `http://localhost:8099` | `http://localhost:8099/api/engagement/mailboxes/oauth/google/callback` | `http://localhost:8099/api/engagement/mailboxes/oauth/microsoft/callback` |
| Local (Caddy) | `https://localhost` | `https://localhost/api/engagement/mailboxes/oauth/google/callback` | `https://localhost/api/engagement/mailboxes/oauth/microsoft/callback` |

Google and Microsoft both accept plain `http` only for `localhost`. Staging has two rows because both
hosts reach it; register both and set the Public base URL to the one people actually open.

## Scopes

| Provider | Scopes | Notes |
|---|---|---|
| Google | `openid`, `email`, `https://www.googleapis.com/auth/gmail.readonly`, `https://www.googleapis.com/auth/gmail.compose` | Both Gmail scopes are **restricted**. In "Testing" only listed test users (up to 100) can connect; production needs Google verification and a CASA assessment. |
| Microsoft Graph (delegated) | `openid`, `email`, `offline_access`, `User.Read`, `Mail.ReadWrite`, `Mail.Send` | With the tenant setting `common`, the registration must accept "any organizational directory and personal Microsoft accounts"; otherwise set **Microsoft tenant** to your directory id. |

## Where each value goes

| Value | Where |
|---|---|
| Public base URL, Google client id, Microsoft client id, Microsoft tenant, Gmail notification topic, Gmail push service account | Runtime settings → Mailboxes & engagement, per environment |
| Google client secret, Microsoft client secret | Superadmin → Provider keys: `google_oauth`, `microsoft_oauth` |

Nobody pastes a secret into chat, a ticket or a file in the repo.

## Reply pickup

Staging and production: point the Gmail Pub/Sub push subscription at
`<public base URL>/api/engagement/webhooks/gmail` with authentication on, the audience set to that
same URL, and the service account entered in Runtime settings. Outlook subscribes itself to
`<public base URL>/api/engagement/webhooks/graph`. Neither can reach `localhost`: locally, replies
arrive through the periodic mailbox sync instead, a few minutes behind.
