# GCS artifact storage — the declared GCP/PostgreSQL topology stores
# published deliverables (PDFs, OBJ models, generated tool bundles) in
# object storage instead of the compose shared volume.
#
# NOTE (honest scope): backend/services/artifact_store.py currently
# implements the local POSIX backend; the store CONTRACT is
# backend-neutral but a GCS backend is NOT yet implemented in the
# application. This module provisions the bucket, versioning, retention
# and IAM so the target exists; wiring the GCS ArtifactStore backend is
# the follow-up application change tracked in the target matrix.

resource "google_storage_bucket" "artifacts" {
  name                        = var.bucket_name
  project                     = var.project_id
  location                    = var.location
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  # Immutable revision identities (§20.6): artifacts are never mutated;
  # lifecycle rules only age out NONCURRENT revisions after the declared
  # retention window, and no deletion rule touches live objects.
  lifecycle_rule {
    condition {
      days_since_noncurrent_time = var.noncurrent_retention_days
    }
    action {
      type = "Delete"
    }
  }

  lifecycle_rule {
    condition {
      age = var.abandoned_prefix_age_days
    }
    action {
      type          = "AbortIncompleteMultipartUpload"
    }
  }

  public_access_prevention = "enforced"
}

resource "google_storage_bucket_iam_member" "api_artifacts" {
  for_each = toset(var.accessor_sa_emails)

  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${each.value}"
}

# Backup verification identity (§32.21 key/artifact custody): the
# restore drill must read historical object versions with an identity
# SEPARATE from the running application.
resource "google_storage_bucket_iam_member" "restore_verifier" {
  count = var.restore_verifier_sa_email != "" ? 1 : 0

  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${var.restore_verifier_sa_email}"
}
