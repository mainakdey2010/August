#!/usr/bin/env bash
# Configure the GCS backend for Terraform state.
# SERA reuses the Cloud Build bucket (already exists, no new bucket needed).
# State is stored at: gs://{project}_cloudbuild/terraform-state/sera/{env}
#
# Run from iac/terraform/ after installing Terraform.

set -euo pipefail

PROJECT=$(gcloud config get-value project)
BUCKET="${PROJECT}_cloudbuild"   # already exists — created by Cloud Build
ENV="${1:-dev}"                  # pass "prod" as arg for prod workspace

echo "==> Enabling versioning on gs://${BUCKET} (safe to run repeatedly)"
gcloud storage buckets update "gs://${BUCKET}" \
  --versioning \
  --project="${PROJECT}"

echo ""
echo "==> Now run:"
echo "    export TF_VAR_project_id=\$(gcloud config get-value project)"
echo "    terraform init \\"
echo "      -backend-config=\"bucket=${BUCKET}\" \\"
echo "      -backend-config=\"prefix=terraform-state/sera/${ENV}\""

