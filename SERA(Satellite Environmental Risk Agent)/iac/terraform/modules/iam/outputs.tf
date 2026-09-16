output "api_sa_email" {
  value = google_service_account.sera["api"].email
}

output "ingest_sa_email" {
  value = google_service_account.sera["ingest"].email
}

output "agents_sa_email" {
  value = google_service_account.sera["agents"].email
}

