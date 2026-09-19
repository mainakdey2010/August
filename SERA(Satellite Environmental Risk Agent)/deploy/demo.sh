#!/usr/bin/env bash
# Build/deploy the CURRENT checked-out PR commit. Never pushes or merges develop.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${GCP_PROJECT:?Set GCP_PROJECT}"
: "${SERA_GEMINI_MODEL:?Set a Gemini model available in Vertex AI for your project}"
REGION="${REGION:-asia-south1}"
GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"
SQL_INSTANCE="${SQL_INSTANCE:-sera-postgres}"
GCS_BUCKET="${GCS_BUCKET:-sera-rasters-dev}"
BQ_DATASET="${BQ_DATASET:-sera_analytics_dev}"
SERVICE="sera-demo"
JOB="sera-demo-scan"
SECRET="sera-demo-database-url"
API_SA="sa-sera-demo-api@${GCP_PROJECT}.iam.gserviceaccount.com"
WORKER_SA="sa-sera-demo-worker@${GCP_PROJECT}.iam.gserviceaccount.com"
for binary in gcloud bq python3 git; do command -v "$binary" >/dev/null; done
# Prevent accidentally deploying develop or an uncommitted local variant.
[[ "$(git branch --show-current)" != "develop" ]] || { echo 'Check out the demo PR branch first.' >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo 'Commit local changes before deploying a reviewable image.' >&2; exit 1; }
COMMIT="$(git rev-parse HEAD)"
IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT}/sera/sera:demo-${COMMIT}"
gcloud services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  aiplatform.googleapis.com earthengine.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com bigquery.googleapis.com storage.googleapis.com --project="$GCP_PROJECT"
CONNECTION="$(gcloud sql instances describe "$SQL_INSTANCE" --project="$GCP_PROJECT" --format='value(connectionName)')"
for short in sa-sera-demo-api sa-sera-demo-worker; do
  if ! gcloud iam service-accounts describe "${short}@${GCP_PROJECT}.iam.gserviceaccount.com" --project="$GCP_PROJECT" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$short" --project="$GCP_PROJECT"
  fi
done
for account in "$API_SA" "$WORKER_SA"; do
  gcloud projects add-iam-policy-binding "$GCP_PROJECT" --member="serviceAccount:${account}" --role=roles/cloudsql.client --condition=None --quiet >/dev/null
done
for role in roles/aiplatform.user roles/earthengine.viewer roles/serviceusage.serviceUsageConsumer roles/bigquery.jobUser; do
  gcloud projects add-iam-policy-binding "$GCP_PROJECT" --member="serviceAccount:${WORKER_SA}" --role="$role" --condition=None --quiet >/dev/null
done
# Dataset-scoped write permission; no project-wide BigQuery data editor grant.
bq --project_id="$GCP_PROJECT" query --use_legacy_sql=false \
  "GRANT \`roles/bigquery.dataEditor\` ON SCHEMA \`${GCP_PROJECT}.${BQ_DATASET}\` TO 'serviceAccount:${WORKER_SA}'"
gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET}" --member="serviceAccount:${WORKER_SA}" --role=roles/storage.objectAdmin >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET}" --member="serviceAccount:${API_SA}" --role=roles/storage.objectViewer >/dev/null

# Dedicated DB/user/secret; an existing secret is reused, never silently rotated.
if ! gcloud secrets describe "$SECRET" --project="$GCP_PROJECT" >/dev/null 2>&1; then
  if gcloud sql users list --instance="$SQL_INSTANCE" --project="$GCP_PROJECT" --format='value(name)' | python3 -c "import sys; sys.exit(0 if 'sera_demo' in sys.stdin.read().splitlines() else 1)"; then
    echo 'sera_demo SQL user exists but its secret is missing. Restore sera-demo-database-url before rerunning; no password was changed.' >&2
    exit 1
  fi
  umask 077
  credential_file="$(mktemp)"
  trap 'rm -f "$credential_file"' EXIT
  password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(40))')"
  gcloud sql users create sera_demo --instance="$SQL_INSTANCE" --project="$GCP_PROJECT" --password="$password" --quiet
  SERA_SETUP_PASSWORD="$password" SERA_SETUP_CONNECTION="$CONNECTION" python3 - <<'PY' > "$credential_file"
import os, urllib.parse
password=urllib.parse.quote(os.environ['SERA_SETUP_PASSWORD'],safe='')
connection=urllib.parse.quote('/cloudsql/'+os.environ['SERA_SETUP_CONNECTION'],safe='')
print(f'postgresql://sera_demo:{password}@/sera_demo?host={connection}',end='')
PY
  unset password
  gcloud secrets create "$SECRET" --project="$GCP_PROJECT" --replication-policy=automatic --data-file="$credential_file"
  rm -f "$credential_file"
  trap - EXIT
fi
for account in "$API_SA" "$WORKER_SA"; do
  gcloud secrets add-iam-policy-binding "$SECRET" --project="$GCP_PROJECT" --member="serviceAccount:${account}" --role=roles/secretmanager.secretAccessor >/dev/null
done

# Build tests run inside Cloud Build before the container image is published.
gcloud builds submit . --project="$GCP_PROJECT" --region="$REGION" --config=deploy/cloudbuild-demo.yaml --substitutions="_IMAGE=${IMAGE}"
COMMON_ENV="GCP_PROJECT=${GCP_PROJECT},GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION},SERA_GEMINI_MODEL=${SERA_GEMINI_MODEL},GCS_BUCKET=${GCS_BUCKET},BQ_DATASET=${BQ_DATASET}"
gcloud run jobs deploy "$JOB" --project="$GCP_PROJECT" --region="$REGION" --image="$IMAGE" \
  --service-account="$WORKER_SA" --command=python --args=-m,sera.demo.worker,not-dispatched \
  --tasks=1 --parallelism=1 --max-retries=0 --task-timeout=1800s --cpu=1 --memory=1Gi \
  --set-cloudsql-instances="$CONNECTION" --set-secrets="DATABASE_URL=${SECRET}:latest" --set-env-vars="$COMMON_ENV"
gcloud run jobs execute "$JOB" --project="$GCP_PROJECT" --region="$REGION" --args=-m,sera.demo.bootstrap --wait
gcloud run jobs add-iam-policy-binding "$JOB" --project="$GCP_PROJECT" --region="$REGION" \
  --member="serviceAccount:${API_SA}" --role=roles/run.jobsExecutorWithOverrides >/dev/null
gcloud run deploy "$SERVICE" --project="$GCP_PROJECT" --region="$REGION" --image="$IMAGE" \
  --service-account="$API_SA" --command=uvicorn --args=sera.demo.app:app,--host,0.0.0.0,--port,8080 \
  --min-instances=0 --max-instances=2 --cpu=1 --memory=512Mi --timeout=120 --no-allow-unauthenticated \
  --add-cloudsql-instances="$CONNECTION" --set-secrets="DATABASE_URL=${SECRET}:latest" \
  --set-env-vars="${COMMON_ENV},SERA_DEMO_JOB=projects/${GCP_PROJECT}/locations/${REGION}/jobs/${JOB}"
SERVICE_URL="$(gcloud run services describe "$SERVICE" --project="$GCP_PROJECT" --region="$REGION" --format='value(status.url)')"
printf 'Deployed %s from commit %s\n' "$SERVICE_URL" "$COMMIT"
printf 'Open UI: gcloud run services proxy %s --project=%s --region=%s --port=8080\n' "$SERVICE" "$GCP_PROJECT" "$REGION"
printf 'Then visit http://localhost:8080 and run both scenarios. No scan is claimed successful by deployment alone.\n'
