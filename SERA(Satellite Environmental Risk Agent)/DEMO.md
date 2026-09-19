# SERA live demo: historical evidence → ADK/Gemini → map and analyst brief

This branch adds a supported, bounded demo workflow. It makes actual Earth Engine and Vertex AI calls when deployed; there is no synthetic-data switch or template fallback in the public demo API. **Live GCP acceptance has not run in the authoring environment, which has no ADC credentials. Do not describe this branch as a verified live deployment until `demo-verify.py` passes in the project and an analyst reviews its output.**

## Start from this one PR

```bash
git fetch origin
git checkout feat/sera-live-demo-adk-replays
cd 'SERA(Satellite Environmental Risk Agent)'
export GCP_PROJECT=august-505217
export REGION=asia-south1
export GOOGLE_CLOUD_LOCATION=global
# Set an explicit model ID currently available to this project's Vertex AI account.
export SERA_GEMINI_MODEL='<your-available-gemini-model-id>'
bash deploy/demo.sh
```

Run as a project administrator in Cloud Shell or a workstation with gcloud, bq and Python. Earth Engine must already be registered for this project. Existing prerequisites from the SERA environment: Artifact Registry repository `sera`, `sera-postgres`, `sera-rasters-dev`, and `sera_analytics_dev`. The script builds the checked-out commit, tests it, creates dedicated demo service accounts and SQL credentials, initializes the demo database, and deploys private `sera-demo` plus `sera-demo-scan`. It leaves the existing `sera` service and open PRs untouched. Credentials stay in Secret Manager; no service-account keys are needed.

The dedicated Cloud SQL user must have CREATEDB for first bootstrap (the built-in Cloud SQL user default). In a restricted instance an administrator must instead create database `sera_demo` owned by SQL user `sera_demo`. Bootstrap fails visibly if permissions are insufficient. Reruns reuse the DB secret. An interrupted first setup that created a SQL user but no secret must be repaired explicitly; it never silently rotates that user's password.

For browser access:

```bash
gcloud run services proxy sera-demo --project=august-505217 --region=asia-south1 --port=8080
```

Open `http://localhost:8080`. Select a scenario and **Run live replay**. This authenticates through the local gcloud proxy; the service is not made public. The caller needs Cloud Run Invoker on `sera-demo` (project owners already have broader access). Keep the proxy running in one terminal, then use a second terminal for acceptance:

```bash
python3 deploy/demo-verify.py --url http://localhost:8080 --output demo-validation
```

Acceptance requires all four scans to reach complete, actual Vertex model provenance and raw output, H3 evidence, asset-buffer evidence, persisted before/after PNGs, and a successful BigQuery load. The command saves JSON and images and exits nonzero on failure/timeout. Failure detail is in Cloud Run Job logs. Use **Retry this scan** after addressing a failure, then run verification again; it reuses the saved IDs. Continue fixes on this same branch and PR. No merge is needed to test it.

The model ID is intentionally required rather than hardcoding a model that may be unavailable or retired. IAM alone does not guarantee a particular model is enabled in the chosen location.

## Four retrospective scenarios

| Scenario | Monitoring point (illustrative) | Before window | After window | Demo question |
|---|---|---|---|---|
| Sindh floods, 2022 | Near Sehwan: 26.43 N, 67.85 E; 5 km half-width | June 1–30, 2022 | September 1–20, 2022 | Where did NDWI/MNDWI rise, and is the asset buffer sufficiently observed? |
| Lahaina fire, 2023 | Town monitoring point: 20.88 N, 156.675 W; 3 km half-width | July 15–August 8, 2023 | August 9–31, 2023 | Where did NBR/NDVI fall and dNBR rise after the August 8 fire? |
| Nepal–Gyirong disaster, August 26, 2026 | User-supplied port point: 28.279722 N, 85.377778 E; 3 km half-width | July 26–August 26, 2026 | August 27–September 19, 2026 | What optical change is visible near the port, and where do terrain/cloud gaps prevent interpretation? |
| Upper Assam floods, 2026 | Illustrative Sivasagar-area point: 26.98 N, 94.63 E; 5 km half-width | June 1–July 1, 2026 | July 22–August 15, 2026 | Where did water indices rise or vegetation decline, relative to prior monsoon seasons? |

All window ends are **exclusive**. The selected points are demo monitoring locations, not verified client assets or property boundaries. Event references establish historical context, not point-level impact. No fixed positive outcome is inserted. If cloud-free imagery or asset-buffer evidence is insufficient, the UI shows the gap and live acceptance fails rather than fabricating a detection.

