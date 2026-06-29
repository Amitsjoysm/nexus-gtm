# Terraform + provider pinning. Remote state belongs in S3 + DynamoDB lock, ENCRYPTED with KMS
# (this stack writes secrets into state). Configure the backend below for anything beyond a POC.
terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }

  # backend "s3" {
  #   bucket         = "infojoy-tfstate"
  #   key            = "nexus/prod/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "infojoy-tflock"
  #   encrypt        = true
  #   kms_key_id     = "alias/terraform-state"
  # }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Env       = var.env
    }
  }
}
