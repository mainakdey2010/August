locals {
  # Artifact Registry image base path — used by Cloud Run services
  image_base = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repo}/sera"

  # Cloud SQL connection name — used by Cloud Run services and modules
  sql_conn = "${var.project_id}:${var.region}:${var.cloud_sql_instance_name}"
}