Historical context: [ADB's Sindh reconstruction project](https://www.adb.org/projects/57323-001/main), [FEMA Hawaii disaster declaration](https://www.fema.gov/disaster/4724), and [AP's account of the August 8 Lahaina fire](https://apnews.com/article/4dfee66d3185b1a1f7ea5a946bce9eb2).

The Gyirong replay uses the supplied customs-point coordinates, whose facility boundary remains unverified. [Stimson’s August 2026 event account](https://www.stimson.org/2026/a-cascading-disaster-on-the-china-nepal-border-what-to-know-about-the-august-2026-rasuwa-flood/) provides context. The Assam replay uses a bounded sample inside the previously requested Upper Assam area; [regional reporting](https://www.theguardian.com/global-development/2026/aug/14/india-assam-climate-disaster-floods-brahmaputra-homeless-deaths) documents the July–August floods. Both use five prior seasonal windows. Neither covers an entire disaster footprint. Monsoon clouds, mountain shadows, transient flood peaks and seasonal paddy water can limit interpretation.

Suggested demo sequence: open a completed saved run → show before/after acquisition windows → choose NDWI or dNBR on the H3 map → inspect the asset-buffer row and seasonal chart → read Gemini's evidence-linked briefing → show source IDs, gaps and model provenance → download the evidence package. A live rerun can be shown separately; processing latency is measured, not promised.

## What the demo implements

- Region/asset-point configuration and a maximum 20 km-wide bounded optical region, up to 1,000 H3 cells and 120 scenes per window. Two active submissions at a time limit demo load.
- Real Sentinel-2 SR harmonized collection; common SCL masks for vegetation, bare soil and water, avoiding the QA60 gap in 2022–2024. NDVI, NDWI, MNDWI and NBR. dNBR is derived from jointly valid pre/post NBR pixels.
- The same 20 m reduction scale for current and historical measurements. Each prior-year matching seasonal composite contributes one temporal sample; no substitution of pixel variance for temporal variance.
- A minimum 50% jointly usable area before producing a scope/index summary. Seasonal z-scores require at least three usable prior years and a standard deviation >=0.02. Gaps remain explicit.
- H3 region summaries and overlap-weighted asset-buffer summaries. These approximate buffer measurements using cell means and are explicitly labelled as such.
- Durable PostgreSQL scan/config/checkpoint records. Every worker claims its scan atomically. Repeated submission reuses the saved scan; forced recompute creates a new ID. Crashed workers can be retried after the 40-minute lease expires; jobs time out after 30 minutes.
- API launches a Cloud Run Job using its own identity. No constantly running Celery worker or Redis scale-from-zero assumption is required by this demo.
- Real Google ADK `Agent`, `Runner`, in-memory per-call session and explicit Vertex AI Gemini client. Structured output, evidence-ID/identity validation, retained limitations and gaps, model/package metadata, input hash, raw response and token metadata. The quantitative layer is deterministic. Model failures fail the scan; there is no silent authored-text fallback.
- Private GCS previews and evidence JSON; BigQuery `demo_evidence` archive; browser map, before/after images, seasonal chart, polling, retry and export. Database checkpoint is the authoritative scan state.

The root UI is also mounted in the existing `sera.api.main` application, but `deploy/demo.sh` uses the focused `sera.demo.app` service so only the supported demo API appears in that service's OpenAPI. The legacy Celery ingestion, broad registry and older `/v1/scans` pipeline are retained for later convergence; this PR does not assert all legacy products are now validated. The earlier checkpoint/date fixes from PR #7 are included in the new branch; PRs #7/#8 are not merged or closed.

## API

- `GET /v1/demo/scenarios`
- `POST /v1/demo/scenarios/{id}/register`, or `POST /v1/demo/regions` with a scenario-shaped configuration
- `GET /v1/demo/regions`
- `POST /v1/demo/scans` with `{"region_id":"sindh-flood-2022"}`
- `GET /v1/demo/scans/{scan_id}`
- `POST /v1/demo/scans/{scan_id}/retry`
- `GET /v1/demo/scans/{scan_id}/previews/before` (or `after`)
- `GET /v1/demo/scans/{scan_id}/export`

`GET /v1/demo/health` verifies DB reachability and configuration presence only; it does not claim model execution. OpenAPI is at `/docs`.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r src/sera/requirements.txt pytest
PYTHONPATH=src python -m pytest tests -q
export PYTHONPATH=src
export DATABASE_URL=sqlite:///sera-demo.db
export SERA_DEMO_LOCAL_WORKER=true
# For actual scans, authenticate ADC and configure GCP_PROJECT, SERA_GEMINI_MODEL,
# GOOGLE_CLOUD_LOCATION, GCS_BUCKET, and BQ_DATASET as above.
uvicorn sera.demo.app:app --port 8080
```

SQLite is only for local development and rejected by the Cloud Run API. Cloud workers and API share PostgreSQL. No live Google credentials are required by unit/contract tests. Test adapters are synthetic and are not live scan evidence.

## Scientific and operational limits

These are **retrospective reconstructions**, using observations acquired before the cutoff. The original operational availability of reprocessed historical products is not established, so no warning lead time is claimed. A 0.15 directional index delta is an uncalibrated review rule, not an event probability or damage classifier. Seasonal composites can differ in clear-pixel support. The UI keeps those limitations attached to the report; semantic interpretation still needs human review.

No SAR flood classifier, LST, nine-index certification, 100 × 100 km throughput claim, automated geocoding, or automatic emergency action is included. The four real-event scenarios are executable definitions, not successful detection claims until their saved live outputs are reviewed. Existing GCS lifecycle rules may remove previews after 30 days; export the demo package if it must remain available longer. GCS/BQ operations and Vertex calls incur project usage; Cloud Run Jobs stop after each run, and the API has min instances zero.

References: [Sentinel-2 catalogue and QA60 gap](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR_HARMONIZED), [ADK LLM agents](https://adk.dev/agents/llm-agents/), [ADK Gemini configuration](https://adk.dev/agents/models/google-gemini/), [Cloud Run roles](https://docs.cloud.google.com/iam/docs/roles-permissions/run).
