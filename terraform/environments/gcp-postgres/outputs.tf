output "api_url" {
  value = google_cloud_run_v2_service.api.uri
}

output "cloud_sql_connection_name" {
  value = module.cloud_sql.connection_name
}

output "artifacts_bucket" {
  value = module.artifacts.bucket_name
}

output "api_sa_email" {
  value = module.iam.api_sa_email
}

output "worker_sa_email" {
  value = module.iam.worker_sa_email
}
