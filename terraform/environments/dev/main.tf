terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.37"
    }
  }

  backend "gcs" {
    bucket = "agenticos-tfstate-dev" # This needs to exist or be created outside this config
    prefix = "terraform/state"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# --- Module Invocations ---

# IAM
module "iam" {
  source     = "../../modules/iam"
  project_id = var.project_id
}

# Secrets
module "secrets" {
  source          = "../../modules/secrets"
  project_id      = var.project_id
  api_sa_email    = module.iam.api_sa_email
  worker_sa_email = module.iam.worker_sa_email
}

# Firestore
module "firestore" {
  source      = "../../modules/firestore"
  project_id  = var.project_id
  database    = "(default)"
  location_id = "nam5"
}

# Pub/Sub
module "pubsub" {
  source     = "../../modules/pubsub"
  project_id = var.project_id
}

# Pull-based consumer: this container has no HTTP server, so it must run as a
# Cloud Run Worker Pool rather than as a Cloud Run Service.
resource "google_cloud_run_v2_worker_pool" "worker" {
  name     = "agenticos-worker"
  location = var.region
  project  = var.project_id

  template {
    service_account = module.iam.worker_sa_email
    containers {
      image = var.worker_image
      dynamic "env" {
        for_each = merge(var.shared_env_vars, {
          SERVICE_TYPE         = "WORKER"
          RUN_WORKER           = "1"
          WORKER_ONLY          = "1"
          GOOGLE_CLOUD_PROJECT = var.project_id
          GOOGLE_CLOUD_REGION  = "global"
        })
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = module.secrets.worker_secret_env_mappings
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = element(split("/", env.value), 3)
              version = "latest"
            }
          }
        }
      }
    }
  }
  scaling {
    manual_instance_count = 1
  }
}

# Cloud Run API
module "cloud_run_api" {
  source          = "../../modules/cloud_run"
  project_id      = var.project_id
  region          = var.region
  service_name    = "agenticos-api"
  image           = var.api_image
  service_account = module.iam.api_sa_email
  min_instances   = 0
  max_instances   = 50
  timeout_seconds = 300
  concurrency     = 80
  environment_vars = merge(var.shared_env_vars, {
    "SERVICE_TYPE"         = "API"
    "RUN_WORKER"           = "0"
    "GOOGLE_CLOUD_PROJECT" = var.project_id
    "GOOGLE_CLOUD_REGION"  = "global"
  })
  secrets   = module.secrets.secret_env_mappings
  is_public = true
}

# Cloud Scheduler
module "scheduler" {
  source             = "../../modules/scheduler"
  project_id         = var.project_id
  region             = var.region
  pubsub_topic_id    = module.pubsub.scheduler_topic_id
  scheduler_sa_email = module.iam.scheduler_sa_email
}

# Artifact Registry
module "artifact_registry" {
  source          = "../../modules/artifact_registry"
  project_id      = var.project_id
  region          = var.region
  deploy_sa_email = module.iam.deploy_sa_email
}
