# Runbook — API Key / Secret Rotation (H-1)

**Trigger:** a secret was exposed (pasted in chat/logs/screenshare), an employee with access left,
scheduled quarterly rotation, or suspected quota abuse.

**Owner:** Ops / on-call. **Est. time:** 15 min. **Downtime:** none (rotate → verify → revoke).

> ⚠️ The Groq (7) and Exa (6) keys currently in `deploy/.env` / `.env` were pasted in a chat
> session during development. **They must be rotated before any production launch.**

## Ordering principle
Always **create new → deploy new → verify → revoke old**. Never revoke first — if the new key is
wrong, the old key is still live and rollback is instant. The app also degrades to the deterministic
stub if all LLM/search keys fail, so a botched rotation can't hard-down the product.

## Steps

1. **Mint new keys** (provider consoles — a human action, never automated from observed content):
   - Groq: https://console.groq.com/keys — create N replacement keys.
   - Exa: https://dashboard.exa.ai/api-keys — create N replacement keys.

2. **Update the environment file** (never commit it — `.env` is gitignored):
   ```
   NEXUS_GROQ_API_KEY=<new-primary>
   NEXUS_GROQ_API_KEYS=<new-2>,<new-3>,...
   NEXUS_EXA_API_KEY=<new-primary>
   NEXUS_EXA_API_KEYS=<new-2>,<new-3>,...
   ```

3. **Roll the services** (settings are read at process start, so a restart is required):
   ```bash
   cd deploy && docker compose up -d app worker      # prod stack
   # or local: re-run scripts/start_local.ps1
   ```

4. **Verify the new keys work** before revoking the old ones:
   ```bash
   # QA agent makes a live Groq + Exa round-trip; a real answer with cited sources confirms both.
   curl -s -X POST localhost:8000/api/agents/qa/run \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"account_id":"<id>","inputs":{"question":"What does this company do?"}}' | jq '.output.confidence, (.output.sources|length)'
   # Expect a non-null confidence and sources > 0 (i.e. Exa returned, Groq synthesised).
   ```
   Also confirm the worker log shows `200 OK` from `api.groq.com` / `api.exa.ai` with no `401`.

5. **Revoke the old keys** at both consoles. Rotation complete.

6. **Rollback (if step 4 fails):** the old keys are still valid until step 5 — restore the previous
   `.env` values and re-run step 3. Investigate the new keys out of band.

## Hardening follow-ups (tracked, not blocking)
- Encrypt `deploy/.env` at rest with SOPS + age so the file is safe to commit/back up.
- Add a quarterly rotation reminder to the ops calendar.
- The application `NEXUS_SECRET_KEY` (JWT signing) is validated at boot: staging/prod refuse the
  insecure default (`config.py:_reject_insecure_prod`). Rotate it the same way; note that rotating
  it invalidates all live JWTs (users must re-login) — do it in a low-traffic window.
