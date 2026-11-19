variable "project_id" {
  description = "GCP project id"
  type        = string
}

variable "region" {
  description = "GCP region for the instance"
  type        = string
}

variable "instance_name" {
  description = "Cloud SQL instance name"
  type        = string
  default     = "agentos-postgres"
}

variable "database_name" {
  description = "Application database name"
  type        = string
  default     = "agentos"
}

variable "tier" {
  description = "Instance machine tier"
  type        = string
  default     = "db-g1-small"
}

variable "availability_type" {
  description = "ZONAL for dev, REGIONAL for staging/prod"
  type        = string
  default     = "ZONAL"
}

variable "disk_size_gb" {
  description = "Storage size in GB"
  type        = number
  default     = 25
}

variable "network_id" {
  description = "VPC network self-link for private IP"
  type        = string
}

variable "iam_user_emails" {
  description = "Service account emails granted IAM database auth"
  type        = list(string)
}

variable "app_user_name" {
  description = "Application database username (password auth via the proxy)"
  type        = string
  default     = "agentos"
}

variable "deletion_protection" {
  description = "Cloud SQL deletion protection flag"
  type        = bool
  default     = true
}
