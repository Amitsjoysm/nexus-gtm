output "app_fqdn" {
  value       = azurerm_container_app.app.latest_revision_fqdn
  description = "Default ACA ingress FQDN. CNAME var.domain to this and bind a managed certificate."
}

output "app_default_url" {
  value = "https://${azurerm_container_app.app.latest_revision_fqdn}"
}

output "acr_login_server" {
  value = azurerm_container_registry.main.login_server
}

output "postgres_fqdn" {
  value     = azurerm_postgresql_flexible_server.main.fqdn
  sensitive = true
}

output "custom_domain_next_step" {
  value = "Bind ${var.domain}: CNAME -> ${azurerm_container_app.app.latest_revision_fqdn}, then add an azurerm_container_app_custom_domain + managed certificate."
}
