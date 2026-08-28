# 12 — Resource inventory: everything that exists, and why

Every Azure resource and every repository file that participates in keeping the site live —
including the free and near-free ones, which are easy to dismiss and expensive to delete.

The column that matters most is **"what breaks without it"**. Several entries cost nothing and are
load-bearing; two cost nothing, look important, and are not.

---

# Part 1 — Azure resources

**18 Terraform-declared resources**, plus 3 created by Azure on your behalf. Full list from the
running deployment:

```bash
az resource list --resource-group nexus-prod-rg --query "[].{name:name,type:type}" -o table
az resource list --resource-group nexus-tfstate-rg --query "[].{name:name,type:type}" -o table
```

## 1.1 Compute — the application itself

| Resource | Terraform | Est./mo | Purpose | Without it |
|---|---|---|---|---|
| `nexus-prod-app` | `azurerm_container_app.app` | $10–35 | FastAPI + the built React SPA. **Also runs `alembic upgrade head` and `apply_rls.py` on every boot.** External HTTPS ingress. | No site |
| `nexus-prod-worker` | `azurerm_container_app.worker` | $15–20 | Queue consumer **and the scheduler**. Drives automation, signal ingestion, billing rollups, period closes, digests. | Site loads; nothing ever happens. Silent — the UI looks fine |
| `nexus-prod-valkey` | `azurerm_container_app.valkey` | ~$10 | Job queue + idempotency store. Internal TCP only, exactly 1 replica | App and worker cannot exchange work; no background jobs run |
| `nexus-prod-aca` | `azurerm_container_app_environment.main` | **$0** | The environment all three run in. Provides internal DNS, the shared VNet integration, log routing | Nothing runs. **Replacing it destroys all three apps** |

> **The worker is the one people forget.** It has no ingress and no URL, so a health check never
> touches it. If it dies, the product quietly stops doing everything that makes it a product.

> **`min == max == 1` on Valkey is not a tuning knob.** Two replicas are two independent queues —
> a job enqueued by the API could sit in the instance the worker is not reading from.

## 1.2 Data

| Resource | Terraform | Est./mo | Purpose | Without it |
|---|---|---|---|---|
| `nexus-prod-pg-<sfx>` | `azurerm_postgresql_flexible_server.main` | ~$17 | All tenant data. B1ms, 32 GB, private, no HA. **~50 max connections — the binding constraint on the whole stack** | No data, no login, `/ready` fails |
| `nexus` database | `..._flexible_server_database.nexus` | $0 | The database itself | — |
| `require_secure_transport = OFF` | `..._flexible_server_configuration.no_tls` | $0 | Lets asyncpg connect without client certs. **Safe only because traffic never leaves the VNet** | App cannot connect without TLS config work |

Automatic backups (14-day retention, PITR) are included in the server cost — no separate resource.

## 1.3 Networking — all free, all load-bearing

| Resource | Terraform | Cost | Purpose | Without it |
|---|---|---|---|---|
| `nexus-prod-vnet` | `azurerm_virtual_network.main` | **$0** | 10.20.0.0/16 private network | Postgres would need public exposure |
| `aca` subnet /23 | `azurerm_subnet.aca` | **$0** | Delegated to `Microsoft.App/environments`. **Must be /23** | Environment cannot be created |
| `db` subnet /24 | `azurerm_subnet.db` | **$0** | Delegated to `Microsoft.DBforPostgreSQL/flexibleServers` | Postgres cannot be VNet-integrated |
| `pe` subnet /24 | `azurerm_subnet.pe` | **$0** | Reserved for private endpoints | Currently unused — see below |
| `privatelink.postgres.database.azure.com` | `azurerm_private_dns_zone.postgres` | ~$0.50 | Resolves the Postgres FQDN to its private IP | **App cannot find the database.** DNS resolves to nothing |
| `postgres-link` | `azurerm_private_dns_zone_virtual_network_link.postgres` | **$0** | Attaches that zone to the VNet | Same — the zone exists but nothing consults it |

> **The private DNS zone costs about fifty cents and is absolutely load-bearing.** Delete it and
> the app cannot resolve its own database. It is the single cheapest resource here that causes a
> total outage.

