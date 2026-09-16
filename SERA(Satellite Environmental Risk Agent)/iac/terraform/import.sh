#!/usr/bin/env bash
# Import existing GCP resources into Terraform state.
# Run ONCE after `terraform init` to adopt resources created before Terraform.
#
# Usage:
#   export TF_VAR_project_id=$(gcloud config get-value project)
#   cd iac/terraform
#   bash backend-setup.sh dev    # enable versioning on Cloud Build bucket (once)
#   terraform init \
#     -backend-config="bucket=$(gcloud config get-value project)_cloudbuild" \
#     -backend-config="prefix=terraform-state/sera/dev"
#   bash import.sh

set -euo pipefail

PROJECT=$(gcloud config get-value project)
REGION="asia-south1"

echo "==> Importing resources for project=${PROJECT} region=${REGION}"

# ── Service accounts — for_each keys are "api", "ingest", "agents" ─────────────
terraform import \
  'module.iam.google_service_account.sera["api"]' \
  "projects/${PROJECT}/serviceAccounts/sa-sera-api-dev@${PROJECT}.iam.gserviceaccount.com"

terraform import \
  'module.iam.google_service_account.sera["ingest"]' \
  "projects/${PROJECT}/serviceAccounts/sa-sera-ingest-dev@${PROJECT}.iam.gserviceaccount.com"

terraform import \
  'module.iam.google_service_account.sera["agents"]' \
  "projects/${PROJECT}/serviceAccounts/sa-sera-agents-dev@${PROJECT}.iam.gserviceaccount.com"

# Note: google_project_iam_member (google_project_iam_member.sera[*]) are additive.
# Applying them adds missing bindings without removing existing ones. No import needed.

# ── Artifact Registry ─────────────────────────────────────────────────────────
terraform import \
  "google_artifact_registry_repository.sera" \
  "projects/${PROJECT}/locations/${REGION}/repositories/sera"

# ── Cloud SQL ─────────────────────────────────────────────────────────────────
terraform import \
  "module.cloudsql.google_sql_database_instance.sera" \
  "${PROJECT}/sera-postgres"

terraform import \
  "module.cloudsql.google_sql_database.sera" \
  "projects/${PROJECT}/instances/sera-postgres/databases/sera"

terraform import \
  "module.cloudsql.google_sql_user.sera" \
  "projects/${PROJECT}/instances/sera-postgres/users/sera"

# ── Secrets ───────────────────────────────────────────────────────────────────
terraform import \
  "module.secrets.google_secret_manager_secret.redis_url" \
  "projects/${PROJECT}/secrets/sera-redis-srt"

terraform import \
  "module.secrets.google_secret_manager_secret.db_url" \
  "projects/${PROJECT}/secrets/sera-db-url"

# Note: sera-github-github-oauthtoken-518350 is managed by Cloud Build — leave unmanaged.

# ── Cloud Run API service ─────────────────────────────────────────────────────
terraform import \
  "module.cloudrun.google_cloud_run_v2_service.api" \
  "projects/${PROJECT}/locations/${REGION}/services/sera"

# Note: sera-ingest worker service does not exist yet — terraform apply will create it.

# ── BigQuery dataset ──────────────────────────────────────────────────────────
terraform import \
  "module.storage.google_bigquery_dataset.sera" \
  "${PROJECT}/sera_analytics_dev"

# ── GCS bucket ────────────────────────────────────────────────────────────────
terraform import \
  "module.storage.google_storage_bucket.rasters" \
  "sera-rasters-dev"

echo ""
echo "==> Import complete. Now review the plan:"
echo "    terraform plan -var-file=environments/dev.tfvars"
echo ""
echo "    Expected plan:"
echo "    - 0 resources to destroy"
echo "    - SAs: no-op (already exist)"
echo "    - IAM bindings: add missing roles for ingest + all roles for agents"
echo "    - sera-ingest Cloud Run service: CREATE (new)"
echo "    - Secret accessor bindings: add for ingest SA"

