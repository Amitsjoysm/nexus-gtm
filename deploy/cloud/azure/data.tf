locals {
  pg_password  = lookup(var.secrets, "POSTGRES_PASSWORD", "")
  app_password = lookup(var.secrets, "NEXUS_APP_DB_PASSWORD", "")
}

# ============================ PostgreSQL Flexible Server ============================
resource "azurerm_postgresql_flexible_server" "main" {
  name                          = local.pg_name # globally unique — see local.uniq in platform.tf
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_resource_group.main.location
  version                       = var.pg_version
  delegated_subnet_id           = azurerm_subnet.db.id
  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
  administrator_login           = "nexus"
  administrator_password        = local.pg_password
  sku_name                      = var.pg_sku
  storage_mb                    = var.pg_storage_mb
  backup_retention_days = var.pg_backup_retention_days

  # DECIDE THIS NOW OR REBUILD THE SERVER LATER. `geo_redundant_backup_enabled` is settable ONLY
  # AT CREATION on Flexible Server — Azure refuses to toggle it on a live server, so turning it on
  # afterwards means provisioning a new server, dumping, restoring and re-pointing the app. It is
  # the one setting in this file where the default costs a migration rather than an edit.
  #
  # What it buys: backups replicated to the Azure paired region, so the estate survives losing the
  # whole primary region — and `geo-restore` becomes available. Without it, backups live only in
  # the primary region and a regional failure takes the data with it.
  # What it costs: roughly the price of the backup storage again. On a 32 GB server that is a
  # rounding error against the compute, which is why prod defaults ON despite Stage-0 frugality.
  geo_redundant_backup_enabled = var.pg_geo_redundant_backup

  public_network_access_enabled = false

  # zone deliberately UNSET. Pinning a zone narrows the SKU/region/zone combination Azure must
  # satisfy, and a zone without capacity for the chosen SKU makes an otherwise-valid server
  # unprovisionable. With HA off (pg_ha_enabled = false at Stage 0) a specific zone buys nothing —
  # there is no standby to place in a different one. Let Azure choose a zone with capacity.
  # Set this only alongside high_availability, where the standby zone must differ.
  # zone                        = "1"

  dynamic "high_availability" {
    for_each = var.pg_ha_enabled ? [1] : []
    content {
      mode                      = "ZoneRedundant"
      standby_availability_zone = "2"
    }
  }

  # ZONE IS AZURE'S TO OWN, NOT TERRAFORM'S.
  #
  # With `zone` unset, Azure assigns whichever zone has capacity — which is the point (pinning a
  # zone can make an otherwise-valid SKU unprovisionable). But Terraform then reads back the
  # assigned value, compares it with the null in config, and proposes a change. Azure refuses it:
  #   `zone` can only be changed when exchanged with the zone specified in
  #   `high_availability.0.standby_availability_zone`
  # so every subsequent plan is permanently dirty and every apply fails on a server that is
  # perfectly healthy. Hardcoding a zone instead just moves the problem: the correct value is
  # whatever Azure happened to pick, which is not knowable before the create.
  #
  # ignore_changes is the honest expression of "Azure decides this": set at create, never
  # reconciled afterwards. Remove it only alongside high_availability, where the standby zone
  # must differ from the primary and both become deliberate choices.
  lifecycle {
    ignore_changes = [zone]
  }

  depends_on = [azurerm_private_dns_zone_virtual_network_link.postgres]
}

resource "azurerm_postgresql_flexible_server_database" "nexus" {
  name      = "nexus"
  server_id = azurerm_postgresql_flexible_server.main.id
  collation = "en_US.utf8"
  charset   = "UTF8"
}

# In-VNet private traffic only — disable forced TLS to keep the asyncpg DSN simple (no certs).
resource "azurerm_postgresql_flexible_server_configuration" "no_tls" {
  name      = "require_secure_transport"
  server_id = azurerm_postgresql_flexible_server.main.id
  value     = "OFF"
}

# ============================ Redis / Valkey ============================
# There is deliberately NO managed cache resource here.
#
# Azure Cache for Redis (Microsoft.Cache/Redis, `azurerm_redis_cache`) is RETIRED: as of
# 2026-08 a create returns
#   400 BadRequest: Azure Cache for Redis is retiring, create Azure Managed Redis instead.
# That is the resource TYPE being refused, not a deprecated argument - renaming
# `enable_non_ssl_port` does nothing for it.
#
# The successor, Azure Managed Redis (Microsoft.Cache/redisEnterprise), starts around $40-90/mo
# and, on the azurerm ~> 3.110 pin here, likely exposes only the Enterprise_E* SKUs at ~$300/mo.
# Against a ~$90/mo total budget for 10-15 users that is the single largest line item, for a
# component used in exactly two places: the job queue (nexus/workers/queue.py) and the
# idempotency store (nexus/core/idempotency.py).
#
# So Valkey runs as an internal-only Container App instead - see azurerm_container_app.valkey in
# container_apps.tf. Same valkey:8-alpine image deploy/docker-compose.prod.yml already runs.
