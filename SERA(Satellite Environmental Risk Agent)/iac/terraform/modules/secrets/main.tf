# Secret names managed here; values are NEVER managed by Terraform.
# Add a secret version manually:
#   printf "YOUR_VALUE" | gcloud secrets versions add <name> --data-file=-

resource "google_secret_manager_secret" "redis_url" {
  project   = var.project_id
  secret_id = var.redis_secret_name

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "db_url" {
  project   = var.project_id
  secret_id = var.db_url_secret_name

  replication {
    auto {}
  }
}

# Both the API SA (needs Redis for health check) and ingest SA (needs Redis broker)
# require read access to the Redis secret.
resource "google_secret_manager_secret_iam_member" "redis_api" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.redis_url.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.api_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "redis_ingest" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.redis_url.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.ingest_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "db_url_api" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.db_url.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.api_sa_email}"
}

resource "google_secret_manager_secret_iam_member" "db_url_ingest" {
  project   = var.project_id
  secret_id = google_secret_manager_secret.db_url.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${var.ingest_sa_email}"
}

