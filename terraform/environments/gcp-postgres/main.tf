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
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
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

# ── Service enablement for THIS topology ───────────────────────────
# The root main.tf enables firestore/secretmanager/cloudrun/pubsub for
# the Firestore profile; the GCP/PostgreSQL target needs its own APIs.
# disable_on_destroy=false keeps a re-apply from shutting shared APIs.
resource "google_project_service" "sqladmin" {
  service            = "sqladmin.googleapis.com"
  disable_on_destroy = false
}
resource "google_project_service" "compute" {
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}
resource "google_project_service" "servicenetworking" {
  service            = "servicenetworking.googleapis.com"
  disable_on_destroy = false
}
resource "google_project_service" "artifactregistry" {
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = false
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

  depends_on = [
    google_service_networking_connection.private_services_access,
    google_project_service.sqladmin,
    google_project_service.compute,
  ]
}

# ── Application database credentials ───────────────────────────────
# The app reads ONE env var (POSTGRES_DSN). Assemble the socket DSN with
# the generated password, store it in Secret Manager, and wire it as a
# secret-backed env var on the API and worker — the password never
# appears in plain env vars, logs, or the DSN of a plain variable.
resource "google_secret_manager_secret" "postgres_dsn" {
  secret_id = "agenticos-postgres-dsn"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [module.secrets]
}

locals {
  postgres_dsn_secret = "postgresql://${module.cloud_sql.app_user_name}@/agentos?host=/cloudsql/${module.cloud_sql.connection_name}"
}

resource "google_secret_manager_secret_version" "postgres_dsn" {
  secret      = google_secret_manager_secret.postgres_dsn.id
  secret_data = "${local.postgres_dsn_secret}&password=${module.cloud_sql.app_user_password}"

  depends_on = [module.cloud_sql]
}

resource "google_secret_manager_secret_iam_member" "postgres_dsn_api" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.postgres_dsn.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${module.iam.api_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "postgres_dsn_worker" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.postgres_dsn.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${module.iam.worker_sa_email}"
}

# ── Pub/Sub transport grants ───────────────────────────────────────
# The app's PubSubMessageBus publishes to and consumes from the topics
# the pubsub module creates; the service accounts need the project-level
# roles or every publish/consume is DENIED.
resource "google_project_iam_member" "api_pubsub_publisher" {
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${module.iam.api_sa_email}"
}

resource "google_project_iam_member" "api_pubsub_subscriber" {
  project = var.project_id
  role    = "roles/pubsub.subscriber"
  member  = "serviceAccount:${module.iam.api_sa_email}"
}

resource "google_project_iam_member" "api_pubsub_viewer" {
  project = var.project_id
  role    = "roles/pubsub.viewer"
  member  = "serviceAccount:${module.iam.api_sa_email}"
}

resource "google_project_iam_member" "worker_pubsub_publisher" {
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${module.iam.worker_sa_email}"
}

resource "google_project_iam_member" "worker_pubsub_subscriber" {
  project = var.project_id
  role    = "roles/pubsub.subscriber"
  member  = "serviceAccount:${module.iam.worker_sa_email}"
}

resource "google_project_iam_member" "worker_pubsub_viewer" {
  project = var.project_id
  role    = "roles/pubsub.viewer"
  member  = "serviceAccount:${module.iam.worker_sa_email}"
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
  shared_env = merge(var.shared_env_vars, {
    STORAGE_BACKEND = "postgres"
    # POSTGRES_DSN is SECRET-BACKED (see google_secret_manager_secret_version
    # above): socket DSN through the auth-proxy sidecar + the generated
    # password. It must not exist as a plain env var.
    # Bus transport: Google-managed Pub/Sub (the pubsub module provisions
    # every topic the app consumes plus the -worker-sub pull
    # subscriptions; NATS would need its own deployment this profile
    # does not include).
    MESSAGE_BUS_TRANSPORT = var.message_bus_transport
    GOOGLE_CLOUD_PROJECT  = var.project_id
    GOOGLE_CLOUD_REGION   = var.region
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

      env {
        name = "POSTGRES_DSN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.postgres_dsn.id
            version = "latest"
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
    # /cloudsql/<connection_name>; the application credentials flow as a
    # Secret Manager secret (POSTGRES_DSN), never a plain env var.
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

      env {
        name = "POSTGRES_DSN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.postgres_dsn.id
            version = "latest"
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

# ── Frontend (optional: provide var.frontend_image) ────────────────
# The Next.js standalone server needs no Cloud SQL proxy; it talks to
# the API over the project's public endpoint baked at BUILD time
# (NEXT_PUBLIC_API_URL in cloudbuild.frontend.yaml — runtime env cannot
# rewrite a browser bundle).
resource "google_cloud_run_v2_service" "frontend" {
  count = var.frontend_image != "" ? 1 : 0

  name     = "agentos-frontend"
  location = var.region
  project  = var.project_id
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = module.iam.api_sa_email
    containers {
      image = var.frontend_image
      env {
        name  = "PORT"
        value = "3000"
      }
      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }
    }
    scaling {
      min_instance_count = 0
      max_instance_count = var.api_max_instances
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "frontend_public" {
  count = (var.frontend_image != "" && var.api_public) ? 1 : 0

  name     = google_cloud_run_v2_service.frontend[0].name
  location = var.region
  project  = var.project_id
  role     = "roles/run.invoker"
  member   = "allUsers"
}
