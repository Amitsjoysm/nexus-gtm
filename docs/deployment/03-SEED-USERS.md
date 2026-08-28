# 03 — First workspace, superadmin and users

**There are no seeded users.** A fresh deployment has an empty `users` table and an empty `tenants`
table. Nobody can log in until you create the first workspace.

---

## The trap: the UI cannot create the first user

`frontend/src/pages/LoginPage.tsx` calls **`registerStart`** — the OTP flow — and never
`/auth/signup`. So the browser always asks the API to *email a verification code*, regardless of
the `NEXUS_OTP_REGISTRATION_ENABLED` setting.

If SMTP is not configured, **no code is ever sent and signup is impossible from the UI**. There is
no error on screen that explains this; the form simply waits for a code that will never arrive.

Two ways out. Do the first now, the second when you want self-serve signup.

---

## A. Create the first workspace via the API (works immediately)

`/auth/signup` is single-step and needs no email — it is only closed when
`NEXUS_OTP_REGISTRATION_ENABLED=true`, which defaults to false.

```bash
cd ~/nexus/deploy/cloud/azure && URL="$(terraform output -raw app_default_url)"

curl -s -X POST "$URL/api/auth/signup" -H 'Content-Type: application/json' -d '{
  "company_name": "MarketJoy",
  "company_slug": "marketjoy",
  "email": "you@example.com",
  "full_name": "Your Name",
  "password": "REPLACE-WITH-A-STRONG-PASSWORD"
}' | python3 -m json.tool
```

Success returns a `TokenResponse` (201). The first user of a workspace is **`owner`**.

Then log in through the UI at `$URL` with that email and password — **login is a normal flow and
needs no email at all.** Only registration is gated on OTP.

| Failure | Meaning |
|---|---|
| `403 Email verification required` | `NEXUS_OTP_REGISTRATION_ENABLED=true`. Set it false, or use path B. |
| `409 Company slug already taken` | Pick a different slug. |
| `409 Email already registered` | The user exists — just log in. |
| `422` | Slug must match `^[a-z0-9][a-z0-9-]{1,79}$`; password ≥ 8 chars. |

## B. Enable email so the UI signup works

Set these in `deploy/.env`, then re-apply (see [07-OPERATIONS.md](07-OPERATIONS.md#changing-a-secret-or-api-key)):

```
NEXUS_SYSTEM_SMTP_PROVIDER=gmail
NEXUS_SYSTEM_SMTP_USERNAME=notifications@yourdomain.com
NEXUS_SYSTEM_SMTP_PASSWORD=<app password, not the account password>
NEXUS_SYSTEM_SMTP_FROM=notifications@yourdomain.com
NEXUS_SYSTEM_SMTP_FROM_NAME=NEXUS GTM
```

Gmail requires an **App Password** with 2FA enabled — a normal account password is rejected.
`system_smtp_provider` selects host/port presets; see `nexus/notifications/email_sender.py`.

With SMTP working you can optionally set `NEXUS_OTP_REGISTRATION_ENABLED=true` so every account is
email-verified. **Do not enable it before SMTP is verified working** — that closes `/auth/signup`
too, and locks you out of both paths.

Verify delivery by registering a throwaway account through the UI and watching for the code.

---

## Platform superadmin

Platform admin is **completely separate from tenant RBAC**. No workspace role — not even `owner` —
grants access to the `/admin` platform console. Membership comes from an env allowlist or the
`platform_admins` table, and it fails closed.

```
NEXUS_PLATFORM_ADMIN_EMAILS=you@example.com,ops@example.com
```

Empty means **nobody** has platform access, which is the default. Re-apply after setting it.

```bash
cd ~/nexus/deploy/cloud/azure
terraform apply -var "name_suffix=$SFX" -var "alarm_email=ops@example.com" -var "image=$REG/nexus:$TAG"
```

> **The env allowlist deliberately carries full power.** It exists to solve the bootstrap
> chicken-and-egg problem — narrowing it would reintroduce the lockout it prevents. Grant granular
> per-permission access through the `platform_admins` table instead, once you can reach the console.

Confirm:

```bash
curl -s "$URL/api/admin/plans" -H "Authorization: Bearer $TOKEN" -o /dev/null -w "%{http_code}\n"
```

200 = platform access. 403 = the email is not in the allowlist, or the app has not restarted since
you set it.

---

## Adding teammates

Once an owner exists, invite through the UI: **Settings → Team → Invite**. Roles are hierarchical:

```
owner  >  admin  >  manager  >  rep
```

- **rep** — inbox, accounts, research, drafting outreach
- **manager** — plus lists, plays, analytics
- **admin** — plus team, roles, integrations
- **owner** — plus billing and workspace deletion

Invites are emailed, so this also needs SMTP. Without it, create users via `/auth/signup` into the
same workspace slug, then adjust roles in the UI.

---

## Demo data (optional, non-production)

`scripts/seed_demo.py` creates a workspace with accounts, contacts, signals and inbox tasks against
a running API — useful for exercising the UI before real data exists.

```bash
python3 scripts/seed_demo.py --base-url "$URL"
```

It is idempotent (logs in if the owner exists, matches accounts by name). It creates a *real*
workspace called `northwind` with a known password, so:

> **Do not run this against a production instance you will keep.** Either delete the workspace
> afterwards or run it only on staging. A workspace with published credentials is a live account
> anyone who reads this repo can log into.

---

## What "no seeded users" protects you from

It would be far more convenient to ship a default admin. It is also how most self-hosted products
get compromised: the default credentials are in the public repository, and the first deployment
that forgets to change them is an open door. An empty database that requires one deliberate API
call is the safer default, and the cost is exactly one command.
