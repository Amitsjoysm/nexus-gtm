# NOT eastus. Measured 2026-08-05: PostgreSQL Flexible Server provisioning is RESTRICTED in East
# US on at least some subscriptions — `list-skus` returns a "Provisioning is restricted in this
# region" notice instead of a catalog. Azure surfaces that as
# `400 ParameterOutOfRange: The value of the 'Version' should be in: []`, an EMPTY list that reads
# as "PostgreSQL 16 is unsupported" and sends you off to change the version. It is not the version.
#
# Confirm before choosing a region — a restricted region returns a few hundred bytes, a healthy
# one tens of thousands:
#   for r in eastus eastus2 centralus westus3; do \
#     echo "$r: $(az postgres flexible-server list-skus -l $r -o json | wc -c) bytes"; done
#
# Everything is regional and the Postgres server must sit in the same region as its delegated
# subnet, so changing this after a deploy means rebuilding the whole stack — not just the database.
variable "location" {
  type    = string
  default = "eastus2"
}

variable "project" {
  type    = string
  default = "nexus"
}

variable "env" {
  type    = string
  default = "prod"
}

variable "domain" {
  type        = string
  description = "Public hostname, e.g. app.example.com. Bind it to the app's Container App ingress."

  # Defaulted, because this variable PROVISIONS NOTHING. Its only consumer is the
  # `custom_domain_next_step` output string in outputs.tf — the ingress in container_apps.tf does
  # not reference it, and no certificate or DNS record is created from it. Requiring a real
  # hostname up front therefore blocks a deploy on a decision that has no effect on the
  # infrastructure produced.
  #
  # Until a custom domain is bound, the app is reachable on the ACA-assigned
  # `*.azurecontainerapps.io` FQDN (the `app_default_url` output), which carries a
  # Microsoft-managed TLS certificate. Binding a real domain later is an ADDITIVE change —
  # a CNAME plus azurerm_container_app_custom_domain — not a rebuild.
  default = "app.example.com"
}

# Suffix for the three GLOBALLY-unique names (ACR, Postgres, Redis). Empty derives one from the
# subscription id — deterministic, so a re-apply never proposes replacing the registry.
#
# OVERRIDE THIS AFTER A DESTROY. Azure RESERVES a Redis cache name even after the cache is
# deleted, and its own error says releasing it needs a support ticket. A deterministic suffix is
# exactly right for repeated applies and exactly wrong for a rebuild: the redeploy computes the
# same name its predecessor just reserved and is refused. Bump to "v2", "v3", ... when rebuilding
# from scratch.
variable "name_suffix" {
  type    = string
  default = ""
}

# PostgreSQL major version. A variable rather than a literal because version availability is
# per-region AND per-subscription, and the failure mode is opaque: an unregistered provider or an
# unsupported version both surface as `400 ... 'Version' should be in: []`, an EMPTY list that
# reads as "16 is unsupported" when the cause is usually something else entirely. Check with:
#   az postgres flexible-server list-skus -l <region> -o json
# Nothing in this codebase requires 16 — the migrations and RLS policies work on 13+.
variable "pg_version" {
  type    = string
  default = "16"
}

variable "image" {
  type        = string
  description = "Full ACR image:tag for app/worker. deploy.sh fills this after pushing."
  default     = "mcr.microsoft.com/k8se/quickstart:latest" # bootstrap placeholder
}

# ============================== Sizing (Stage 0) ==============================
# These defaults are deliberately the SMALLEST shape that is still production-correct for the
# current workload (10-15 users). Every one of them scales up in place — none is an
# architectural dead end. Triggers and target values: docs/deployment/23-SCALING-ROADMAP.md.

variable "app_cpu" { default = 0.5 }
variable "app_memory" { default = "1Gi" }

# min 1: one always-on replica. The trade is explicit — if it crashes, service is down for the
# tens of seconds it takes to restart AND re-run migrations on boot (deploy/entrypoint.sh).
# Raising this to 2 removes that gap but does NOT fit the B1ms connection budget; see pg_sku.
variable "app_min" { default = 1 }
variable "app_max" { default = 3 }

variable "worker_cpu" { default = 0.25 }
variable "worker_memory" { default = "0.5Gi" }

