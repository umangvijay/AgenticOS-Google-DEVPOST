output "bucket_name" {
  value = google_storage_bucket.artifacts.name
}

output "bucket_url" {
  value = google_storage_bucket.artifacts.url
}
