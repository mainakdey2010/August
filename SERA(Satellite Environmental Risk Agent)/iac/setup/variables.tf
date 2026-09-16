variable "project_id" {
  description = "GCP project ID. Set via TF_VAR_project_id env var."
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "asia-south1"
}

