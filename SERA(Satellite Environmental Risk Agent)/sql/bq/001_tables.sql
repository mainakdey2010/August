-- SERA BigQuery Schema
-- Applies all feedback fixes:
--   - index_values: scan_id added to CLUSTER BY (fix #13 — partition pruning)
--   - index_values: partition expiration 365 days (fix #4.2)
--   - risk_events:  quality_score + quality_tier columns (replaces additive gap penalty)
--   - agent state:  separate BQ tables instead of PostGIS JSONB (fix #3)
-- Run with: bq query --use_legacy_sql=false --project_id=<project> < 001_tables.sql

-- ── baseline_spectral_stats ────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS `{project}.{dataset}.baseline_spectral_stats`
(
  region_id       STRING  NOT NULL,
  h3_cell         STRING  NOT NULL,
  index_id        STRING  NOT NULL,
  sensor_id       STRING  NOT NULL,
  week_of_year    INT64   NOT NULL,
  mean            FLOAT64,
  stddev          FLOAT64,
  p10             FLOAT64,
  p25             FLOAT64,
  p50             FLOAT64,
  p75             FLOAT64,
  p90             FLOAT64,
  sample_count    INT64,
  baseline_years  ARRAY<INT64>,
  last_updated    TIMESTAMP
)
PARTITION BY RANGE_BUCKET(week_of_year, GENERATE_ARRAY(1, 53, 1))
CLUSTER BY region_id, index_id, h3_cell
OPTIONS (
  description = "Pre-computed per-H3-cell spectral index baselines from GEE server-side reduction",
  labels      = [("workflow_name", "sera-baseline"), ("env", "prod")]
);


-- ── index_values ─────────────────────────────────────────────────────────────
-- Fix #13: CLUSTER BY includes scan_id so anomaly detection query (WHERE scan_id=@x) is efficient
-- Fix #4.2: partition_expiration_days = 365 — raw values expire, baselines are permanent

CREATE TABLE IF NOT EXISTS `{project}.{dataset}.index_values`
(
  scan_id         STRING  NOT NULL,
  region_id       STRING  NOT NULL,
  h3_cell         STRING  NOT NULL,
  index_id        STRING  NOT NULL,
  sensor_id       STRING  NOT NULL,
  image_date      DATE    NOT NULL,
  index_value     FLOAT64,
  pixel_count     INT64,
  stddev          FLOAT64,
  coverage_pct    FLOAT64,
  created_at      TIMESTAMP
)
PARTITION BY image_date
CLUSTER BY scan_id, region_id, index_id        -- scan_id first: anomaly query filters by scan_id
OPTIONS (
  description              = "Per-H3-cell spectral index values, loaded from GEE server-side CSV export",
  partition_expiration_days = 365,             -- raw values expire; baselines are permanent
  labels                   = [("workflow_name", "sera-ingest"), ("env", "prod")]
);


-- ── scan_log ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS `{project}.{dataset}.scan_log`
(
  scan_id               STRING  NOT NULL,
  region_id             STRING  NOT NULL,
  scan_date             DATE    NOT NULL,
  triggered_by          STRING,
  triggered_by_user     STRING,
  indices_requested     ARRAY<STRING>,
  indices_computed      ARRAY<STRING>,
  indices_gap           ARRAY<STRING>,
  cloud_cover_pct       FLOAT64,
  sar_fallback_used     BOOL,
  sar_fallback_indices  ARRAY<STRING>,
  status                STRING,
  failure_reason        STRING,
  gee_task_ids          ARRAY<STRING>,
  bq_job_ids            ARRAY<STRING>,
  agent_run_id          STRING,
  duration_seconds      INT64,
  created_at            TIMESTAMP,
  updated_at            TIMESTAMP
)
PARTITION BY scan_date
CLUSTER BY region_id, status
OPTIONS (
  labels = [("workflow_name", "sera-scan"), ("env", "prod")]
);


-- ── risk_events ──────────────────────────────────────────────────────────────
-- Fix: gap penalty removed from score; quality_score / quality_tier added as separate dims

CREATE TABLE IF NOT EXISTS `{project}.{dataset}.risk_events`
(
  event_id              STRING  NOT NULL,
  scan_id               STRING  NOT NULL,
  region_id             STRING  NOT NULL,
  asset_id              STRING,
  event_date            DATE    NOT NULL,
  risk_tier             STRING,
  composite_score       FLOAT64,
  index_scores          JSON,
  -- quality dimensions (separate from risk)
  quality_score         FLOAT64,               -- fraction of requested indices computed (0-1)
  quality_tier          STRING,               -- GOOD | DEGRADED | POOR
  data_gap_indices      ARRAY<STRING>,
  data_gap_penalty      FLOAT64,              -- retained for schema compat, always 0.0
  affected_h3_cells     ARRAY<STRING>,
  geojson_payload       JSON,
  agent_reasoning       STRING,
  executive_summary     STRING,
  notified_at           TIMESTAMP,
  notification_channels ARRAY<STRING>,
  created_at            TIMESTAMP
)
PARTITION BY event_date
CLUSTER BY region_id, risk_tier
OPTIONS (
  labels = [("workflow_name", "sera-risk"), ("env", "prod")]
);


-- ── data_gap_log ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS `{project}.{dataset}.data_gap_log`
(
  scan_id                  STRING  NOT NULL,
  region_id                STRING  NOT NULL,
  index_id                 STRING  NOT NULL,
  gap_date                 DATE    NOT NULL,
  reason                   STRING,
  cloud_cover_pct          FLOAT64,
  sar_fallback_attempted   BOOL,
  sar_fallback_success     BOOL,
  sar_proxy_index_id       STRING,
  created_at               TIMESTAMP
)
PARTITION BY gap_date
CLUSTER BY region_id, index_id
OPTIONS (
  labels = [("workflow_name", "sera-gaps"), ("env", "prod")]
);


-- ── agent_anomaly_results  (BQ-based agent state — replaces Postgres JSONB) ──

CREATE TABLE IF NOT EXISTS `{project}.{dataset}.agent_anomaly_results`
(
  scan_id     STRING    NOT NULL,
  region_id   STRING    NOT NULL,
  stage       STRING    NOT NULL,   -- always 'anomaly_detection'
  payload     JSON      NOT NULL,
  created_at  TIMESTAMP NOT NULL
)
PARTITION BY DATE(created_at)
CLUSTER BY scan_id
OPTIONS (
  partition_expiration_days = 90,
  labels = [("workflow_name", "sera-agents"), ("env", "prod")]
);


-- ── agent_risk_results ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS `{project}.{dataset}.agent_risk_results`
(
  scan_id     STRING    NOT NULL,
  region_id   STRING    NOT NULL,
  stage       STRING    NOT NULL,   -- always 'risk_evaluation'
  payload     JSON      NOT NULL,
  created_at  TIMESTAMP NOT NULL
)
PARTITION BY DATE(created_at)
CLUSTER BY scan_id
OPTIONS (
  partition_expiration_days = 90,
  labels = [("workflow_name", "sera-agents"), ("env", "prod")]
);
