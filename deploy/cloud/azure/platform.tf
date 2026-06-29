# Container registry (admin creds for a simple ACA pull; managed identity is the hardening path).
resource "azurerm_container_registry" "main" {
  name                = replace("${local.name}acr", "-", "") # ACR names are alphanumeric + globally unique
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Standard"
  admin_enabled       = true
}

# Container Apps environment (VNet-integrated so it can reach the private DB + Redis).
resource "azurerm_container_app_environment" "main" {
  name                       = "${local.name}-aca"
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  infrastructure_subnet_id   = azurerm_subnet.aca.id
}
