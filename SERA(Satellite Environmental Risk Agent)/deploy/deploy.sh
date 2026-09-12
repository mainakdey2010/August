#!/usr/bin/env bash
# SERA — Cloud Run deployment script
# Run after setup.sh is complete and image is built.
# Usage: bash deploy/deploy.sh [dev|qa|prod]

set -euo pipefail

ENV="${1:-dev}"
PROJECT_ID="${GCP_PROJECT:-your-project-id}"
REGION="europe-west1"
IMAGE_TAG="${REGION}-docker.pkg.dev/${PROJECT_ID}/sera/sera:latest"
BQ_DATASET="sera_analytics_${ENV}"
GCS_BUCKET="sera-rasters-${ENV}"

# Fetch Redis URL from Secret Manager
REDIS_URL=$(gcloud secrets versions access latest --secret="sera-redis-url" --project="${PROJECT_ID}")

# Cloud SQL connection name
SQL_CONN="${PROJECT_ID}:${REGION}:sera-postgres"

echo "==> Deploying SERA env=${ENV} project=${PROJECT_ID}"

# ── API (Cloud Run service — scales to 0 when idle) ──────────────────────────
echo "==> Deploying API..."
gcloud run deploy "sera-api-${ENV}" \
  --image="${IMAGE_TAG}" \
  --platform=managed \
  --region="${REGION}" \
  --service-account="sa-sera-api-${ENV}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --add-cloudsql-instances="${SQL_CONN}" \
  --set-env-vars="ENV=${ENV},GCP_PROJECT=${PROJECT_ID},BQ_DATASET=${BQ_DATASET},GCS_BUCKET=${GCS_BUCKET}" \
  --set-secrets="REDIS_URL=sera-redis-url:latest,DATABASE_URL=sera-db-url:latest" \
  --min-instances=0 \
  --max-instances=5 \
  --memory=512Mi \
  --cpu=1 \
  --timeout=60s \
  --allow-unauthenticated \
  --quiet

API_URL=$(gcloud run services describe "sera-api-${ENV}" --region="${REGION}" --format="value(status.url)")
echo "  API URL: ${API_URL}"

# ── Celery ingest worker (Cloud Run service — always-on, min 1) ───────────────
# Note: Celery workers need to be always-on — they pull from Redis queue.
# min-instances=1 prevents cold start delay on task dispatch.
# Cost: ~1 vCPU × 24h = ~$0.50/day on free tier credits.
echo "==> Deploying ingest worker..."
gcloud run deploy "sera-worker-ingest-${ENV}" \
  --image="${IMAGE_TAG}" \
  --command="celery" \
  --args="-A,sera.tasks.celery_app,worker,-Q,ingest,--concurrency=4,--hostname=ingest@%h,--loglevel=info" \
  --platform=managed \
  --region="${REGION}" \
  --service-account="sa-sera-ingest-${ENV}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --add-cloudsql-instances="${SQL_CONN}" \
  --set-env-vars="ENV=${ENV},GCP_PROJECT=${PROJECT_ID},BQ_DATASET=${BQ_DATASET},GCS_BUCKET=${GCS_BUCKET},GEE_MAX_CONCURRENT=8" \
  --set-secrets="REDIS_URL=sera-redis-url:latest,DATABASE_URL=sera-db-url:latest" \
  --min-instances=1 \
  --max-instances=2 \
  --memory=1Gi \
  --cpu=2 \
  --timeout=3600s \
  --no-allow-unauthenticated \
  --quiet

# ── Celery agents worker ──────────────────────────────────────────────────────
echo "==> Deploying agents worker..."
gcloud run deploy "sera-worker-agents-${ENV}" \
  --image="${IMAGE_TAG}" \
  --command="celery" \
  --args="-A,sera.tasks.celery_app,worker,-Q,agents,--concurrency=2,--hostname=agents@%h,--loglevel=info" \
  --platform=managed \
  --region="${REGION}" \
  --service-account="sa-sera-agents-${ENV}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --add-cloudsql-instances="${SQL_CONN}" \
  --set-env-vars="ENV=${ENV},GCP_PROJECT=${PROJECT_ID},BQ_DATASET=${BQ_DATASET}" \
  --set-secrets="REDIS_URL=sera-redis-url:latest,DATABASE_URL=sera-db-url:latest" \
  --min-instances=1 \
  --max-instances=2 \
  --memory=1Gi \
  --cpu=1 \
  --timeout=600s \
  --no-allow-unauthenticated \
  --quiet

# ── Celery beat (Cloud Run Job — single instance, always-on) ─────────────────
# Beat scheduler must run as a single instance to avoid duplicate task firing.
# Use Cloud Run Jobs with --tasks=1 (not a service).
echo "==> Deploying Celery beat..."
gcloud run jobs create "sera-beat-${ENV}" \
  --image="${IMAGE_TAG}" \
  --command="celery" \
  --args="-A,sera.tasks.celery_app,beat,--loglevel=info" \
  --region="${REGION}" \
  --service-account="sa-sera-ingest-${ENV}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars="ENV=${ENV},GCP_PROJECT=${PROJECT_ID},BQ_DATASET=${BQ_DATASET}" \
  --set-secrets="REDIS_URL=sera-redis-url:latest" \
  --memory=256Mi \
  --cpu=1 \
  --task-timeout=86400s \
  --max-retries=3 \
  --quiet 2>/dev/null || \
gcloud run jobs update "sera-beat-${ENV}" \
  --image="${IMAGE_TAG}" \
  --region="${REGION}" \
  --quiet

# Execute the beat job (runs continuously until stopped)
gcloud run jobs execute "sera-beat-${ENV}" --region="${REGION}" --quiet

echo ""
echo "==> Deployment complete."
echo "  API: ${API_URL}"
echo "  Health: ${API_URL}/v1/health"
echo ""
echo "==> Estimated monthly cost (after credits exhausted):"
echo "  Cloud SQL db-f1-micro:    ~\$7/mo"
echo "  Cloud Run workers (2×):   ~\$20/mo (min-instances=1, 2vCPU, 1Gi)"
echo "  Cloud Run API:            ~\$1/mo  (scales to 0)"
echo "  Cloud Storage:            ~\$1/mo  (30-day lifecycle on rasters)"
echo "  BigQuery:                 Free tier (1TB/mo queries, 10GB storage)"
echo "  Upstash Redis:            Free tier"
echo "  GEE:                      Free (non-commercial research)"
echo "  Total estimate:           ~\$29/mo"
echo ""
echo "  \$300 credit = ~10 months runway"
