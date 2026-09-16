# All SA definitions and their roles live here.
# To add a role: append one string to the relevant list.
# To add a new SA: add one block to the map.

locals {
  service_accounts = {
    api = {
      account_id   = "sa-sera-api-${var.env}"
      display_name = "SERA API (${var.env})"
      roles = [
        "roles/bigquery.dataViewer",
        "roles/bigquery.jobUser",
        "roles/cloudsql.client",
        "roles/earthengine.viewer",
        "roles/secretmanager.secretAccessor",
        "roles/serviceusage.serviceUsageConsumer",
      ]
    }
    ingest = {
      account_id   = "sa-sera-ingest-${var.env}"
      display_name = "SERA Celery ingest worker (${var.env})"
      roles = [
        "roles/bigquery.dataEditor",
        "roles/bigquery.jobUser",
        "roles/cloudsql.client",
        "roles/earthengine.writer",
        "roles/secretmanager.secretAccessor",
        "roles/serviceusage.serviceUsageConsumer",
        "roles/storage.objectAdmin",
      ]
    }
    agents = {
      account_id   = "sa-sera-agents-${var.env}"
      display_name = "SERA ADK agents (${var.env})"
      roles = [
        "roles/bigquery.dataViewer",
        "roles/bigquery.jobUser",
        "roles/cloudsql.client",
        "roles/secretmanager.secretAccessor",
      ]
    }
  }

  # Flatten to (sa_key, role) pairs — one entry per IAM binding needed
  role_bindings = {
    for pair in flatten([
      for sa_key, sa_cfg in local.service_accounts : [
        for role in sa_cfg.roles : {
          key    = "${sa_key}:${role}"
          sa_key = sa_key
          role   = role
        }
      ]
    ]) : pair.key => pair
  }
}

