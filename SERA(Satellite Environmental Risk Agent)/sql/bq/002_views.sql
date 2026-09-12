-- SERA BigQuery Views
-- monitoring_regions_view: bridging layer between Postgres (source of truth for regions)
-- and BigQuery (where ingest/agent tasks query region configs).
--
-- Two options — pick one based on setup:
--
-- Option A (recommended for MVP): BigQuery External Table over Cloud SQL
--   Create a Cloud SQL → BQ connection via BigQuery Omni / federated queries.
--   Config in GCP Console: BigQuery > External Connections > Cloud SQL.
--
-- Option B (simpler, small region count): materialized copy synced by a beat task.
--   A Celery beat task (sync_regions_to_bq) runs every 15min and does a full replace.
--   Use this when region count < 1000 and sync latency is acceptable.
--
-- The view definition below assumes Option B (materialized table already populated).

-- ── Option B: materialized regions table (synced from Postgres) ───────────────

CREATE TABLE IF NOT EXISTS `{project}.{dataset}.monitoring_regions_cache`
(
  region_id     STRING  NOT NULL,
  display_name  STRING,
  asset_tier    STRING,
  interval_days INT64,
  active        BOOL,
  config_yaml   STRING,   -- full YAML config blob
  geom_wkt      STRING,   -- WKT geometry (for reference; spatial ops done in PostGIS)
  synced_at     TIMESTAMP
)
OPTIONS (
  labels = [("workflow_name", "sera-sync"), ("env", "prod")]
);

-- View alias used throughout the codebase
CREATE OR REPLACE VIEW `{project}.{dataset}.monitoring_regions_view` AS
SELECT * FROM `{project}.{dataset}.monitoring_regions_cache`;

-- ── Beat task to sync regions (add to celery_app.beat_schedule) ──────────────
-- "sync-regions-to-bq": every 15 minutes
-- Task: sera.tasks.ingest.sync_regions_to_bq
--
-- The task does:
--   1. SELECT * FROM monitoring_regions (Postgres)
--   2. WRITE_TRUNCATE to monitoring_regions_cache (BQ)
-- This keeps region configs in BQ without a federated connection requirement.
