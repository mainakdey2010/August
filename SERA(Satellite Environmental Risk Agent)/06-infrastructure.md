# SERA — Infrastructure & Deployment
`v1.0 | 2026-09-07`

---

## Environment Matrix

| Env | GCP Project | Purpose | GEE Project | BQ Dataset |
|---|---|---|---|---|
| DV | `sera-dev` | Development & integration testing | `sera-gee-dev` | `sera_analytics_dev` |
| QA | `sera-qa` | QA & pre-prod validation | `sera-gee-qa` | `sera_analytics_qa` |
| NP | `sera-nonprod` | Staging / load testing | `sera-gee-nonprod` | `sera_analytics_np` |
| PD | `sera-prod` | Production | `sera-gee-prod` | `sera_analytics_pd` |

All four environments are structurally identical. Config differences (thresholds, region lists, alert targets) are managed via environment-specific YAML overlays loaded at deploy time.

---

## GCP Project Structure (per environment)

```
{env}-project/
├── Cloud Storage
│   └── sera-rasters-{env}/         # GeoTIFF exports per scan
│       └── {region_id}/{scan_id}/{index_id}/
│
├── BigQuery
│   └── sera_analytics_{env}/
│       ├── baseline_spectral_stats
│       ├── index_values
│       ├── scan_log
│       ├── risk_events
│       └── data_gap_log
│
├── Cloud Run / GKE
│   ├── sera-api                    # FastAPI gateway
│   ├── sera-celery-ingest          # Celery ingest worker pool
│   └── sera-celery-agents          # Celery agent worker pool (separate pool)
│
├── Cloud Memorystore               # Redis broker for Celery
├── Cloud SQL (PostgreSQL + PostGIS)# spatial queries, agent state
└── Secret Manager                  # webhook URLs, email lists, GEE SA keys
```

---

## Service Accounts & IAM

One service account per role per environment. No cross-environment SA sharing.

| SA Name | Roles | Purpose |
|---|---|---|
| `sa-sera-api-{env}` | `bigquery.dataViewer`, `cloudsql.client` | API reads only |
| `sa-sera-ingest-{env}` | `bigquery.dataEditor`, `storage.objectAdmin`, `earthengine.writer` | Celery ingest workers |
| `sa-sera-agents-{env}` | `bigquery.dataViewer`, `cloudsql.client` | ADK agent reads |
| `sa-sera-report-{env}` | `bigquery.dataEditor`, `cloudsql.client` | Reporting writes to risk_events |
| `sa-sera-gee-{env}` | GEE service account | GEE task submission and export |

GEE service accounts are registered in the Earth Engine console per project.
No individual user accounts are granted GEE access in production.

---

## Required Resource Labels

Every BigQuery job and GEE export must carry these labels. Missing labels = build fails in CI.

```yaml
# Mandatory on all BQ jobs and GEE export tasks
labels:
  env:           ${ENV}               # dev | qa | nonprod | prod
  workflow_name: ${WORKFLOW_NAME}     # sera-ingest | sera-baseline | sera-agents | sera-scan
  region_id:     ${REGION_ID}
  scan_id:       ${SCAN_ID}
  asset_tier:    ${ASSET_TIER}        # critical | high | medium | low | n/a
```

Labels are validated in the BQ job config helper:

```python
REQUIRED_LABELS = {"env", "workflow_name", "region_id", "scan_id", "asset_tier"}

def build_job_config(labels: dict) -> QueryJobConfig:
    missing = REQUIRED_LABELS - labels.keys()
    if missing:
        raise ValueError(f"Missing required BQ job labels: {missing}")
    return QueryJobConfig(labels=labels)
```

---

## Celery Worker Pools

Two separate pools to prevent agent workloads from starving ingestion tasks (and vice versa):

```python
# celery_config.py
task_routes = {
    "sera.tasks.ingest.*":  {"queue": "ingest"},
    "sera.tasks.gee.*":     {"queue": "ingest"},
    "sera.tasks.agents.*":  {"queue": "agents"},
    "sera.tasks.baseline.*":{"queue": "ingest"},
}

# Worker launch (separate processes/containers)
# celery -A sera worker -Q ingest  --concurrency=8
# celery -A sera worker -Q agents  --concurrency=4
```

GEE export tasks are I/O-bound (wait for GEE to complete server-side computation).
Agent tasks are CPU/LLM-bound (ADK calls to Claude API).
Separate pools prevent one from blocking the other.

---

## Deployment

```yaml
# Cloud Run service config (abbreviated)
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: sera-api-{env}
  labels:
    env: {env}
    component: api
spec:
  template:
    metadata:
      annotations:
        autoscaling.knative.dev/minScale: "1"
        autoscaling.knative.dev/maxScale: "10"
    spec:
      serviceAccountName: sa-sera-api-{env}@{project}.iam.gserviceaccount.com
      containers:
        - image: gcr.io/{project}/sera-api:{VERSION}
          env:
            - name: ENV
              value: {env}
            - name: BQ_DATASET
              value: sera_analytics_{env}
            - name: GCS_BUCKET
              value: sera-rasters-{env}
```

---

## Budget Alerts

Set per environment in GCP Billing:

| Env | Monthly budget | Alert at |
|---|---|---|
| DV | €500 | 80% |
| QA | €500 | 80% |
| NP | €1,000 | 70%, 90% |
| PD | €5,000 | 70%, 90%, 100% |

BQ slot reservations (FLEX or STANDARD) evaluated once DV baseline costs are known.
GEE compute is billed separately per project; track via GEE Usage Report.
