# THE APP FQDN, NOT A REVISION FQDN.
#
# `latest_revision_fqdn` embeds the revision name (nexus-prod-app--3acvd46...). It is correct only
# until the next deploy: creating a new revision retires the old one, and the previous hostname
# starts returning ACA's own "Azure Container App - Unavailable" 404 page — an HTML error that
# looks like the application is broken rather than like a stale URL.
#
# `ingress[0].fqdn` is the stable per-app hostname that always routes to whatever revision is
# live. It is also the correct CNAME target for a custom domain, for the same reason: a DNS record
# pointing at a revision would break on the next release.
output "app_fqdn" {
  value       = azurerm_container_app.app.ingress[0].fqdn
  description = "Stable ACA ingress FQDN. CNAME var.domain to this and bind a managed certificate."
}

output "app_default_url" {
  value = "https://${azurerm_container_app.app.ingress[0].fqdn}"
}

output "acr_login_server" {
  value = azurerm_container_registry.main.login_server
}

output "postgres_fqdn" {
  value     = azurerm_postgresql_flexible_server.main.fqdn
  sensitive = true
}

output "custom_domain_next_step" {
  # Same reasoning as app_fqdn above: a CNAME pointing at a REVISION hostname breaks on the next
  # release, which is the worst possible time to discover a DNS mistake.
  value = "Bind ${var.domain}: CNAME -> ${azurerm_container_app.app.ingress[0].fqdn}, then add an azurerm_container_app_custom_domain + managed certificate."
}
