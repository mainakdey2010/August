# SERA — Data Models
`v1.0 | 2026-09-07`

---

## BigQuery Schemas

### `sera_analytics.baseline_spectral_stats`

Precomputed historical statistics per spatial cell, per index, per week-of-year.
Built once from the full historical archive, refreshed incrementally each week.

```sql
CREATE TABLE `{project}.sera_analytics.baseline_spectral_stats`
(
  region_id       STRING  NOT NULL,
  h3_cell         STRING  NOT NULL,   -- H3 resolution-8 hex identifier
  index_id        STRING  NOT NULL,   -- 'NDVI' | 'NDRE' | 'LST' | etc.
  sensor_id       STRING  NOT NULL,   -- 'S2_SR' | 'L8_T1' | 'S1_GRD'
  week_of_year    INT64   NOT NULL,   -- 1-52
  p10             FLOAT64,
  p25             FLOAT64,
  p50             FLOAT64,
  p75             FLOAT64,
  p90             FLOAT64,
  mean            FLOAT64,
  stddev          FLOAT64,
  sample_count    INT64,
  baseline_years  ARRAY<INT64>,       -- e.g. [2016, 2017, ..., 2025]
  last_updated    TIMESTAMP
)
PARTITION BY RANGE_BUCKET(week_of_year, GENERATE_ARRAY(1, 53, 1))
CLUSTER BY region_id, index_id, h3_cell
OPTIONS (
  labels = [
    ("workflow_name", "sera-baseline"),
    ("asset_tier",    "n/a"),
    ("env",           "prod")
  ]
);
```

**Query pattern:** anomaly detection joins current index values against this table
on `(region_id, h3_cell, index_id, sensor_id, week_of_year)`.
At H3 resolution-8 (~0.74 km² per cell), a single large region may have ~10k cells.
Clustering by `region_id, index_id, h3_cell` keeps scans tight.

---

### `sera_analytics.index_values`

Raw computed index values per scan, per cell. Incrementally loaded after each GEE export.

```sql
CREATE TABLE `{project}.sera_analytics.index_values`
(
  scan_id         STRING  NOT NULL,
  region_id       STRING  NOT NULL,
  h3_cell         STRING  NOT NULL,
  index_id        STRING  NOT NULL,
  sensor_id       STRING  NOT NULL,
  image_date      DATE    NOT NULL,   -- acquisition date of the source image
  index_value     FLOAT64,
  pixel_count     INT64,              -- valid pixels in cell (post cloud mask)
  coverage_pct    FLOAT64,            -- pct of cell with valid pixels
  created_at      TIMESTAMP
)
PARTITION BY image_date
CLUSTER BY region_id, index_id
OPTIONS (
  labels = [("workflow_name", "sera-ingest"), ("env", "prod")]
);
```

**Incremental guard:** before loading, check `MAX(image_date)` per `(region_id, index_id)`.
Only load images where `image_date > MAX(image_date)`.

---

### `sera_analytics.scan_log`

One record per scan execution. Tracks status, indices, gaps, and job IDs for auditability.

```sql
CREATE TABLE `{project}.sera_analytics.scan_log`
(
  scan_id             STRING  NOT NULL,
  region_id           STRING  NOT NULL,
  scan_date           DATE    NOT NULL,
  triggered_by        STRING,              -- 'schedule' | 'manual' | 'event'
  triggered_by_user   STRING,
  indices_requested   ARRAY<STRING>,
  indices_computed    ARRAY<STRING>,
  indices_gap         ARRAY<STRING>,       -- requested but not computable
  cloud_cover_pct     FLOAT64,
  sar_fallback_used   BOOL,
  sar_fallback_indices ARRAY<STRING>,
  status              STRING,              -- 'pending'|'ingesting'|'reasoning'|'complete'|'failed'|'partial'
  failure_reason      STRING,
  gee_task_ids        ARRAY<STRING>,
  bq_job_ids          ARRAY<STRING>,
  agent_run_id        STRING,
  duration_seconds    INT64,
  created_at          TIMESTAMP,
  updated_at          TIMESTAMP
)
PARTITION BY scan_date
CLUSTER BY region_id, status
OPTIONS (
  labels = [("workflow_name", "sera-scan"), ("env", "prod")]
);
```

---

### `sera_analytics.risk_events`

One record per risk event identified by the Risk Evaluation Agent.

```sql
CREATE TABLE `{project}.sera_analytics.risk_events`
(
  event_id            STRING  NOT NULL,
  scan_id             STRING  NOT NULL,
  region_id           STRING  NOT NULL,
  asset_id            STRING,
  event_date          DATE    NOT NULL,
  risk_tier           STRING,              -- 'CRITICAL'|'HIGH'|'MEDIUM'|'LOW'|'NONE'
  composite_score     FLOAT64,             -- 0.0 – 1.0
  index_scores        JSON,
  -- Structure: {"NDVI": {"z_score": -2.3, "anomaly": true, "weight": 0.35},
  --             "LST":  {"z_score":  3.1, "anomaly": true, "weight": 0.25}, ...}
  data_gap_indices    ARRAY<STRING>,       -- indices missing for this scan
  data_gap_penalty    FLOAT64,
  affected_h3_cells   ARRAY<STRING>,
  geojson_payload     JSON,                -- GeoJSON Feature with full geometry + properties
  agent_reasoning     STRING,              -- ADK agent explanation (structured prose)
  executive_summary   STRING,             -- human-readable summary for reporting
  notified_at         TIMESTAMP,
  notification_channels ARRAY<STRING>,
  created_at          TIMESTAMP
)
PARTITION BY event_date
CLUSTER BY region_id, risk_tier
OPTIONS (
  labels = [("workflow_name", "sera-risk"), ("env", "prod")]
);
```

