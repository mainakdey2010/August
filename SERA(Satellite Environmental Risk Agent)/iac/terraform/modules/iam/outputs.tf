output "api_sa_email" {
  description = "Email of the API service account"
  value       = google_service_account.api.email
}

output "ingest_sa_email" {
  description = "Email of the Celery ingest worker service account"
  value       = google_service_account.ingest.email
}

output "agents_sa_email" {
  description = "Email of the ADK agents service account"
  value       = google_service_account.agents.email
}

