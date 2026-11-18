# GCP / PostgreSQL target overlay (§32.20 target matrix Target 2).
#
# This is the topology the release scope declares: Cloud SQL PostgreSQL
# + GCS artifacts + NATS-capable workers + Cloud Run API/worker pool,
# closing the recorded gap ("no Cloud SQL/GCS resource exists in either
# root"). The Firestore dev overlay remains a SEPARATE profile.
#
# Prerequisites (owner-supplied, outside this config):
#   - terraform state bucket (backend gcs)
#   - a GCP project with billing
#   - images pushed to the Artifact Registry (cloudbuild.yaml output)
#   - Secret Manager values seeded (secrets_master_key, jwt keys,
#     attestation key — same set as the dev overlay's secrets module)

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.37"
    }
  }

  backend "gcs" {
    bucket = "agenticos-tfstate-gcp-postgres" # must pre-exist
    prefix = "terraform/state"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ── shared foundation ──────────────────────────────────────────────

module "iam" {
  source     = "../../modules/iam"
  project_id = var.project_id
}

module "secrets" {
  source             = "../../modules/secrets"
  project_id         = var.project_id
  api_sa_email       = module.iam.api_sa_email
  worker_sa_email    = module.iam.worker_sa_email
}

module "pubsub" {
  source     = "../../modules/pubsub"
  project_id = var.project_id
}

module "artifact_registry" {
  source          = "../../modules/artifact_registry"
  project_id      = var.project_id
  region          = var.region
  deploy_sa_email = module.iam.deploy_sa_email
}

# ── VPC + private services access for Cloud SQL ────────────────────

resource "google_compute_network" "vpc" {
  name                    = "agentos-vpc"
  project                 = var.project_id
  auto_create_subnetworks = false
}

resource "google_compute_global_address" "private_services_range" {
  name          = "agentos-psa-range"
  project       = var.project_id
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "private_services_access" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services_range.name]
}

# Cloud Run services need egress to the internet (Vertex AI, MCP
# sources) — a Cloud NAT on the VPC provides it without public IPs.
resource "google_compute_router" "router" {
  name    = "agentos-router"
  project = var.project_id
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "nat" {
  name                               = "agentos-nat"
  project                            = var.project_id
  region                             = var.region
  router                             = google_compute_router.router.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  depends_on = [google_compute_network.vpc]
}

# ── Cloud SQL (managed PostgreSQL) ─────────────────────────────────

module "cloud_sql" {
  source = "../../modules/cloud_sql"

  project_id      = var.project_id
  region          = var.region
  network_id      = google_compute_network.vpc.id
  iam_user_emails = [module.iam.api_sa_email, module.iam.worker_sa_email]

  depends_on = [google_service_networking_connection.private_services_access]
}

# ── GCS artifacts ──────────────────────────────────────────────────

module "artifacts" {
  source     = "../../modules/gcs_artifacts"
  project_id = var.project_id
  bucket_name = var.artifacts_bucket_name != "" ? var.artifacts_bucket_name : "${var.project_id}-artifacts"

  accessor_sa_emails = [
    module.iam.api_sa_email,
    module.iam.worker_sa_email,
  ]
  restore_verifier_sa_email = module.iam.deploy_sa_email
}

# ── Cloud SQL proxy + env contract shared by API and worker ────────

locals {
  # asyncpg DSN over the auth proxy unix socket; IAM token auth.
  postgres_dsn = "postgresql://${module.iam.api_sa_email}@/agentos?host=/cloudsql/${module.cloud_sql.connection_name}"

  shared_env = merge(var.shared_env_vars, {
    STORAGE_BACKEND = "postgres"
    POSTGRES_DSN    = local.postgres_dsn
    # NATS over the public endpoint until a private-path decision is
    # made (JetStream persistence lives in Cloud SQL's outbox today;
    # the bus transport degrades to in-process pull when NATS is
    # unreachable — recorded in supported-profiles.md).
    MESSAGE_BUS_TRANSPORT = var.message_bus_transport
    NATS_SERVERS          = var.nats_servers
    GOOGLE_CLOUD_PROJECT  = var.project_id
    GOOGLE_CLOUD_REGION   = var.region
    GCS_ARTIFACT_BUCKET   = module.artifacts.bucket_name
  })
}

# ── Cloud Run API (with Cloud SQL auth-proxy sidecar) ──────────────

resource "google_cloud_run_v2_service" "api" {
  name     = "agentos-api"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = module.iam.api_sa_email
    timeout         = "300s"

    containers {
      image = var.api_image

      dynamic "env" {
        for_each = local.shared_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = module.secrets.secret_env_mappings
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

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }
    }

    # Cloud SQL Auth Proxy sidecar: IAM-authenticated unix socket at
    # /cloudsql/<connection_name>; no DB credentials in Secret Manager.
    containers {
      name  = "cloud-sql-proxy"
      image = "gcr.io/cloud-sql-proxy/cloud-sql-proxy:2.14.0"
      args  = ["--private-ip", module.cloud_sql.connection_name]

      resources {
        limits = {
          cpu    = "0.5"
          memory = "512Mi"
        }
      }
    }

    scaling {
      min_instance_count = 0
      max_instance_count = var.api_max_instances
    }
  }

  depends_on = [module.cloud_sql]
}

resource "google_cloud_run_v2_service_iam_member" "api_public" {
  count = var.api_public ? 1 : 0

  name     = google_cloud_run_v2_service.api.name
  location = var.region
  project  = var.project_id
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ── Cloud Run worker pool (pull-based, same sidecar) ───────────────

resource "google_cloud_run_v2_worker_pool" "worker" {
  name     = "agentos-worker"
  location = var.region
  project  = var.project_id

  template {
    service_account = module.iam.worker_sa_email

    containers {
      image = var.worker_image

      dynamic "env" {
        for_each = merge(local.shared_env, {
          SERVICE_TYPE = "WORKER"
          RUN_WORKER   = "1"
          WORKER_ONLY  = "1"
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

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
      }
    }

    containers {
      name  = "cloud-sql-proxy"
      image = "gcr.io/cloud-sql-proxy/cloud-sql-proxy:2.14.0"
      args  = ["--private-ip", module.cloud_sql.connection_name]

      resources {
        limits = {
          cpu    = "0.5"
          memory = "512Mi"
        }
      }
    }
  }

  scaling {
    manual_instance_count = var.worker_instances
  }

  depends_on = [module.cloud_sql]
}

# ── Scheduler (same contract as the dev overlay) ───────────────────

module "scheduler" {
  source             = "../../modules/scheduler"
  project_id         = var.project_id
  region             = var.region
  pubsub_topic_id    = module.pubsub.scheduler_topic_id
  scheduler_sa_email = module.iam.scheduler_sa_email
}
