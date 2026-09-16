# ── Service accounts ──────────────────────────────────────────────────────────

resource "google_service_account" "api" {
  project      = var.project_id
  account_id   = "sa-sera-api-${var.env}"
  display_name = "SERA API service account (${var.env})"
}

resource "google_service_account" "ingest" {
  project      = var.project_id
  account_id   = "sa-sera-ingest-${var.env}"
  display_name = "SERA Celery ingest worker (${var.env})"
}

resource "google_service_account" "agents" {
  project      = var.project_id
  account_id   = "sa-sera-agents-${var.env}"
  display_name = "SERA ADK agent worker (${var.env})"
}

# ── API SA roles (read-only for BQ; Cloud SQL client; Secret access) ─────────

locals {
  api_member    = "serviceAccount:${google_service_account.api.email}"
  ingest_member = "serviceAccount:${google_service_account.ingest.email}"
  agents_member = "serviceAccount:${google_service_account.agents.email}"
}

resource "google_project_iam_member" "api_bq_viewer" {
  project = var.project_id
  role    = "roles/bigquery.dataViewer"
  member  = local.api_member
}

resource "google_project_iam_member" "api_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = local.api_member
}

resource "google_project_iam_member" "api_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = local.api_member
}

resource "google_project_iam_member" "api_service_usage" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = local.api_member
}

resource "google_project_iam_member" "api_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = local.api_member
}

# Needed for GEE initialization check in /v1/health
resource "google_project_iam_member" "api_earthengine_viewer" {
  project = var.project_id
  role    = "roles/earthengine.viewer"
  member  = local.api_member
}

# ── Ingest SA roles (GEE write, BQ editor, GCS, Cloud SQL, secrets) ──────────

resource "google_project_iam_member" "ingest_earthengine_writer" {
  project = var.project_id
  role    = "roles/earthengine.writer"
  member  = local.ingest_member
}

resource "google_project_iam_member" "ingest_bq_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = local.ingest_member
}

resource "google_project_iam_member" "ingest_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = local.ingest_member
}

resource "google_project_iam_member" "ingest_storage_admin" {
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = local.ingest_member
}

resource "google_project_iam_member" "ingest_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = local.ingest_member
}

resource "google_project_iam_member" "ingest_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = local.ingest_member
}

resource "google_project_iam_member" "ingest_service_usage" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = local.ingest_member
}

# ── Agents SA roles (read-only — ADK sessions read BQ + PostGIS) ─────────────

resource "google_project_iam_member" "agents_bq_viewer" {
  project = var.project_id
  role    = "roles/bigquery.dataViewer"
  member  = local.agents_member
}

resource "google_project_iam_member" "agents_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = local.agents_member
}

resource "google_project_iam_member" "agents_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = local.agents_member
}

resource "google_project_iam_member" "agents_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = local.agents_member
}

