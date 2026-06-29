locals {
  pg_password  = lookup(var.secrets, "POSTGRES_PASSWORD", "")
  app_password = lookup(var.secrets, "NEXUS_APP_DB_PASSWORD", "")
}

# ============================ PostgreSQL Flexible Server ============================
resource "azurerm_postgresql_flexible_server" "main" {
  name                          = "${local.name}-pg"
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_resource_group.main.location
  version                       = "16"
  delegated_subnet_id           = azurerm_subnet.db.id
  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
  administrator_login           = "nexus"
  administrator_password        = local.pg_password
  sku_name                      = var.pg_sku
  storage_mb                    = var.pg_storage_mb
  backup_retention_days         = 14
  geo_redundant_backup_enabled  = false
  public_network_access_enabled = false
  zone                          = "1"

  dynamic "high_availability" {
    for_each = var.pg_ha_enabled ? [1] : []
    content {
      mode                      = "ZoneRedundant"
      standby_availability_zone = "2"
    }
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

# ============================ Azure Cache for Redis ============================
resource "azurerm_redis_cache" "main" {
  name                          = "${local.name}-redis"
  resource_group_name           = azurerm_resource_group.main.name
  location                      = azurerm_resource_group.main.location
  capacity                      = var.redis_capacity
  family                        = "C"
  sku_name                      = var.redis_sku
  non_ssl_port_enabled          = true # plain redis:// on 6379 over the private endpoint
  minimum_tls_version           = "1.2"
  public_network_access_enabled = false
}

resource "azurerm_private_endpoint" "redis" {
  name                = "${local.name}-redis-pe"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  subnet_id           = azurerm_subnet.pe.id

  private_service_connection {
    name                           = "redis"
    private_connection_resource_id = azurerm_redis_cache.main.id
    subresource_names              = ["redisCache"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "redis"
    private_dns_zone_ids = [azurerm_private_dns_zone.redis.id]
  }
}
