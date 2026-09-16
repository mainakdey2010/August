resource "google_sql_database_instance" "sera" {
  project          = var.project_id
  name             = var.instance_name
  region           = var.region
  database_version = "POSTGRES_15"

  settings {
    tier              = "db-f1-micro"
    availability_type = "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = 10

    backup_configuration {
      enabled = true
    }

    ip_configuration {
      ipv4_enabled = false  # private IP only
    }
  }

  deletion_protection = true
}

resource "google_sql_database" "sera" {
  project  = var.project_id
  instance = google_sql_database_instance.sera.name
  name     = var.db_name
  charset  = "UTF8"
}

resource "google_sql_user" "sera" {
  project  = var.project_id
  instance = google_sql_database_instance.sera.name
  name     = var.db_user
  # Password is NOT managed here — set via:
  #   gcloud sql users set-password sera --instance=sera-postgres --password=<pw>
  lifecycle {
    ignore_changes = [password]
  }
}

