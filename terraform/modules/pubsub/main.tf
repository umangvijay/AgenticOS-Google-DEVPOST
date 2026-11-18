locals {
  topics = [
    "agentos-workflow-events",
    "agentos-scheduler-triggers",
    "agentos-mcp-validation",
    "agentos-recovery-events"
  ]
}

resource "google_pubsub_topic" "topics" {
  for_each = toset(local.topics)
  name     = each.key
  project  = var.project_id
}

data "google_project" "current" {
  project_id = var.project_id
}

resource "google_pubsub_topic" "dead_letters" {
  for_each = toset(local.topics)
  name     = "${each.key}-dead-letter"
  project  = var.project_id
}

resource "google_pubsub_subscription" "quarantine" {
  for_each                   = toset(local.topics)
  name                       = "${each.key}-quarantine"
  project                    = var.project_id
  topic                      = google_pubsub_topic.dead_letters[each.key].id
  message_retention_duration = "604800s"
  expiration_policy { ttl = "" }
}

resource "google_pubsub_topic_iam_member" "dead_letter_publisher" {
  for_each = toset(local.topics)
  project  = var.project_id
  topic    = google_pubsub_topic.dead_letters[each.key].name
  role     = "roles/pubsub.publisher"
  member   = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# Pull subscriptions consumed by backend.worker through Pub/Sub StreamingPull.
resource "google_pubsub_subscription" "worker_pull_subs" {
  for_each = toset(local.topics)
  name     = "${each.key}-worker-sub"
  project  = var.project_id
  topic    = google_pubsub_topic.topics[each.key].name

  ack_deadline_seconds = 600 # 10 minutes

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letters[each.key].id
    max_delivery_attempts = 5
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
}

resource "google_pubsub_subscription_iam_member" "dead_letter_forwarder" {
  for_each     = toset(local.topics)
  project      = var.project_id
  subscription = google_pubsub_subscription.worker_pull_subs[each.key].name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}
