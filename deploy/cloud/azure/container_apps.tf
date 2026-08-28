# ============================ Valkey (queue + idempotency store) ============================
# Replaces the RETIRED azurerm_redis_cache — see the comment block in data.tf.
#
# Internal ingress only: `external_enabled = false` means this is reachable from other apps in
# this Container Apps environment and from nowhere else. There is no password, and that is safe
# ONLY because of that: the port is never routable from the internet. If this is ever made
# external, it needs `--requirepass` first — an open Redis is trivially remote-code-executable.
resource "azurerm_container_app" "valkey" {
  name                         = "${local.name}-valkey"
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"

  ingress {
    external_enabled = false
    transport        = "tcp" # Redis speaks its own protocol, not HTTP
    target_port      = 6379
    exposed_port     = 6379
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    # EXACTLY ONE REPLICA, and this is not a tuning knob. A queue is stateful: two replicas are
    # two independent queues, so a job enqueued by the API might sit in the instance the worker
    # is not reading from and simply never run. min == max is what prevents that.
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "valkey"
      image  = "valkey/valkey:8-alpine" # same image as deploy/docker-compose.prod.yml
      cpu    = 0.25
      memory = "0.5Gi"

      # NO --appendonly: Container Apps gives this container ephemeral storage, so an AOF file
      # would be written to a disk that disappears with the container and would only cost write
      # amplification for nothing. Persistence would need an Azure Files mount, which is the
      # fragile ACA-stateful pattern this stack deliberately avoids for Postgres.
      #
      # WHAT THAT COSTS, stated plainly: a restart (deploy, platform maintenance, OOM) loses
      # whatever was queued at that instant. The durability design absorbs most of it — periodic
      # sweeps re-enqueue themselves every tick and are idempotent, and handler failures are
      # dead-lettered to Postgres (nexus/workers/durability.py). What is genuinely lost is
      # one-shot work in flight: a campaign send, an orchestration run, an account refresh.
      # Idempotency keys are short-TTL, so losing them can at worst allow one duplicate retry.
      #
      # This is the same exposure as the single-node Basic C0 cache it replaces, at ~1/4 the cost
      # — but restarts are MORE frequent than managed-node failures, so it is not identical.
      # `--save ''` disables RDB snapshots for the same reason.
      #
      # WHY `command` WITH `sh -c` RATHER THAN A PLAIN `args` LIST: valkey disables snapshotting
      # via an EMPTY-STRING argument, and an empty element in the azurerm `args` list crashes the
      # provider outright:
      #   interface conversion: interface {} is nil, not string
      # (visible in the error as `--save <nil>`). It never reaches Azure. Wrapping the flags in a
      # single shell string keeps the empty argument inside a value the provider can serialise.
      #
      # `exec` matters: without it, sh stays PID 1 and swallows SIGTERM, so Container Apps would
      # wait out the full termination grace period on every restart instead of stopping cleanly.
      command = ["sh", "-c", "exec valkey-server --save '' --maxmemory-policy noeviction"]
    }
  }

  # Azure defaults workload_profile_name to "Consumption" server-side; config does not set it, so
  # every plan shows `"Consumption" -> null` forever. Same class of phantom diff as
  # infrastructure_resource_group_name on the environment (see platform.tf).
  lifecycle {
    ignore_changes = [workload_profile_name, tags]
  }
}

locals {
  pg_fqdn = azurerm_postgresql_flexible_server.main.fqdn

  # Reached over the ACA environment's internal DNS by app name. No password (internal-only, see
  # above), no TLS (traffic never leaves the environment's private network).
  redis_host = azurerm_container_app.valkey.name

  # Connection URLs (Terraform-owned; it knows the managed endpoints). The API connects as the
  # least-priv RLS role; the worker + migrations as the owner.
  url_app    = "postgresql+asyncpg://nexus_app:${local.app_password}@${local.pg_fqdn}:5432/nexus"
  url_owner  = "postgresql+asyncpg://nexus:${local.pg_password}@${local.pg_fqdn}:5432/nexus"
  url_redis = "redis://${local.redis_host}:6379/0"

  # POSTGRES_PASSWORD is only the DB master password (set on the server) — not needed in the apps.
  shared = { for k, v in var.secrets : k => v if k != "POSTGRES_PASSWORD" }

  app_secrets = merge(local.shared, {
    NEXUS_DATABASE_URL = local.url_app
    NEXUS_DB_OWNER_URL = local.url_owner
    NEXUS_REDIS_URL    = local.url_redis
  })
  worker_secrets = merge(local.shared, {
    NEXUS_DATABASE_URL = local.url_owner # worker runs cross-tenant sweeps as the owner
    NEXUS_REDIS_URL    = local.url_redis
  })
}

