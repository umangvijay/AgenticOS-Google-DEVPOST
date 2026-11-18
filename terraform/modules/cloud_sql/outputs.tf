output "connection_name" {
  description = "Cloud SQL connection name for the auth proxy sidecar"
  value       = google_sql_database_instance.postgres.connection_name
}

output "instance_name" {
  value = google_sql_database_instance.postgres.name
}

output "database_name" {
  value = google_sql_database.database.name
}
