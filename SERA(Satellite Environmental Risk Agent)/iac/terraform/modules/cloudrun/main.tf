# ── API service ───────────────────────────────────────────────────────────────

resource "google_cloud_run_v2_service" "api" {
  project  = var.project_id
  name     = var.service_name
  location = var.region

  template {
    service_account = var.api_sa_email

    containers {
      image = var.image

      env { name = "ENV";         value = var.env }
      env { name = "GCP_PROJECT"; value = var.project_id }
      env { name = "BQ_DATASET";  value = var.bq_dataset }
      env { name = "GCS_BUCKET";  value = var.gcs_bucket }

      env {
        name = "REDIS_URL"
        value_source {
          secret_key_ref { secret = var.redis_secret_name; version = "latest" }
        }
      }
      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref { secret = var.db_url_secret_name; version = "latest" }
        }
      }

      resources { limits = { cpu = "1"; memory = "512Mi" } }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance { instances = [var.cloud_sql_conn_name] }
    }

    scaling { min_instance_count = 0; max_instance_count = 5 }
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image]
  }
}

# ── Celery ingest worker ──────────────────────────────────────────────────────
# Runs with sa-sera-ingest-dev — EE, BQ, GCS, Cloud SQL, Secrets all via ADC.
# No personal credentials, no SA keys. min_instance_count=1 so queue drains promptly.

resource "google_cloud_run_v2_service" "ingest_worker" {
  project  = var.project_id
  name     = "${var.service_name}-ingest"
  location = var.region

  template {
    service_account = var.ingest_sa_email

    containers {
      image   = var.image
      command = ["celery"]
      args    = [
        "-A", "sera.tasks.celery_app",
        "worker",
        "-Q", "ingest",
        "--concurrency=2",
        "--hostname=ingest@%h",
        "--loglevel=info",
      ]

      env { name = "ENV";             value = var.env }
      env { name = "GCP_PROJECT";     value = var.project_id }
      env { name = "BQ_DATASET";      value = var.bq_dataset }
      env { name = "GCS_BUCKET";      value = var.gcs_bucket }
      env { name = "GEE_MAX_CONCURRENT"; value = "8" }

      env {
        name = "REDIS_URL"
        value_source {
          secret_key_ref { secret = var.redis_secret_name; version = "latest" }
        }
      }
      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref { secret = var.db_url_secret_name; version = "latest" }
        }
      }

      resources { limits = { cpu = "1"; memory = "1Gi" } }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance { instances = [var.cloud_sql_conn_name] }
    }

    scaling { min_instance_count = 1; max_instance_count = 2 }
  }

  lifecycle {
    ignore_changes = [template[0].containers[0].image]
  }
}

# ── IAM: public invoker for API only (not for worker) ────────────────────────
# The ingest worker has NO invoker binding — it is not reachable via HTTP.
# It pulls from the Redis queue. Cloud Run keeps it alive via min_instance_count=1.

resource "google_cloud_run_v2_service_iam_member" "api_public_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "service_url" {
  value = google_cloud_run_v2_service.api.uri
}

output "ingest_worker_name" {
  value = google_cloud_run_v2_service.ingest_worker.name
}

