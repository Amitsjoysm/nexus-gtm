# Deploy on GitHub Codespaces (from Docker Hub images)

Spin up the full InfoJoy GTM stack in the cloud with no local setup. The Codespace pulls the
published images (`ronsmithz/nexus-gtm-api`, `ronsmithz/nexus-gtm-worker`), starts Postgres +
Valkey + API + worker, and seeds the demo tenant automatically.

## 1. Add your keys as a Codespaces secret (one-time, secure)

Your API keys must **never** be committed. Codespaces injects them as encrypted environment
variables instead. Create a single secret containing your whole `.env`:

1. GitHub → your avatar → **Settings** → **Codespaces** → **Secrets** → **New secret**
   (or repo → **Settings** → **Secrets and variables** → **Codespaces**).
2. **Name:** `DOTENV`
3. **Value:** paste the *entire contents* of your local `.env` (all `NEXUS_*` lines — Groq keys,
   Exa keys, `NEXUS_LLM_PROVIDER=auto`, `NEXUS_CONTACT_SEARCH_SOURCES=search`, etc.).
4. **Repository access:** grant it to `Amitsjoysm/nexus-gtm`.
5. Save.

On boot, `.devcontainer/setup.sh` writes that value to `.env` inside the Codespace. If you skip
this step the app still runs, but in degraded/stub mode (no live LLM/search).

> The keys currently in your `.env` are the ones shared earlier — **rotate them in the Groq/Exa
> consoles** before any real production use, then update the `DOTENV` secret.

## 2. Launch the Codespace

Repo → **Code ▸ Codespaces ▸ Create codespace on main** (pick a **4-core / 8 GB** machine).

First boot takes ~3–5 min while it pulls images, starts the stack, and seeds. Watch progress in
the terminal (look for `[setup]` lines).

## 3. Open the app

When port **8000** is forwarded, click **Open in Browser** (Ports tab). Log in with the seeded
demo account:

- **Email:** `owner@northwind.example`
- **Password:** `demo-password-123`
- **Workspace:** `northwind`

## Everyday use

- The stack auto-restarts when you resume the Codespace (`postStartCommand`).
- Re-seed manually if needed:
  ```bash
  docker compose -f docker-compose.hub.yml exec api python scripts/seed_demo.py --base-url http://localhost:8000
  ```
- Pull newer images:
  ```bash
  docker compose -f docker-compose.hub.yml pull && docker compose -f docker-compose.hub.yml up -d
  ```
- Logs: `docker compose -f docker-compose.hub.yml logs -f api`

## Notes

- Data persists in Docker volumes (`pgdata`, `valkeydata`) for the life of the Codespace.
- The images carry **no secrets**; all keys come from the `DOTENV` secret at runtime.
- Same compose works on any Docker host, not just Codespaces:
  `cp .env.example .env && docker compose -f docker-compose.hub.yml up -d`.
