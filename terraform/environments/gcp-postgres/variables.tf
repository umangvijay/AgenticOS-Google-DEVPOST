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
  # The artifact store is per-instance POSIX until the GCS backend lands
  # (recorded in deploy-gcp.md): a second API instance serves downloads
  # from ITS OWN filesystem. Keep 1 until that backend exists.
  type    = number
  default = 1
}

variable "worker_instances" {
  description = "Manual worker-pool instance count"
  type        = number
  default     = 1
}

variable "message_bus_transport" {
  description = "Bus transport for this target. pubsub: the pubsub module provisions every topic the app consumes plus the -worker-sub pull subscriptions. nats requires a NATS deployment this profile does not include."
  type        = string
  default     = "pubsub"
}

variable "frontend_image" {
  description = "Artifact Registry image URL for the frontend (from cloudbuild.frontend.yaml); empty = no frontend service"
  type        = string
  default     = ""
}

variable "shared_env_vars" {
  description = "Additional env shared by API and worker"
  type        = map(string)
  default     = {}
}
