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
  # than nice-to-have. See docs/deployment/05-BACKUP-RESTORE.md.
  #
  # State contains SECRETS IN PLAINTEXT (the Postgres passwords, every API key passed through
  # var.secrets). Hence: no public blob access, TLS 1.2 minimum, and access restricted to the
  # people and the CI identity that genuinely deploy.
  # `key` IS DELIBERATELY ABSENT — this is a PARTIAL backend, and the omission is load-bearing.
  #
  # It used to be hardcoded to "nexus/prod.tfstate". With staging added, a hardcoded key means
  # `terraform apply -var env=staging` writes the STAGING estate into the PRODUCTION state file.
  # Terraform would then see production's resources as absent from config and propose destroying
  # every one of them — the resource group, the database, the registry. The plan reads as a normal
  # create/destroy cycle; nothing announces that two environments are sharing one state.
  #
  # So the key is supplied at init time instead, one file per environment:
  #
  #   terraform init -backend-config="key=nexus/prod.tfstate"
  #   terraform init -backend-config="key=nexus/staging.tfstate"
  #
  # deploy/cloud/deploy.sh derives it from ENV_NAME automatically. The cost of a partial backend
  # is that a BARE `terraform init` now prompts for the key rather than silently choosing one —
  # which is the correct behaviour when picking the wrong one destroys an environment.
  #
  # Terraform WORKSPACES were the alternative and were rejected: `terraform workspace` state is
  # invisible in the command you type, so `terraform apply` in the wrong workspace looks identical
  # to the right one. An explicit key appears in the init command and in the plan header.
  backend "azurerm" {
    resource_group_name  = "nexus-tfstate-rg"
    storage_account_name = "nexustfstate05082026"
    container_name       = "tfstate"
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy = false
    }
  }
}
