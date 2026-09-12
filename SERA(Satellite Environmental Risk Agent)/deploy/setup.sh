#!/usr/bin/env bash
# SERA — GCP project bootstrap script
# Designed for a free-tier GCP account with ~$300 credit.
# Estimated monthly cost after credits: ~$15-25/mo
#
# Run once: bash deploy/setup.sh
# Prerequisites:
#   gcloud CLI installed and authenticated
#   PROJECT_ID set below

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
PROJECT_ID="${GCP_PROJECT:-your-project-id}"
REGION="europe-west1"       # pick closest to your data
ENV="dev"
BQ_DATASET="sera_analytics_dev"
GCS_BUCKET="sera-rasters-${ENV}"
IMAGE_TAG="gcr.io/${PROJECT_ID}/sera:latest"

echo "==> Setting up SERA on project: ${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}"

# ── Enable APIs ───────────────────────────────────────────────────────────────
echo "==> Enabling GCP APIs..."
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  storage.googleapis.com \
  bigquery.googleapis.com \
  secretmanager.googleapis.com \
  earthengine.googleapis.com \
  cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com \
  --quiet

# ── Artifact Registry ─────────────────────────────────────────────────────────
echo "==> Creating Artifact Registry..."
gcloud artifacts repositories create sera \
  --repository-format=docker \
  --location="${REGION}" \
  --quiet 2>/dev/null || echo "  (already exists)"

IMAGE_TAG="${REGION}-docker.pkg.dev/${PROJECT_ID}/sera/sera:latest"

# ── Cloud Storage ─────────────────────────────────────────────────────────────
echo "==> Creating GCS bucket: ${GCS_BUCKET}"
gcloud storage buckets create "gs://${GCS_BUCKET}" \
  --location="${REGION}" \
  --uniform-bucket-level-access 2>/dev/null || echo "  (already exists)"

# Lifecycle: delete objects older than 30 days (CSV exports are transient)
gcloud storage buckets update "gs://${GCS_BUCKET}" \
  --lifecycle-file=- <<'EOF'
{
  "rule": [{
    "action": {"type": "Delete"},
    "condition": {"age": 30}
  }]
}
EOF

# ── BigQuery ──────────────────────────────────────────────────────────────────
echo "==> Creating BigQuery dataset: ${BQ_DATASET}"
bq mk --dataset \
  --location=EU \
  --description="SERA analytics dev" \
  "${PROJECT_ID}:${BQ_DATASET}" 2>/dev/null || echo "  (already exists)"

echo "==> Creating BigQuery tables..."
# Replace placeholders and run
sed "s/{project}/${PROJECT_ID}/g; s/{dataset}/${BQ_DATASET}/g" \
  sql/bq/001_tables.sql | bq query --use_legacy_sql=false --project_id="${PROJECT_ID}"

# ── Cloud SQL (PostgreSQL + PostGIS) ─────────────────────────────────────────
# db-f1-micro: ~$7/month — cheapest tier that supports PostGIS
echo "==> Creating Cloud SQL instance (db-f1-micro)..."
gcloud sql instances create sera-postgres \
  --database-version=POSTGRES_15 \
  --tier=db-f1-micro \
  --region="${REGION}" \
  --storage-size=10GB \
  --storage-auto-increase \
  --backup-start-time=02:00 \
  --quiet 2>/dev/null || echo "  (already exists)"

gcloud sql databases create sera --instance=sera-postgres --quiet 2>/dev/null || true
gcloud sql users create sera --instance=sera-postgres --password=sera --quiet 2>/dev/null || true

echo "==> Applying PostGIS migrations..."
# Connect via Cloud SQL Auth Proxy (install: https://cloud.google.com/sql/docs/postgres/sql-proxy)
# cloud-sql-proxy "${PROJECT_ID}:${REGION}:sera-postgres" &
# PROXY_PID=$!
# sleep 3
# PGPASSWORD=sera psql -h 127.0.0.1 -U sera -d sera -f sql/postgis/001_initial.sql
# PGPASSWORD=sera psql -h 127.0.0.1 -U sera -d sera -f sql/postgis/002_webhooks.sql
# kill $PROXY_PID
echo "  Manual step: run PostGIS migrations via Cloud SQL Auth Proxy (see comment above)"

# ── Service Accounts ──────────────────────────────────────────────────────────
echo "==> Creating service accounts..."
for ROLE in api ingest agents; do
  SA="sa-sera-${ROLE}-${ENV}"
  gcloud iam service-accounts create "${SA}" \
    --display-name="SERA ${ROLE} (${ENV})" \
    --quiet 2>/dev/null || echo "  SA ${SA} already exists"
done

# Bind roles
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:sa-sera-ingest-${ENV}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor" --quiet
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:sa-sera-ingest-${ENV}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin" --quiet
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:sa-sera-api-${ENV}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataViewer" --quiet
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:sa-sera-api-${ENV}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" --quiet

# ── GEE Service Account ───────────────────────────────────────────────────────
echo ""
echo "==> GEE Service Account setup (manual):"
echo "  1. Go to: https://code.earthengine.google.com/"
echo "  2. Sign up / sign in with your Google account"
echo "  3. Go to: https://console.cloud.google.com/apis/credentials"
echo "  4. Create a Service Account key for sa-sera-ingest-${ENV}"
echo "  5. Register it in GEE: https://signup.earthengine.google.com/#!/service_accounts"
echo "  6. Store the key in Secret Manager:"
echo "     gcloud secrets create sera-gee-sa-key --data-file=sa-key.json"

# ── Build & Push Image ────────────────────────────────────────────────────────
echo "==> Building Docker image..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker build -t "${IMAGE_TAG}" .
docker push "${IMAGE_TAG}"
echo "  Image: ${IMAGE_TAG}"

# ── Upstash Redis (free tier — no Memorystore) ────────────────────────────────
echo ""
echo "==> Redis setup (Upstash — free tier, no Memorystore cost):"
echo "  1. Sign up at https://upstash.com"
echo "  2. Create a Redis database (free tier: 10k commands/day, 256MB)"
echo "  3. Copy the Redis URL (format: rediss://user:pass@host:port)"
echo "  4. Store it: gcloud secrets create sera-redis-url --data-file=- <<< 'rediss://...'"

echo ""
echo "==> Setup complete. Next: bash deploy/deploy.sh"
