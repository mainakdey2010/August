output "cloud_run_url" {
  description = "Cloud Run API service URL"
  value       = module.cloudrun.service_url
}

output "cloud_sql_connection_name" {
  description = "Cloud SQL connection name for use in Cloud Run --add-cloudsql-instances"
  value       = "${var.project_id}:${var.region}:${var.cloud_sql_instance_name}"
}

output "artifact_registry_repo" {
  description = "Artifact Registry repository path"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repo}"
}

