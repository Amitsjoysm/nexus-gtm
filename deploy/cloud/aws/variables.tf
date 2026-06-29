variable "region" {
  type        = string
  default     = "us-east-1"
  description = "AWS region."
}

variable "project" {
  type        = string
  default     = "nexus"
  description = "Name prefix for all resources."
}

variable "env" {
  type        = string
  default     = "prod"
  description = "Environment label."
}

variable "domain" {
  type        = string
  description = "Public hostname to serve, e.g. app.example.com. Its parent zone must exist in Route 53."
}

variable "route53_zone_name" {
  type        = string
  description = "The Route 53 hosted zone the domain lives in, e.g. example.com."
}

variable "image" {
  type        = string
  description = "Full ECR image URI:tag for the app/worker (set by deploy.sh after the first push)."
  default     = "" # deploy.sh fills this; a placeholder image is used until the real one is pushed
}

# ---- Container image used purely to bootstrap before the real image exists ----
variable "bootstrap_image" {
  type        = string
  default     = "public.ecr.aws/docker/library/busybox:1.36"
  description = "Throwaway image so the cluster can be created before the app image is pushed."
}

# ---- Sizing (Fargate CPU is in vCPU units: 256=0.25 vCPU; memory in MiB) ----
variable "app_cpu" { default = 512 }
variable "app_memory" { default = 1024 }
variable "app_desired" { default = 2 }
variable "app_min" { default = 2 }
variable "app_max" { default = 10 }
variable "worker_cpu" { default = 256 }
variable "worker_memory" { default = 512 }
variable "postgres_cpu" { default = 1024 }
variable "postgres_memory" { default = 2048 }
variable "valkey_cpu" { default = 256 }
variable "valkey_memory" { default = 512 }

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "alarm_email" {
  type        = string
  default     = ""
  description = "Email subscribed to the CloudWatch alarm SNS topic (confirm the subscription)."
}

# ---- Secrets ----
# A map of ENV_VAR_NAME => value, injected into the containers from SSM SecureString. Populated by
# deploy.sh from deploy/.env (which composes NEXUS_DATABASE_URL / NEXUS_DB_OWNER_URL / NEXUS_REDIS_URL
# from the passwords + the in-cluster DNS names). NEVER commit real values; pass via TF_VAR_secrets.
variable "secrets" {
  type      = map(string)
  sensitive = true
  default   = {}
}

# Non-secret env shared by app + worker (service-specific bits are set in services.tf).
variable "common_env" {
  type = map(string)
  default = {
    NEXUS_ENV           = "prod"
    NEXUS_QUEUE_BACKEND = "redis"
  }
}

# ---- VARIANT (a): managed data tier -----------------------------------------------------------
# false (default) = self-hosted Postgres + Valkey on Fargate + EFS (cheapest).
# true            = managed RDS Multi-AZ + ElastiCache (HA, backups, patching handled). The two
#                   self-hosted ECS services are skipped and the connection URLs point at the
#                   managed endpoints. This is the recommended production posture.
variable "use_managed_data" {
  type    = bool
  default = false
}

# RDS (only when use_managed_data = true)
variable "rds_instance_class" {
  type    = string
  default = "db.t4g.medium"
}
variable "rds_allocated_storage" {
  type    = number
  default = 50
}
variable "rds_max_allocated_storage" {
  type    = number
  default = 200 # storage autoscaling ceiling
}
variable "rds_multi_az" {
  type    = bool
  default = true
}
variable "rds_backup_retention_days" {
  type    = number
  default = 14
}
variable "rds_deletion_protection" {
  type    = bool
  default = true
}

# ElastiCache (only when use_managed_data = true)
variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}
variable "redis_replicas" {
  type        = number
  default     = 1 # 1 primary + 1 replica across AZs (automatic failover)
  description = "Number of read replicas; >=1 enables automatic failover."
}
