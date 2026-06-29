locals {
  pg_fqdn    = azurerm_postgresql_flexible_server.main.fqdn
  redis_host = azurerm_redis_cache.main.hostname
  redis_key  = azurerm_redis_cache.main.primary_access_key

  # Connection URLs (Terraform-owned; it knows the managed endpoints). The API connects as the
  # least-priv RLS role; the worker + migrations as the owner.
  url_app    = "postgresql+asyncpg://nexus_app:${local.app_password}@${local.pg_fqdn}:5432/nexus"
  url_owner  = "postgresql+asyncpg://nexus:${local.pg_password}@${local.pg_fqdn}:5432/nexus"
  url_redis  = "redis://:${local.redis_key}@${local.redis_host}:6379/0"

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
      args = ["uvicorn", "nexus.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]

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

  depends_on = [azurerm_postgresql_flexible_server_database.nexus, azurerm_private_endpoint.redis]
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
}