> **The `pe` subnet is currently unused** — it existed for the Redis private endpoint, which went
> away with Azure Cache for Redis. Costs nothing; keep it, since a future managed Redis or a
> private-endpoint Postgres would need it.

## 1.4 Registry and observability

| Resource | Terraform | Est./mo | Purpose | Without it |
|---|---|---|---|---|
| `nexusprodacr<sfx>` | `azurerm_container_registry.main` | ~$5 | Holds every built image. **Your rollback history lives here** | Cannot deploy; cannot roll back |
| `nexus-prod-logs` | `azurerm_log_analytics_workspace.main` | $2–5 | Container logs. Ingestion is the cost driver | `az containerapp logs show` returns nothing — blind during incidents |
| `nexus-prod-alerts` | `azurerm_monitor_action_group.main` | **$0** | Where alerts are delivered. **Has zero receivers unless `alarm_email` is set** | Alerts fire into the void while the portal shows a rule |
| `nexus-prod-app-restarts` | `azurerm_monitor_metric_alert.app_replicas` | ~$0.10 | Fires when restarts > 3 in 5 min | No signal that the app is crash-looping |

## 1.5 Created by Azure, not by you

| Resource | Cost | Purpose | Note |
|---|---|---|---|
| `ME_nexus-prod-aca_nexus-prod-rg_<region>` | $0 | Managed infrastructure RG for the ACA environment | **Do not touch or delete.** Its name is also what forced environment replacement on every apply until `ignore_changes` was added |
| `nexus-prod-app` managed certificate | **$0** | TLS for `*.azurecontainerapps.io`, auto-renewing | Why no domain or cert purchase is needed to go live |
| `cloud-shell-storage-<region>` | ~$0.10 | Cloud Shell's persistent home | Holds `~/clouddrive` — your `.env` backups |

## 1.6 State — outside the stack on purpose

| Resource | Managed by | Est./mo | Purpose | Without it |
|---|---|---|---|---|
| `nexus-tfstate-rg` | **Manual** | $0 | Isolates state from anything Terraform can destroy | A `terraform destroy` would delete the record of what exists |
| `nexustfstate<n>` storage account | **Manual** | ~$0.05 | Holds `nexus/prod.tfstate` | Terraform cannot plan or apply |
| `tfstate` blob container | **Manual** | $0 | The container the backend writes to | `terraform init` fails with a bare 404 |
| Blob versioning + 30-day soft delete | **Manual** | ~$0.01 | **The recovery story for a corrupted state file** | A bad write is unrecoverable |

> These four are the only things in this document Terraform does not manage, and that is
> deliberate. Terraform must never own the record of what Terraform did.

## 1.7 Total

| Group | Est./mo |
|---|---|
| Compute (app + worker + valkey) | $35–65 |
| Postgres | ~$17 |
| ACR | ~$5 |
| Logs + alerts | $2–5 |
| Networking, DNS, certs, environment | ~$0.50 |
| State storage | ~$0.06 |
| **Total** | **~$60–95** |

Estimates, not verified against live pricing. **Roughly $0.60/month of that — the private DNS zone
— will take the whole site down if deleted.**

---

# Part 2 — Repository files that keep it alive

## 2.1 Terraform (`deploy/cloud/azure/`)

| File | Role | Critical detail |
|---|---|---|
| `versions.tf` | Provider pin + **state backend** | `~> 3.110`. Bumping to 4.x renames `enable_non_ssl_port` |
| `variables.tf` | All inputs | Holds the **connection-budget arithmetic** and the `NEXUS_APP_DB_PASSWORD` validator that refuses to deploy without tenant isolation |
| `network.tf` | RG, VNet, subnets, DNS | Subnet delegations are not optional |
| `platform.tf` | ACR + ACA environment | `local.uniq` (global names) and the `ignore_changes` that stops per-apply destruction |
| `data.tf` | Postgres | `ignore_changes = [zone]`; documents why there is no managed Redis |
| `container_apps.tf` | The three apps + connection URLs | Composes DB/Redis URLs; `--workers 1`; Valkey's `sh -c` command |
| `monitoring.tf` | Action group + alert | Silent when `alarm_email` is empty |
| `outputs.tf` | FQDNs | **Stable ingress FQDN, never a revision one** |

