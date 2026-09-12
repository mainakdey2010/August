# SERA — Satellite Environmental Risk Agent
## Architecture & Design Overview
`v1.0 | 2026-09-07`

---

## 1. What SERA Is

An automated geospatial intelligence system that monitors environmental risk across critical infrastructure assets by combining petabyte-scale satellite imagery (Google Earth Engine) with asset registry data (BigQuery GIS / PostGIS), running multi-agent AI reasoning to produce actionable, auditable risk assessments.

---

## 2. Design Principles

| Principle | Implication |
|---|---|
| Config-driven, not code-driven | Monitoring regions, index weights, thresholds live in YAML — not hardcoded in agents or workers |
| Incremental by default | Every ingestion step checkpoints; re-runs never double-count or re-scan already-processed data |
| Data gaps are first-class outputs | Missing index data (cloud cover, sensor outage, GEE quota) is logged and factored into risk scoring — never silently skipped |
| One async runtime | Celery owns task execution. Agents own reasoning. These do not overlap. |
| Environment parity | DV → QA → NP → PD with separate GCP projects, service accounts, and BQ datasets |
| Cost attribution from day one | Every BigQuery job and GEE export carries required labels before any workload hits production |

---

## 3. Corrected Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Consumer Layer                                │
│          Ops Dashboard · Executive Reports · Regulatory API         │
│                  (React + Mapbox GL · REST / WebSocket)             │
└────────────────────────────┬─────────────────────────────────────────┘
                             │ REST / WebSocket
┌────────────────────────────▼─────────────────────────────────────────┐
│                       API Gateway (FastAPI)                          │
│   Auth (OAuth2/API key) · Rate limiting · REST routes               │
│   Webhook registration & delivery · Scan lifecycle management       │
└────────┬──────────────────────────────────────┬──────────────────────┘
         │ dispatch                              │ trigger (post-ingest)
┌────────▼────────────────┐          ┌───────────▼──────────────────────┐
│    Celery / Redis       │          │       Agent Subsystem (ADK)      │
│                         │          │                                  │
│  Ingest schedule        │          │  ┌── Anomaly Detection ───────┐  │
│  GEE export tasks       │  status  │  │  per-index, per-h3-cell    │  │
│  BQ load & merge jobs   │◄─────────┤  │  z-score vs. baseline      │  │
│  Baseline refresh       │          │  └────────────────────────────┘  │
│  Cloud mask pipeline    │          │  ┌── Risk Evaluation ─────────┐  │
│  SAR fallback routing   │          │  │  composite scoring         │  │
│  Quota tracking         │          │  │  data-gap penalty          │  │
└────────┬────────────────┘          │  │  asset criticality weight  │  │
         │                           │  └────────────────────────────┘  │
         │                           │  ┌── Reporting ───────────────┐  │
         │                           │  │  JSON/GeoJSON payload      │  │
         │                           │  │  executive summary prose   │  │
         │                           │  │  audit trail               │  │
         │                           │  └────────────────────────────┘  │
         │                           └───────────┬──────────────────────┘
         │                                       │
┌────────▼───────────────────────────────────────▼──────────────────────┐
│                         Data & Storage Layer                          │
│                                                                       │
│  Google Earth Engine ──► Cloud Storage (raster chips, GeoTIFFs)      │
│  PostGIS  (monitoring_regions, asset_boundaries, spatial queries)     │
│  BigQuery (baseline_spectral_stats, risk_events, scan_log, gap_log)   │
└───────────────────────────────────────────────────────────────────────┘
```

**Key fixes vs. v0 spec:**
- Celery and ADK have no execution overlap. Celery handles all I/O. Agents handle all reasoning.
- LangChain removed — ADK is the sole orchestration layer.
- Frontend is a consumer, not a monolith — API designed for multiple consumer types.
- Sensor availability + data gap tracking are explicit pipeline stages.

---

## 4. Component Responsibilities

### Ingestion Module (Celery)
- Scheduled and event-driven scan triggers
- GEE sensor availability check per region/date
- Index eligibility filter (which indices can be computed given available sensors)
- Cloud masking and SAR fallback routing
- GEE server-side computation and export to Cloud Storage
- BQ incremental load with `last_processed_image_date` checkpoint per region

### Analytics Engine (Celery + BigQuery)
- Spectral index calculation (delegated to GEE server-side)
- H3 spatial tessellation of raster outputs
- Raster-vector overlay against asset boundaries (PostGIS)
- Baseline materialisation and incremental refresh
- Data gap logging

### Agent Subsystem (ADK)
- Triggered after ingestion completes (via Celery callback → FastAPI webhook)
- Reads structured inputs from BigQuery — never raw rasters
- Anomaly Detection Agent: per-index z-score evaluation per H3 cell
- Risk Evaluation Agent: weighted fusion across indices, asset criticality, data-gap penalty
- Reporting Agent: structured JSON/GeoJSON + executive prose synthesis

### Storage Layer
- **Cloud Storage**: raw GeoTIFF raster chips per scan/region
- **PostGIS**: monitoring regions, asset boundaries (spatial query engine)
- **BigQuery**: all tabular analytics — baseline stats, risk events, scan log, data gap log

### API Gateway (FastAPI)
- Scan lifecycle (trigger, poll, results)
- Region management (CRUD)
- Webhook registration and delivery
- Downstream dashboard and notification integration

---

## 5. End-to-End Data Flow

```
1. TRIGGER
   Celery beat (schedule) or POST /v1/scans (manual/event)
   → scan record created in scan_log (status: pending)

2. SENSOR AVAILABILITY CHECK
   GEE availability API queried for each sensor (S2_SR, L8_T1, S1_GRD)
   over region bbox and scan_date
   → index eligibility matrix computed

3. CLOUD MASKING & SAR FALLBACK ROUTING
   For each optical index: check cloud cover pct against threshold
   → if above threshold: route to SAR proxy if defined, else log data gap

4. GEE COMPUTATION (server-side)
   Eligible indices computed via GEE PixelwiseExpression
   Cloud-masked, atmospherically corrected
   → GeoTIFF chips exported to Cloud Storage

5. H3 TESSELLATION + BQ LOAD
   Raster chips read, values aggregated to H3 resolution-8 cells
   → loaded incrementally to index_values table in BQ
   → scan_log updated (indices_computed, indices_gap, cloud_cover_pct)

6. RASTER-VECTOR OVERLAY (PostGIS)
   H3 cells intersected with asset_boundaries
   → affected asset IDs resolved per H3 cell

7. AGENT REASONING (ADK)
   Anomaly Detection Agent: compare current index values vs. baseline_spectral_stats
   Risk Evaluation Agent: composite score per asset
   Reporting Agent: JSON/GeoJSON payload + executive summary

8. PERSISTENCE + NOTIFICATION
   risk_events written to BQ
   Webhooks fired for matching severity tiers
   scan_log updated (status: complete)
```

---

## 6. Document Index

| File | Contents |
|---|---|
| `01-data-models.md` | All BigQuery and PostGIS schemas |
| `02-api-spec.md` | Full REST API specification |
| `03-agent-subsystem.md` | ADK agent design, state, failure handling |
| `04-gee-integration.md` | Sensor registry, index registry, processing pipeline |
| `05-analytics-engine.md` | Baseline materialisation, anomaly detection, data gap logic |
| `06-infrastructure.md` | GCP environments, IAM, cost controls |
| `07-observability.md` | Required labels, metrics, alerting |
| `config/index-registry.yaml` | All supported spectral indices |
| `config/monitoring-region.yaml` | Region configuration template |
