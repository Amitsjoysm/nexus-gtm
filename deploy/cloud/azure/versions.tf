# Remote state belongs in an Azure Storage backend, encrypted. Secrets land in state.
terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.110"
    }
  }
  # backend "azurerm" {
  #   resource_group_name  = "infojoy-tfstate"
  #   storage_account_name = "infojoytfstate"
  #   container_name       = "tfstate"
  #   key                  = "nexus/prod.tfstate"
  # }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy = false
    }
  }
}
