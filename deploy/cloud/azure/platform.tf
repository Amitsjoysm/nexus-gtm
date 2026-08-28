data "azurerm_client_config" "current" {}

locals {
  # ACR names are GLOBALLY unique across all of Azure, not just your subscription — and
  # "nexusprodacr" is exactly the name every other nexus/prod deployment would also compute,
  # so it was already taken. The suffix is derived from the subscription id rather than from
  # `random_string` on purpose:
  #
  #   * DETERMINISTIC. It resolves to the same value on every plan and apply, so the registry is
  #     never proposed for replacement. A random_string would need `keepers` to avoid exactly
  #     that, and getting those wrong destroys the registry your images live in.
  #   * KNOWN AT PLAN TIME, so the name appears in the plan instead of "(known after apply)".
  #   * No extra provider, so no re-`init` and no new lock entry.
  #
  # Max length is 50 alphanumeric characters; this yields ~20.
  #
  # THREE resources in this stack take a globally-unique name, and all three were measured to
  # collide on the obvious value: ACR `nexusprodacr` was taken, Redis `nexus-prod-redis` was
  # taken. Postgres has the same exposure (its name becomes a public DNS label) and simply had
  # not been reached yet. So the suffix is defined ONCE here and applied to all three, rather
  # than bolted on to each as it fails.
  #
  # Redis is the sharpest case: Azure RESERVES a cache name even after deletion, and the error
  # says recovering it requires a support ticket. A predictable name is therefore not merely
  # inconvenient — it can be permanently unusable through no fault of yours.
  # var.name_suffix wins when set — that is the escape hatch after a destroy, since Azure keeps
  # the old Redis name reserved and the derived value would collide with it.
  uniq = var.name_suffix != "" ? var.name_suffix : substr(sha1(data.azurerm_client_config.current.subscription_id), 0, 8)

  acr_name   = substr("${replace("${local.name}acr", "-", "")}${local.uniq}", 0, 50)
  pg_name    = "${local.name}-pg-${local.uniq}"
  redis_name = "${local.name}-redis-${local.uniq}"
}

# ============================ Container registry ============================
#
# ONE REGISTRY IS SHARED BY STAGING AND PRODUCTION, and that is a correctness requirement rather
# than a saving.
#
# azure-pipelines-cd.yml promotes a release by deploying THE SAME IMAGE DIGEST to staging, then
# to production, with no rebuild in between — that is what makes "this artifact was tested" a
# fact the Verify stage can check rather than an assumption. Give each environment its own
# registry and promotion becomes a COPY between registries, which produces a new artifact and
# quietly removes the guarantee. The pipeline would still be green; it would just no longer be
# testing what it ships.
#
# So: production creates the registry, staging consumes it by name.
#
#   var.acr_shared_name == ""   -> create one here (production, and any standalone deploy)
#   var.acr_shared_name != ""   -> look up the existing one (staging points at production's)
#
# Defaulting to "create" keeps a single-environment deployment working exactly as before this
# existed — the same additive posture the rest of this stack takes.
resource "azurerm_container_registry" "main" {
  count               = var.acr_shared_name == "" ? 1 : 0
  name                = local.acr_name
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  # Basic at Stage 0: 10 GB included storage and the same API surface as Standard. The only
  # practical differences are included storage and throughput, neither of which binds for a
  # single image with a handful of tags per week. Basic -> Standard is a tier change with no
  # re-push and no downtime.
  sku           = "Basic"
  admin_enabled = true
}

# The shared registry, when this environment consumes one instead of owning it (staging).
# `data` rather than a second resource: staging must never be able to create, modify or —
# critically — DESTROY the registry that holds production's images. A `terraform destroy` of
# staging would otherwise take production's entire image history with it, including every tag
# the Rollback stage relies on to recover.
data "azurerm_container_registry" "shared" {
  count               = var.acr_shared_name == "" ? 0 : 1
  name                = var.acr_shared_name
  resource_group_name = var.acr_shared_resource_group

  # Naming the missing variable beats Azure's own error, which reports "registry not found" and
  # names the REGISTRY — sending an operator to check a registry that exists perfectly well while
  # the actual fault is an empty resource group in their tfvars.
  lifecycle {
    precondition {
      condition     = trimspace(var.acr_shared_resource_group) != ""
      error_message = "acr_shared_name is set but acr_shared_resource_group is empty. Both are required to consume an existing registry. Find them with: az acr list -o table"
    }
  }
}

# One place the rest of the stack reads the registry from, whichever branch produced it. Without
# this indirection every consumer would need its own `var.acr_shared_name == ""` conditional, and
# the first one written wrongly would be an authentication failure at container pull time — which
# surfaces as a container that will not start, not as a registry error.
locals {
  acr = var.acr_shared_name == "" ? azurerm_container_registry.main[0] : data.azurerm_container_registry.shared[0]
}

# Container Apps environment (VNet-integrated so it can reach the private DB + Redis).
resource "azurerm_container_app_environment" "main" {
  name                       = "${local.name}-aca"
  resource_group_name        = azurerm_resource_group.main.name
  location                   = azurerm_resource_group.main.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  infrastructure_subnet_id   = azurerm_subnet.aca.id

  # WITHOUT THIS, EVERY APPLY DESTROYS AND RECREATES THE ENVIRONMENT.
  #
  # Azure auto-populates `infrastructure_resource_group_name` with the managed group it creates
  # (ME_nexus-prod-aca_nexus-prod-rg_eastus2). Config never sets it, so Terraform reads the server
  # value, compares it with null, and finds a diff on a ForceNew attribute:
  #   - infrastructure_resource_group_name = "ME_..." -> null # forces replacement
  # It cannot be set in config either — the provider only accepts it alongside a workload_profile
  # block, which a Consumption-only environment does not have.
  #
  # Left unfixed this is not cosmetic: replacing the environment takes ~10 minutes to delete plus
  # ~3 to recreate, and CASCADES — every azurerm_container_app in it is replaced too, because
  # container_app_environment_id becomes "known after apply". That is a full outage of the app,
  # the worker and the Valkey queue on every routine deploy.
  #
  # tags is the same shape of noise ({} vs null) without the ForceNew consequence.
  lifecycle {
    ignore_changes = [infrastructure_resource_group_name, tags]
  }
}