# ACA secret names must be lowercase alphanumeric + '-'; env var names stay as-is and reference them.
resource "azurerm_container_app" "app" {
  name                         = "${local.name}-app"
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"

  registry {
    server               = azurerm_container_registry.main.login_server
    username             = azurerm_container_registry.main.admin_username
    password_secret_name = "acr-password"
  }

  secret {
    name  = "acr-password"
    value = azurerm_container_registry.main.admin_password
  }
  dynamic "secret" {
    for_each = local.app_secrets
    content {
      name  = lower(replace(secret.key, "_", "-"))
      value = secret.value
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "auto"
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = var.app_min
    max_replicas = var.app_max

    container {
      name   = "app"
      image  = var.image
      cpu    = var.app_cpu
      memory = var.app_memory
      # args overrides the Docker CMD (not the ENTRYPOINT) so entrypoint.sh still runs migrations.
      #
      # --workers 1, deliberately. A second uvicorn process is wrong on Container Apps for three
      # separate reasons, any one of which is sufficient:
      #   * It DOUBLES database connections per replica against a hard server limit (see pg_sku).
      #   * ACA scales by REPLICA on the http_scale_rule below. In-container process
      #     multiplication hides load from that signal, so the platform scales later than it
      #     should while the container is already saturated.
      #   * Multiple processes each keep their own Prometheus registry, so /metrics reports
      #     roughly half the traffic and flaps between workers unless PROMETHEUS_MULTIPROC_DIR is
      #     set and swept. One process needs none of that machinery (contrast with
      #     deploy/docker-compose.prod.yml, which runs 2 workers and therefore does).
      args = ["uvicorn", "nexus.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]

      dynamic "env" {
        for_each = local.app_secrets
        content {
          name        = env.key
          secret_name = lower(replace(env.key, "_", "-"))
        }
      }
      dynamic "env" {
        for_each = merge(var.common_env, { NEXUS_RUN_MIGRATIONS = "1" })
        content {
          name  = env.key
          value = env.value
        }
      }

      liveness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/health"
      }
      readiness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/ready"
      }
    }

    http_scale_rule {
      name                = "http"
      concurrent_requests = 50
    }
  }

  # Valkey must be serving before the app boots: the queue client connects at startup, and the
  # readiness probe on /ready would otherwise flap while Valkey is still coming up.
  depends_on = [azurerm_postgresql_flexible_server_database.nexus, azurerm_container_app.valkey]

  # Azure defaults workload_profile_name to "Consumption" server-side; config does not set it, so
  # every plan shows `"Consumption" -> null` forever. Same class of phantom diff as
  # infrastructure_resource_group_name on the environment (see platform.tf).
  lifecycle {
    ignore_changes = [workload_profile_name, tags]
  }
}

resource "azurerm_container_app" "worker" {
  name                         = "${local.name}-worker"
  resource_group_name          = azurerm_resource_group.main.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"

  registry {
    server               = azurerm_container_registry.main.login_server
    username             = azurerm_container_registry.main.admin_username
    password_secret_name = "acr-password"
  }

  secret {
    name  = "acr-password"
    value = azurerm_container_registry.main.admin_password
  }
  dynamic "secret" {
    for_each = local.worker_secrets
    content {
      name  = lower(replace(secret.key, "_", "-"))
      value = secret.value
    }
  }

  template {
    min_replicas = 1
    max_replicas = 1 # idempotent single consumer; scale on a queue-depth rule when needed

    container {
      name   = "worker"
      image  = var.image
      cpu    = var.worker_cpu
      memory = var.worker_memory
      args   = ["python", "-m", "nexus.workers.worker"]

      dynamic "env" {
        for_each = local.worker_secrets
        content {
          name        = env.key
          secret_name = lower(replace(env.key, "_", "-"))
        }
      }
      dynamic "env" {
        for_each = var.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  depends_on = [azurerm_container_app.app] # let the app run migrations first

  # Azure defaults workload_profile_name to "Consumption" server-side; config does not set it, so
  # every plan shows `"Consumption" -> null` forever. Same class of phantom diff as
  # infrastructure_resource_group_name on the environment (see platform.tf).
  lifecycle {
    ignore_changes = [workload_profile_name, tags]
  }
}
