# Connecting Microsoft 365: Azure setup

The owner does these steps in the Microsoft Entra admin center. Secrets go only into the Control
plane, never into chat, tickets or the repo.

You need: an account that can register applications in Microsoft Entra ID, and the deployment's
public base URL (local deploy: `https://localhost`). Open **Control plane → Mailbox apps** for the
values marked *(copy)*.

## 1. App registration

1. <https://entra.microsoft.com> → **Identity → Applications → App registrations → New
   registration**.
2. Name: `nexus-mailboxes`.
3. Supported account types: **Accounts in any organizational directory and personal Microsoft
   accounts** (matches tenant `common`). Choose *single tenant* only if every SDR is in your own
   directory, and then set the tenant in step 5.
4. Redirect URI: platform **Web**, value: the **Microsoft redirect URI** *(copy)*, which is
   `<public base URL>/api/engagement/mailboxes/oauth/microsoft/callback`.
5. Register. Copy the **Application (client) ID** and **Directory (tenant) ID** from Overview.

## 2. More redirect URIs

**Authentication → Web → Add URI** for each additional environment (local, staging, production).

## 3. API permissions (delegated)

1. **API permissions → Add a permission → Microsoft Graph → Delegated permissions**, add:
   - `openid`, `email`, `offline_access`
   - `User.Read`
   - `Mail.ReadWrite`
   - `Mail.Send`
2. None of these needs admin consent in a default tenant; each SDR consents when they connect. If
   your organisation disables user consent, press **Grant admin consent** here.

## 4. Client secret

1. **Certificates & secrets → Client secrets → New client secret**, description `nexus`, expiry
   **24 months** (put the expiry date in the calendar: an expired secret stops every refresh).
2. Copy the **Value** immediately (not the Secret ID); it is shown once.

## 5. Control plane

1. **Configuration → Mailboxes & engagement**:
   - **Microsoft app (client) id**: the Application (client) ID.
   - **Microsoft tenant**: `common` (or your tenant id / domain for single tenant).
2. **Provider keys → Add key**: provider **Microsoft app client secret (mailboxes)**, paste the value,
   Save, **Test**. Expected: *probe ok — valid client, not yet authorised by a user*.
   `AADSTS7000215` = wrong secret value (you may have copied the Secret ID); `AADSTS7000222` =
   expired; `AADSTS700016` = wrong client id or tenant.

## 6. Reply notifications

Microsoft Graph calls the **Notification URL** *(copy)* when a connected mailbox receives mail. It
must be a public https address with a valid certificate; it cannot work against `localhost`, where
replies arrive on the few-minute poll instead. Nothing to register in Azure: subscriptions are
created per mailbox by the app.

## 7. Publisher verification (for customers outside your organisation)

**Branding & properties → Publisher verification**: associate a verified Microsoft Partner Network
(MPN / Cloud Partner Program) id. Without it, users in other organisations see an "unverified" warning
and many tenants block the consent.

## 8. Check

**Control plane → Health**: the *mailbox apps* row reads `microsoft: valid client, not yet authorised
by a user`.
