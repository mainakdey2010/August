resource "google_storage_bucket" "rasters" {
  project       = var.project_id
  name          = var.gcs_bucket
  location      = var.region
  force_destroy = false

  uniform_bucket_level_access = true

  lifecycle_rule {
    condition { age = 90 }
    action    { type = "Delete" }
  }
}

resource "google_storage_bucket_iam_member" "rasters_ingest_writer" {
  bucket = google_storage_bucket.rasters.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${var.ingest_sa_email}"
}

resource "google_bigquery_dataset" "sera" {
  project    = var.project_id
  dataset_id = var.bq_dataset
  location   = var.region

  labels = {
    env = var.env
  }

  delete_contents_on_destroy = false
}

resource "google_bigquery_dataset_iam_member" "ingest_editor" {
  project    = var.project_id
  dataset_id = google_bigquery_dataset.sera.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${var.ingest_sa_email}"
}

