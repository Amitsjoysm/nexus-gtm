# Remote state belongs in an Azure Storage backend, encrypted. Secrets land in state.
terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.110"
    }
  }
  # Remote state. NOT optional and NOT a preference:
  #
  #   * State is the only record of what infrastructure exists. On a laptop it is one disk
  #     failure or one `git clean` away from an estate nobody can modify or destroy.
  #   * CI cannot deploy against state it cannot read. The moment azure-pipelines-cd.yml or any
  #     second operator runs Terraform, local state means two people apply against two different
  #     pictures of reality and silently clobber each other.
  #   * The backend takes a LEASE on the blob for the duration of an apply, which is what stops
  #     two concurrent applies from interleaving. Local state has no locking at all.
  #
  # This account has blob VERSIONING and 30-day soft delete enabled — that is the recovery story
  # for a corrupted or truncated state file, and it is why those settings are mandatory rather
  # than nice-to-have. See docs/deployment/21-BACKUP-RESTORE.md.
  #
  # State contains SECRETS IN PLAINTEXT (the Postgres passwords, every API key passed through
  # var.secrets). Hence: no public blob access, TLS 1.2 minimum, and access restricted to the
  # people and the CI identity that genuinely deploy.
  backend "azurerm" {
    resource_group_name  = "nexus-tfstate-rg"
    storage_account_name = "nexustfstate05082026"
    container_name       = "tfstate"
    key                  = "nexus/prod.tfstate"
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy = false
    }
  }
}