---

### `sera_analytics.data_gap_log`

Explicit record of every index that could not be computed, and why.
Feeds data-gap penalty in composite risk scoring and data quality reporting.

```sql
CREATE TABLE `{project}.sera_analytics.data_gap_log`
(
  scan_id                 STRING  NOT NULL,
  region_id               STRING  NOT NULL,
  index_id                STRING  NOT NULL,
  gap_date                DATE    NOT NULL,
  reason                  STRING,
  -- 'cloud_cover' | 'sensor_unavailable' | 'gee_quota_exceeded'
  -- | 'export_timeout' | 'no_sar_proxy' | 'insufficient_pixels'
  cloud_cover_pct         FLOAT64,
  sar_fallback_attempted  BOOL,
  sar_fallback_success    BOOL,
  sar_proxy_index_id      STRING,          -- e.g. 'SAR_RVI' used instead of 'NDVI'
  created_at              TIMESTAMP
)
PARTITION BY gap_date
CLUSTER BY region_id, index_id
OPTIONS (
  labels = [("workflow_name", "sera-gaps"), ("env", "prod")]
);
```

---

## PostGIS Schemas

### `monitoring_regions`

```sql
CREATE TABLE monitoring_regions (
  region_id         TEXT        PRIMARY KEY,
  display_name      TEXT        NOT NULL,
  asset_tier        TEXT        NOT NULL CHECK (asset_tier IN ('critical','high','medium','low')),
  geom              GEOMETRY(POLYGON, 4326) NOT NULL,
  buffer_geom       GEOMETRY(POLYGON, 4326),  -- precomputed proximity buffer
  config_yaml       TEXT,                      -- canonical config blob
  active            BOOL        NOT NULL DEFAULT TRUE,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_monitoring_regions_geom        ON monitoring_regions USING GIST(geom);
CREATE INDEX idx_monitoring_regions_buffer_geom ON monitoring_regions USING GIST(buffer_geom);
CREATE INDEX idx_monitoring_regions_tier        ON monitoring_regions(asset_tier);
```

### `asset_boundaries`

```sql
CREATE TABLE asset_boundaries (
  asset_id          TEXT        PRIMARY KEY,
  region_id         TEXT        NOT NULL REFERENCES monitoring_regions(region_id),
  asset_type        TEXT,                    -- 'pipeline' | 'substation' | 'reservoir' | etc.
  asset_tier        TEXT        NOT NULL,
  geom              GEOMETRY(GEOMETRY, 4326) NOT NULL,  -- point, line, or polygon
  criticality_score FLOAT       NOT NULL DEFAULT 0.5,   -- 0.0-1.0, used in risk weighting
  metadata          JSONB,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_asset_boundaries_geom      ON asset_boundaries USING GIST(geom);
CREATE INDEX idx_asset_boundaries_region_id ON asset_boundaries(region_id);
CREATE INDEX idx_asset_boundaries_tier      ON asset_boundaries(asset_tier);
```

---

## Output Schemas

### Risk Event GeoJSON Payload

```json
{
  "type": "Feature",
  "geometry": {
    "type": "MultiPolygon",
    "coordinates": [...]
  },
  "properties": {
    "event_id": "evt_20260907_emea-energy-north_a3b4c5",
    "scan_id": "scan_20260907_emea-energy-north",
    "region_id": "emea-energy-grid-north",
    "event_date": "2026-09-07",
    "risk_tier": "HIGH",
    "composite_score": 0.74,
    "asset_ids": ["ast_001", "ast_002"],
    "index_scores": {
      "NDVI": { "z_score": -2.3, "anomaly": true, "value": 0.21, "baseline_p50": 0.61, "weight": 0.35 },
      "LST":  { "z_score":  3.1, "anomaly": true, "value": 42.1, "baseline_p50": 31.4, "weight": 0.25 },
      "NDRE": { "z_score": -1.2, "anomaly": false, "value": 0.18, "baseline_p50": 0.31, "weight": 0.20 },
      "NBR":  { "z_score": -0.4, "anomaly": false, "value": 0.55, "baseline_p50": 0.60, "weight": 0.20 }
    },
    "data_gap_indices": [],
    "data_gap_penalty": 0.0,
    "executive_summary": "Significant vegetation decline (NDVI z=-2.3) combined with elevated surface temperature (LST z=+3.1) detected across 3 H3 cells adjacent to asset ast_001. Pattern consistent with heat-stress drought onset. Recommend field inspection within 72 hours.",
    "agent_reasoning": "..."
  }
}
```

### Scan Status Response

```json
{
  "scan_id": "scan_20260907_emea-energy-north",
  "region_id": "emea-energy-grid-north",
  "status": "complete",
  "scan_date": "2026-09-07",
  "indices_computed": ["NDVI", "LST", "NDRE", "NBR"],
  "indices_gap": ["BSI"],
  "gap_reasons": {
    "BSI": "cloud_cover (87%); no SAR proxy available"
  },
  "cloud_cover_pct": 0.87,
  "sar_fallback_used": false,
  "risk_events_count": 2,
  "duration_seconds": 847,
  "results_url": "/v1/scans/scan_20260907_emea-energy-north/results"
}
```
