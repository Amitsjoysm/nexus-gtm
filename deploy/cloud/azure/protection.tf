# ============================ Deletion protection ============================
#
# The database holds every tenant's users, accounts, contacts and billing history. Backups make
# data loss RECOVERABLE; these locks make the common accidents IMPOSSIBLE. They are different
# controls and neither substitutes for the other — a restore still costs an outage and loses
# everything since the last restore point.
#
# WHAT AN AZURE MANAGEMENT LOCK ACTUALLY DOES, stated precisely, because it is routinely
# overestimated:
#
#   * CanNotDelete blocks the DELETE operation on the resource, for EVERY caller — the portal, the
#     CLI, Terraform, and the CI service principal alike. It is enforced by Azure Resource Manager,
#     not by our tooling, so it holds even when the caller is Owner on the subscription.
#   * It does NOT make the data read-only. Writes, migrations and DROP TABLE inside the database
#     are completely unaffected — those are Postgres operations, and ARM cannot see them. RLS and
#     the least-privilege `nexus_app` role are what constrain those.
#   * It is REMOVABLE by anyone holding Microsoft.Authorization/locks/delete (Owner or User Access
#     Administrator). That is the point: deletion becomes a deliberate two-step act with its own
#     audit entry, rather than one confirmation dialog at the end of a long day.
#
# `terraform destroy` FAILS while these exist, by design. The documented removal sequence is in
# docs/deployment/13-DATABASE-ACCESS.md; that friction is the feature.
#
# PRODUCTION ONLY. Staging exists to be rebuilt — locking it would make the environment whose
# whole purpose is disposability the awkward one to dispose of, and operators route around
# friction that protects nothing. `count` is the switch.
locals {
  # A lock is warranted wherever real customer data lives. Only production qualifies today; the
  # condition is written on `var.env` rather than a separate boolean so a future `prod-eu` cannot
  # come up unprotected because somebody forgot to set a flag.
  protect = var.env == "prod" ? 1 : 0
}

# The database server. The single most valuable resource in the estate and the only one whose
# loss is not recoverable by re-running a pipeline.
resource "azurerm_management_lock" "postgres" {
  count      = local.protect
  name       = "${local.name}-pg-nodelete"
  scope      = azurerm_postgresql_flexible_server.main.id
  lock_level = "CanNotDelete"
  notes      = "Production customer data. Remove deliberately: az lock delete --name ${local.name}-pg-nodelete --resource-group ${azurerm_resource_group.main.name} --resource ${local.pg_name} --resource-type Microsoft.DBforPostgreSQL/flexibleServers"

  # CREATED LAST, EXPLICITLY. `scope` already makes this depend on the server, but the database
  # and the server configuration below are siblings — Terraform may create them in parallel with
  # this lock. Creating a database inside a locked server is a create, not a delete, so it is
  # permitted; this ordering costs nothing and removes the question entirely.
  depends_on = [
    azurerm_postgresql_flexible_server_database.nexus,
    azurerm_postgresql_flexible_server_configuration.no_tls,
  ]
}

# ---------------------------------------------------------------------------------------------
# THERE IS DELIBERATELY NO RESOURCE-GROUP-SCOPED LOCK HERE, AND THAT IS A CORRECTION.
#
# One existed. It broke the first deployment that used it, and the failure is worth recording
# because the reasoning that produced it is superficially sound.
#
# The intent was defence in depth: `az group delete` destroys an entire estate with one command,
# so locking the group refuses that up front rather than letting it delete what it can and stop
# at the database lock.
#
# What actually happens: `CanNotDelete` at GROUP scope applies to every child resource and blocks
# any operation ARM implements as a delete — which includes operations that are not deletions at
# all. Creating a VNet-injected PostgreSQL Flexible Server writes a service-association link into
# the delegated subnet, and ARM refuses it:
#
#   Operation on resource gtm-prod-vnet/subnets/db under resource type VirtualNetworks is
#   blocking by customer lock ... Lock Level: CanNotDelete
#
# Measured 2026-08-28 on a real first deploy. The message names a SUBNET and a DELETE lock during
# a database CREATE, so it points at neither the cause nor the fix. Ordering the lock last would
# have hidden it at create time and left it to reappear on the first apply that touches the
# subnet — which is worse, because that one happens under time pressure.
#
# The server lock above is the control that matters: it protects the DATA, which is the only
# thing here that cannot be rebuilt by re-running a pipeline. Everything else in this group is
# reproducible from this Terraform.
#
# If you still want the group locked, apply it BY HAND after the estate is fully built, and know
# that it must be removed before any apply that alters the network:
#
#   az lock create --name gtm-prod-rg-nodelete --resource-group gtm-prod-rg \
#     --lock-type CanNotDelete --notes "Manual. Remove before subnet changes."
#
# It is not in Terraform because Terraform would recreate it on every apply, reintroducing the
# failure above each time the network changes.
# ---------------------------------------------------------------------------------------------
