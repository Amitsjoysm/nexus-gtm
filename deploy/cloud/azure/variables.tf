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
}

variable "common_env" {
  type = map(string)
  default = {
    NEXUS_ENV           = "prod"
    NEXUS_QUEUE_BACKEND = "redis"
  }
}
