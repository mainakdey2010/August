variable "project_id" {
  description = "GCP project ID. Set via TF_VAR_project_id env var — never hardcode."
  type        = string
  # No default — must be supplied at runtime:
  #   export TF_VAR_project_id=$(gcloud config get-value project)
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "asia-south1"
}

variable "env" {
  description = "Environment label: dev | qa | prod"
  type        = string
}

variable "cloud_run_service_name" {
  description = "Name of the Cloud Run API service"
  type        = string
}

variable "cloud_sql_instance_name" {
  description = "Cloud SQL instance name"
  type        = string
}

variable "cloud_sql_db_name" {
  description = "PostgreSQL database name"
  type        = string
  default     = "sera"
}

variable "cloud_sql_user" {
  description = "PostgreSQL username"
  type        = string
  default     = "sera"
}

variable "bq_dataset" {
  description = "BigQuery dataset ID"
  type        = string
}

variable "gcs_bucket" {
  description = "GCS bucket for GEE raster exports"
  type        = string
}

variable "artifact_registry_repo" {
  description = "Artifact Registry Docker repo name"
  type        = string
  default     = "sera"
}

variable "git_branch" {
  description = "Git branch Cloud Build triggers watch (e.g. develop for dev, main for prod)"
  type        = string
  default     = "develop"
}

variable "redis_secret_name" {
  description = "Secret Manager secret that holds the Redis connection string"
  type        = string
}

variable "db_url_secret_name" {
  description = "Secret Manager secret that holds DATABASE_URL"
  type        = string
  default     = "sera-db-url"
}


