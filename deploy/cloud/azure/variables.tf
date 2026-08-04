variable "location" {
  type    = string
  default = "eastus"
}

variable "project" {
  type    = string
  default = "nexus"
}

variable "env" {
  type    = string
  default = "prod"
}

variable "domain" {
  type        = string
  description = "Public hostname, e.g. app.example.com. Bind it to the app's Container App ingress."
}

variable "image" {
  type        = string
  description = "Full ACR image:tag for app/worker. deploy.sh fills this after pushing."
  default     = "mcr.microsoft.com/k8se/quickstart:latest" # bootstrap placeholder
}

# Sizing
variable "app_cpu" { default = 0.5 }
variable "app_memory" { default = "1Gi" }
variable "app_min" { default = 2 }
variable "app_max" { default = 10 }
variable "worker_cpu" { default = 0.25 }
variable "worker_memory" { default = "0.5Gi" }

# Managed data
variable "pg_sku" {
  type    = string
  default = "GP_Standard_D2ds_v5"
}
variable "pg_storage_mb" {
  type    = number
  default = 32768
}
variable "pg_ha_enabled" {
  type    = bool
  default = true
}
variable "redis_sku" {
  type    = string
  default = "Standard" # Standard = replicated (HA); Basic = single node
}
variable "redis_capacity" {
  type    = number
  default = 1
}

variable "alarm_email" {
  type    = string
  default = ""
}

variable "log_retention_days" {
  type    = number
  default = 30
}

# Raw secrets (passwords + API keys) from deploy/.env via TF_VAR_secrets. Connection URLs are
# composed in container_apps.tf from the managed-service endpoints. Sensitive; never committed.
variable "secrets" {
  type      = map(string)
  sensitive = true
  default   = {}

  # The API must connect as the least-privilege `nexus_app` role — that is what makes Postgres RLS
  # an actual tenant boundary rather than a decoration. Two components read this same value and
  # default it in OPPOSITE directions:
  #
  #   * `scripts/apply_rls.py` treats an empty NEXUS_APP_DB_PASSWORD as "single-role deploy" and
  #     skips creating `nexus_app` altogether.
  #   * `container_apps.tf` composes `nexus_app:<password>@...` regardless.
  #
  # Unvalidated, an empty value provisions the entire stack and then fails at container start,
  # pointed at a role that was never created. Worse, the obvious fix is to point the app at the
  # owner URL — which silently removes RLS enforcement for every tenant, and nothing would report
  # it. Refuse at plan time instead: a deploy that cannot enforce tenant isolation should not begin.
  validation {
    condition     = length(trimspace(lookup(var.secrets, "NEXUS_APP_DB_PASSWORD", ""))) >= 16
    error_message = "NEXUS_APP_DB_PASSWORD must be set (>=16 chars) in deploy/.env. The API connects as the least-privilege nexus_app role, and scripts/apply_rls.py only creates that role when this is set. Generate one with: openssl rand -hex 32"
  }
}

variable "common_env" {
  type = map(string)
  default = {
    NEXUS_ENV           = "prod"
    NEXUS_QUEUE_BACKEND = "redis"
  }
}
