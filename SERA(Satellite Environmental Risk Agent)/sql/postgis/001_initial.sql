-- SERA PostGIS Schema
-- Fix #8: asset_boundaries versioned with valid_from/valid_to
--         raster-vector overlay must use current-as-of-scan-date geometry

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── monitoring_regions ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS monitoring_regions (
  region_id         TEXT        PRIMARY KEY,
  display_name      TEXT        NOT NULL,
  asset_tier        TEXT        NOT NULL CHECK (asset_tier IN ('critical','high','medium','low')),
  interval_days     INT         NOT NULL DEFAULT 7,
  geom              GEOMETRY(POLYGON, 4326) NOT NULL,
  buffer_geom       GEOMETRY(POLYGON, 4326),
  config_yaml       TEXT,
  active            BOOL        NOT NULL DEFAULT TRUE,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_monitoring_regions_geom        ON monitoring_regions USING GIST(geom);
CREATE INDEX IF NOT EXISTS idx_monitoring_regions_buffer_geom ON monitoring_regions USING GIST(buffer_geom);
CREATE INDEX IF NOT EXISTS idx_monitoring_regions_tier        ON monitoring_regions(asset_tier);
CREATE INDEX IF NOT EXISTS idx_monitoring_regions_active       ON monitoring_regions(active);


-- ── asset_boundaries (versioned) ──────────────────────────────────────────────
-- Fix #8: valid_from/valid_to enable "as-of-date" geometry lookups.
-- Raster-vector overlay: WHERE valid_from <= scan_date AND (valid_to IS NULL OR valid_to > scan_date)

CREATE TABLE IF NOT EXISTS asset_boundaries (
  id                UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
  asset_id          TEXT        NOT NULL,
  region_id         TEXT        NOT NULL REFERENCES monitoring_regions(region_id),
  asset_type        TEXT,
  asset_tier        TEXT        NOT NULL,
  geom              GEOMETRY(GEOMETRY, 4326) NOT NULL,
  criticality_score FLOAT       NOT NULL DEFAULT 0.5 CHECK (criticality_score BETWEEN 0.0 AND 1.0),
  valid_from        DATE        NOT NULL DEFAULT CURRENT_DATE,
  valid_to          DATE,       -- NULL = currently active
  change_reason     TEXT,       -- e.g. "boundary expansion 2026-09", "asset decommissioned"
  metadata          JSONB,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Spatial index
CREATE INDEX IF NOT EXISTS idx_asset_boundaries_geom      ON asset_boundaries USING GIST(geom);
-- Lookup indexes
CREATE INDEX IF NOT EXISTS idx_asset_boundaries_asset_id  ON asset_boundaries(asset_id);
CREATE INDEX IF NOT EXISTS idx_asset_boundaries_region_id ON asset_boundaries(region_id);
CREATE INDEX IF NOT EXISTS idx_asset_boundaries_validity  ON asset_boundaries(valid_from, valid_to);


-- ── Helper view: current active asset boundaries ───────────────────────────────

CREATE OR REPLACE VIEW current_asset_boundaries AS
SELECT *
FROM asset_boundaries
WHERE valid_from <= CURRENT_DATE
  AND (valid_to IS NULL OR valid_to > CURRENT_DATE);


-- ── Helper function: get asset boundaries as-of a given date ─────────────────

CREATE OR REPLACE FUNCTION asset_boundaries_as_of(as_of_date DATE)
RETURNS TABLE (
  id                UUID,
  asset_id          TEXT,
  region_id         TEXT,
  asset_type        TEXT,
  asset_tier        TEXT,
  geom              GEOMETRY,
  criticality_score FLOAT,
  metadata          JSONB
)
LANGUAGE SQL STABLE AS $$
  SELECT id, asset_id, region_id, asset_type, asset_tier, geom, criticality_score, metadata
  FROM asset_boundaries
  WHERE valid_from <= as_of_date
    AND (valid_to IS NULL OR valid_to > as_of_date)
$$;


-- ── Raster-vector overlay query (parameterised) ───────────────────────────────
-- Usage: pass h3 cell WKT polygons from Python h3.cell_to_boundary()
-- Returns asset_id + h3_cell pairs for the scan date

-- Example: called from sera/storage/postgis.py
-- SELECT DISTINCT ab.asset_id, ab.asset_tier, ab.criticality_score, hcv.h3_cell
-- FROM (VALUES ($1::text), ($2::text), ...) AS hcv(h3_cell)
-- JOIN asset_boundaries_as_of($scan_date) ab
--   ON ST_Intersects(ab.geom, ST_GeomFromText(hcv.h3_cell, 4326))
-- WHERE ab.region_id = $region_id;


-- ── agent_runs (lightweight coordination record — no payload stored here) ──────
-- Payload now lives in BQ agent_anomaly_results / agent_risk_results.

CREATE TABLE IF NOT EXISTS agent_runs (
  agent_run_id    TEXT        PRIMARY KEY,
  scan_id         TEXT        NOT NULL,
  region_id       TEXT        NOT NULL,
  status          TEXT        NOT NULL DEFAULT 'pending',
  failure_stage   TEXT,
  failure_reason  TEXT,
  retry_count     INT         NOT NULL DEFAULT 0,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_scan_id ON agent_runs(scan_id);
