# Cloud SQL PostgreSQL — the managed database for the GCP/PostgreSQL
# target (§32.20 target-matrix gap: the declared topology needs a
# managed PostgreSQL root; the Firestore dev stack cannot establish it).
#
# Connection contract: the backend takes a single POSTGRES_DSN consumed
# by asyncpg. Cloud Run connects via the Cloud SQL Auth Proxy sidecar
# exposing a unix socket at /cloudsql, so the DSN uses the
# host=/cloudsql/<CONNECTION_NAME> form. IAM database authentication is
# used for the application user; no application password is stored in
# Secret Manager (the connector identity IS the credential).

resource "google_sql_database_instance" "postgres" {
  name             = var.instance_name
  project          = var.project_id
  region           = var.region
  database_version = "POSTGRES_16"

  settings {
    tier              = var.tier
    availability_type = var.availability_type
    disk_size         = var.disk_size_gb
    disk_type         = "PD_SSD"

    # IAM database authentication must be explicitly ON for the
    # CLOUD_IAM_SERVICE_ACCOUNT users below (not guaranteed-by-default
    # on every instance creation path).
    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    ip_configuration {
      ipv4_enabled    = false
      private_network = var.network_id
      # Cloud Run reaches the instance through the Cloud SQL Auth Proxy
      # over Private Services Access; no public IP is exposed.
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "03:00"
    }

    maintenance_window {
      day  = 7
      hour = 4
    }
  }

  deletion_protection = var.deletion_protection

  lifecycle {
    prevent_destroy = true
  }
}

# IAM database authentication: the API and worker service accounts are
# the database principals. asyncpg connects over the proxy socket with
# the SA email as the username and an IAM token as the password
# (token injection is done by the application's IAM auth path).
resource "google_sql_user" "iam_users" {
  for_each = toset(var.iam_user_emails)

  name     = each.value
  instance = google_sql_database_instance.postgres.name
  project  = var.project_id
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

# The APPLICATION connects with a password user through the Cloud SQL
# Auth Proxy sidecar (the app reads POSTGRES_DSN; it does not fetch IAM
# tokens as database passwords). The password is generated here, stored
# ONLY in Terraform state + Secret Manager, and assembled into the
# socket DSN the overlay wires as a secret-backed env var.
resource "random_password" "app_user" {
  length  = 32
  special = false
}

resource "google_sql_user" "app_user" {
  name     = var.app_user_name
  instance = google_sql_database_instance.postgres.name
  project  = var.project_id
  password = random_password.app_user.result
}

resource "google_sql_database" "database" {
  name     = var.database_name
  instance = google_sql_database_instance.postgres.name
  project  = var.project_id
}

# The application schema runs from the image's migration runner at
# startup (backend/repositories/postgres/database.py::apply_migrations).
# Terraform owns infrastructure, not schema — the boot job applies the
# ordered SQL migrations exactly like the local compose profile.
