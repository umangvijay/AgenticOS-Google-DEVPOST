variable "project_id" {
  description = "Google Cloud Project ID for the GCP/PostgreSQL target"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "us-central1"
}

variable "api_image" {
  description = "Artifact Registry image URL for the API (from cloudbuild.yaml)"
  type        = string
}

variable "worker_image" {
  description = "Artifact Registry image URL for the worker"
  type        = string
}

variable "artifacts_bucket_name" {
  description = "Override for the artifact bucket name"
  type        = string
  default     = ""
}

variable "api_public" {
  description = "Whether the API is publicly invocable (dev/staging: true; qualification demands the ingress decision be explicit)"
  type        = bool
  default     = true
}

variable "api_max_instances" {
  type    = number
  default = 10
}

variable "worker_instances" {
  description = "Manual worker-pool instance count"
  type        = number
  default     = 1
}

variable "message_bus_transport" {
  description = "Bus transport for this target (nats or memory-pull fallback)"
  type        = string
  default     = "nats"
}

variable "nats_servers" {
  description = "NATS endpoint for the worker pool"
  type        = string
  default     = ""
}

variable "shared_env_vars" {
  description = "Additional env shared by API and worker"
  type        = map(string)
  default     = {}
}
