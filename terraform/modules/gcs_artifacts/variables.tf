variable "project_id" {
  type = string
}

variable "bucket_name" {
  description = "Artifact bucket name (globally unique)"
  type        = string
}

variable "location" {
  description = "Bucket location (region or multi-region)"
  type        = string
  default     = "US"
}

variable "accessor_sa_emails" {
  description = "Service accounts that read/write artifacts (api, worker)"
  type        = list(string)
}

variable "restore_verifier_sa_email" {
  description = "Optional separate restore-drill reader identity"
  type        = string
  default     = ""
}

variable "noncurrent_retention_days" {
  description = "Days to retain noncurrent object versions"
  type        = number
  default     = 90
}

variable "abandoned_prefix_age_days" {
  description = "Age after which incomplete multipart uploads abort"
  type        = number
  default     = 7
}