# ============================== Managed data ==============================
# CONNECTION BUDGET — the binding constraint on this whole stack.
#
# A Burstable B1ms allows roughly 50 connections. Peak usage is:
#
#   app_replicas x uvicorn_processes x (NEXUS_DB_POOL_SIZE + NEXUS_DB_MAX_OVERFLOW
#                                       + platform pool + platform overflow)
#   ... DOUBLED for the length of a rollout, because Container Apps runs the old and new
#       revisions simultaneously, ... + the worker's single process.
#
# At the configured 5+5 (+2+3 platform) = 15/process, with --workers 1:
#
#   steady:        1 app + 1 worker            = 30   of ~50   OK
#   during deploy: 2 app (old+new) + 1 worker  = 45   of ~50   OK, tight
#   app_min = 2:   4 app (old+new) + 1 worker  = 75   of ~50   EXCEEDS
#
# That last row is why app_min stays at 1 here. Raising app_min REQUIRES either a larger SKU or
# a smaller pool — it is not an independent knob. Exceeding max_connections does not degrade
# gracefully: Postgres refuses new connections, which surfaces as 500s under exactly the load
# that caused it, and typically first appears DURING A DEPLOY rather than in steady state.
variable "pg_sku" {
  type    = string
  default = "B_Standard_B1ms" # Burstable. Upgrade trigger: sustained CPU >70% or connections >40.
}
variable "pg_storage_mb" {
  type    = number
  default = 32768
}
# HA off at Stage 0: ZoneRedundant HA runs a full standby and roughly doubles the DB bill for an
# availability guarantee this user count does not yet need. Backups + PITR still cover data loss;
# what HA buys is faster RECOVERY, which is a different question. Flip to true when the SLA
# justifies it — it is an in-place change plus a restart, not a rebuild.
variable "pg_ha_enabled" {
  type    = bool
  default = false
}
# NOTE: redis_sku / redis_capacity were REMOVED with azurerm_redis_cache. Azure Cache for
# Redis is retired (see data.tf); Valkey now runs as an internal-only Container App sized by
# the container block in container_apps.tf, not by a SKU.

variable "alarm_email" {
  type    = string
  default = ""
}

variable "log_retention_days" {
  type    = number
  default = 30
}

# Raw secrets (passwords + API keys) from deploy/.env via TF_VAR_secrets. Connection URLs are
# composed in container_apps.tf from the managed-service endpoints. Sensitive; never committed.
variable "secrets" {
  type      = map(string)
  sensitive = true
  default   = {}

  # The API must connect as the least-privilege `nexus_app` role — that is what makes Postgres RLS
  # an actual tenant boundary rather than a decoration. Two components read this same value and
  # default it in OPPOSITE directions:
  #
  #   * `scripts/apply_rls.py` treats an empty NEXUS_APP_DB_PASSWORD as "single-role deploy" and
  #     skips creating `nexus_app` altogether.
  #   * `container_apps.tf` composes `nexus_app:<password>@...` regardless.
  #
  # Unvalidated, an empty value provisions the entire stack and then fails at container start,
  # pointed at a role that was never created. Worse, the obvious fix is to point the app at the
  # owner URL — which silently removes RLS enforcement for every tenant, and nothing would report
  # it. Refuse at plan time instead: a deploy that cannot enforce tenant isolation should not begin.
  validation {
    condition     = length(trimspace(lookup(var.secrets, "NEXUS_APP_DB_PASSWORD", ""))) >= 16
    error_message = "NEXUS_APP_DB_PASSWORD must be set (>=16 chars) in deploy/.env. The API connects as the least-privilege nexus_app role, and scripts/apply_rls.py only creates that role when this is set. Generate one with: openssl rand -hex 32"
  }
}

variable "common_env" {
  type = map(string)
  default = {
    NEXUS_ENV           = "prod"
    NEXUS_QUEUE_BACKEND = "redis"

    # Tuned DOWN from the 10/20 default to fit the B1ms connection budget documented on pg_sku
    # above. These are the dial to reach for FIRST when connections get tight — before upsizing
    # the database, and before reducing replicas.
    NEXUS_DB_POOL_SIZE    = "5"
    NEXUS_DB_MAX_OVERFLOW = "5"
  }
}
