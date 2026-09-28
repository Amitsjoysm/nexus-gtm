# Connecting Gmail: Google Cloud setup

Every environment's redirect URI, and the scopes, in one table: [oauth-environments.md](oauth-environments.md).

The owner does these steps in Google Cloud. Nobody pastes a secret into chat, a ticket or a file in
the repo; secrets go only into the Control plane.

You need: a Google Workspace or Google account that can create Cloud projects, and the deployment's
public base URL (for the local deploy, `https://localhost`).

Open **Control plane → Mailbox apps** first. Every value below marked *(copy)* is shown there with a
Copy button; copy it rather than typing it.

## 1. Project

1. Go to <https://console.cloud.google.com/projectcreate>.
2. Name it (for example `nexus-mailboxes`) and create it. Note the **project id**.
3. Select the project in the top bar for every step below.

## 2. Enable the APIs

1. **APIs & Services → Library**.
2. Enable **Gmail API**.
3. Enable **Cloud Pub/Sub API**.

## 3. OAuth consent screen

1. **APIs & Services → OAuth consent screen** (in the new console: **Google Auth Platform →
   Branding / Audience / Data access**).
2. User type: **External**.
3. App name, support email, developer contact email: your company's.
4. Authorised domains: the domain of the public base URL (skip for `localhost`).
5. **Data access → Add or remove scopes**, add exactly:
   - `openid`
   - `.../auth/userinfo.email` (shown as `email`)
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.compose`
6. **Audience → Test users**: add the Gmail addresses of the SDRs and the live-test mailbox. While
   the app is in *Testing*, only these can connect, and their refresh tokens expire after 7 days.

## 4. OAuth client

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Web application**. Name: `nexus-mailboxes`.
3. **Authorised redirect URIs → Add URI**: the **Google redirect URI** *(copy)*, which is
   `<public base URL>/api/engagement/mailboxes/oauth/google/callback`. Add one per environment.
4. Create. Copy the **Client ID** and **Client secret** from the dialog.
5. In the Control plane:
   - **Configuration → Mailboxes & engagement → Google OAuth client id**: paste the Client ID, Save.
   - **Provider keys → Add key**: provider **Google OAuth client secret (mailboxes)**, paste the
     secret, Save, then press **Test**. Expected: *probe ok — valid client, not yet authorised by a
     user*. *invalid_client* means the secret or client id is wrong.

## 5. Pub/Sub for reply notifications

Replies still arrive without this (a poll every few minutes), but with it they arrive within seconds.
It needs a public https base URL; it cannot work against `localhost`.

1. **Pub/Sub → Topics → Create topic**, id `gmail-replies`, no default subscription.
2. Open the topic → **Permissions → Add principal**: `gmail-api-push@system.gserviceaccount.com`,
   role **Pub/Sub Publisher**. Without this Gmail cannot publish and every watch fails.
3. **IAM & Admin → Service accounts → Create**: `gmail-push`. No roles needed on the project.
4. Grant your own user **Service Account Token Creator** on that service account if the console
   asks when creating the subscription.
5. **Pub/Sub → Subscriptions → Create subscription**:
   - Topic: `gmail-replies`
   - Delivery type: **Push**
   - Endpoint URL: the **Push endpoint** *(copy)* — it carries no token of ours; the
     authentication is the OIDC token configured on the next line
   - **Enable authentication**, service account `gmail-push@<project-id>.iam.gserviceaccount.com`,
     audience: the **OIDC audience** *(copy)*
   - Acknowledgement deadline 30 s, retry policy *exponential backoff*.
6. In the Control plane, **Configuration → Mailboxes & engagement**:
   - **Gmail notification topic**: `projects/<project-id>/topics/gmail-replies`
   - **Gmail push service account**: `gmail-push@<project-id>.iam.gserviceaccount.com`

## 6. Verification and CASA (start early — weeks, not days)

`gmail.readonly` and `gmail.compose` are **restricted scopes**. Until Google verifies the app, only
test users can connect, and each connection lapses after 7 days.

1. **OAuth consent screen → Publish app** (moves to *In production*, status *Needs verification*).
2. **Prepare for verification**: homepage, privacy policy and terms URLs on your domain; a YouTube
   (unlisted) video showing the OAuth flow and how each scope is used (connect mailbox → campaign
   send → reply appears in the reply desk).
3. Justification per scope: *gmail.compose* sends the SDR's approved outreach and saves drafts from
   their own mailbox; *gmail.readonly* detects replies to that outreach so follow-ups stop and the SDR
   is alerted; mail not belonging to a conversation the app started is discarded without storing.
4. Submit. Google then requires a **CASA Tier 2** security assessment from an authorised assessor;
   follow the link in the verification email.

## 7. Check

**Control plane → Health**: the *mailbox apps* row reads `google: valid client, not yet authorised
by a user`. After phase 03 ships, an SDR pressing **Connect Google** in Settings → Mailboxes completes
the flow.