## 2.2 Application files in the deploy path

| File | Role | Critical detail |
|---|---|---|
| `deploy/Dockerfile` | Multi-stage image | Stage 1 builds the SPA into `nexus/web/dist`; stage 2 is the Python runtime. **One image serves app and worker** |
| `deploy/entrypoint.sh` | Boot sequence | Waits for the DB, runs migrations, applies RLS. **App only** (`NEXUS_RUN_MIGRATIONS=1`) — the worker skips both to avoid a race |
| `scripts/bootstrap_db.py` | Create-or-migrate | Takes a schema lock so two replicas cannot race |
| `scripts/apply_rls.py` | **Tenant isolation** | Creates the least-privilege role and verifies `rolsuper`/`rolbypassrls` are false. **This file is why RLS is real** |
| `deploy/cloud/deploy.sh` | One-command deploy | Registers providers, creates the state container, `az acr build`, two-phase apply |
| `migrations/` | Alembic chain | **Additive only** — rollback depends on it |
| `constraints.txt` | Exact version pins | A rebuild cannot silently pull a breaking transitive |

## 2.3 Not in the repo, and irreplaceable

| File | Where | Why it matters |
|---|---|---|
| **`deploy/.env`** | Your machine + `~/clouddrive` | DB passwords, `NEXUS_SECRET_KEY`, every API key. **Gitignored, not in the image, not in any bundle. Nobody can regenerate it for you** |
| `~/clouddrive/nexus-env.sh` | Cloud Shell | Rebuilds `TF_VAR_secrets` after a reconnect. Holds no secrets itself |
| `terraform.tfvars` | Optional, gitignored | Only if you prefer it to `-var` flags |

> **Losing `.env`**: `NEXUS_SECRET_KEY` gone ⇒ every user logged out. `POSTGRES_PASSWORD` gone ⇒
> resettable via `az postgres flexible-server update`. API keys gone ⇒ re-paste from each console.
> Survivable, but a bad day — and entirely preventable with one `cp`.

---

# Part 3 — The dependency chain

What has to exist for the site to be up, in order:

```
nexus-tfstate-rg + storage + tfstate container      ← manual, outside Terraform
        │
        ▼  terraform init
resource group ─→ VNet ─→ subnets (delegated)
        │              ├─→ private DNS zone + vnet link
        │              │
        ├─→ Log Analytics ──→ ACA environment ──┬─→ valkey     (internal TCP)
        │                                        ├─→ app        (public HTTPS)
        │                                        └─→ worker     (no ingress)
        ├─→ ACR ──→ image ─────────────────────────┘  (app + worker pull this)
        │
        └─→ Postgres (in db subnet, resolved via the DNS zone)
                     ▲
                     └── app runs migrations + apply_rls.py on boot
```

**Single points of failure at this tier**, all accepted deliberately at ~$90/mo:

| Component | Effect | Removed by |
|---|---|---|
| App (`min_replicas = 1`) | Full outage until restart (~30–60s, includes migrations) | `app_min = 2` + a larger Postgres SKU |
| Postgres (no HA) | Outage until PITR restore | `pg_ha_enabled = true` (~2× DB cost) |
| Valkey (1 replica, no persistence) | In-flight one-shot jobs lost on restart | Azure Managed Redis |
| Worker (1 replica) | Automation stops silently | More replicas — safe, the scheduler holds an advisory lock |

## Safe to delete vs. never delete

**Never:**
`nexus-tfstate-rg` · the state storage account · `privatelink.postgres...` zone or its vnet link ·
the ACR · any subnet · the ACA environment (cascades to all three apps)

**Safe:**
Old ACR image tags (keep 2–3 for rollback) · Log Analytics data past retention ·
`cloud-shell-storage-*` if you accept losing `~/clouddrive` — **back up `.env` first**

**Looks important, currently is not:** the `pe` subnet. Free, unused since the Redis change, worth
keeping for a future private endpoint.
