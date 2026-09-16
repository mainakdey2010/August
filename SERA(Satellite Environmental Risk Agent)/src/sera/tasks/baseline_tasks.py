"""
Baseline and H3 asset management tasks.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import ee
from celery import shared_task
from google.api_core.exceptions import GoogleAPIError
from google.cloud import bigquery

from sera.gee.baseline import build_baseline_in_gee, load_baseline_csv_to_bq
from sera.gee.compute import region_geojson_to_h3_fc, upload_h3_cells_as_asset

log = logging.getLogger(__name__)

BQ_DATASET = os.environ.get("BQ_DATASET", "sera_analytics_dev")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "sera-rasters-dev")
ENV        = os.environ.get("ENV", "dev")


@shared_task(
    name="sera.tasks.baseline_tasks.upload_h3_asset_for_region",
    bind=True,
    max_retries=3,
    autoretry_for=(ee.EEException, GoogleAPIError),
    retry_backoff=True,
)
def upload_h3_asset_for_region(self, region_id: str, region_geom_json: dict) -> str:
    """
    Convert region boundary → H3 cells → GEE Asset.
    Called once on region registration. Must complete before first scan can run.
    Returns GEE asset ID.
    """
    ee.Initialize(project=os.environ.get("GCP_PROJECT", "august-505217"))
    h3_fc    = region_geojson_to_h3_fc(region_geom_json, resolution=8)
    asset_id = upload_h3_cells_as_asset(h3_fc, region_id, ENV)
    log.info("H3 asset upload submitted region=%s asset=%s", region_id, asset_id)
    return asset_id


@shared_task(
    name="sera.tasks.baseline_tasks.build_baseline_for_region",
    bind=True,
    max_retries=2,
    autoretry_for=(ee.EEException, GoogleAPIError),
    retry_backoff=True,
    retry_backoff_max=300,
)
def build_baseline_for_region(
    self,
    region_id: str,
    region_geom_json: dict,
    index_id: str,
    sensor_id: str,
    baseline_start_year: int,
    baseline_end_year: int,
) -> str:
    """
    Build baseline stats for one (region, index) pair via GEE.
    Called once on region registration per configured index.
    For very large regions (>50k H3 cells), see tiling note below.
    """
    ee.Initialize(project=os.environ.get("GCP_PROJECT", "august-505217"))

    region_geom = ee.Geometry(region_geom_json)
    h3_fc       = _load_or_build_h3_fc(region_id, region_geom_json)

    # GEE baseline export — for regions >50k cells this may hit GEE feature limits.
    # If cell_count > 50_000: split into decade chunks and merge in BQ.
    cell_count = _estimate_cell_count(region_geom_json)
    if cell_count > 50_000:
        log.warning(
            "Region %s has ~%d H3 cells — splitting baseline into decade chunks",
            region_id, cell_count,
        )
        return _build_baseline_chunked(
            region_id, region_geom, h3_fc, index_id, sensor_id,
            baseline_start_year, baseline_end_year,
        )

    task_id = build_baseline_in_gee(
        region_id           = region_id,
        region_geom         = region_geom,
        index_id            = index_id,
        sensor_id           = sensor_id,
        h3_fc               = h3_fc,
        baseline_start_year = baseline_start_year,
        baseline_end_year   = baseline_end_year,
        gcs_bucket          = GCS_BUCKET,
        env                 = ENV,
    )
    log.info("Baseline GEE task submitted region=%s index=%s task=%s", region_id, index_id, task_id)
    return task_id


@shared_task(name="sera.tasks.baseline_tasks.refresh_all_baselines")
def refresh_all_baselines() -> list[str]:
    """
    Beat task: Sunday 02:00 UTC — trigger incremental baseline refresh for all active regions.
    Incremental = last 1 year only (adds the most recent season to the baseline).
    """
    from datetime import date
    bq      = bigquery.Client()
    regions = _load_active_region_configs(bq)
    launched = []

    for r in regions:
        for idx_cfg in r["config"].get("indices", []):
            if not idx_cfg.get("enabled", True):
                continue
            from sera.config.index_registry import get_index
            defn      = get_index(idx_cfg["id"])
            sensor_id = defn["sensors"]["primary"]
            end_year  = date.today().year
            start_year= end_year - 1     # incremental: last year only

            build_baseline_for_region.delay(
                region_id           = r["region_id"],
                region_geom_json    = r["geom_json"],
                index_id            = idx_cfg["id"],
                sensor_id           = sensor_id,
                baseline_start_year = start_year,
                baseline_end_year   = end_year,
            )
            launched.append(f"{r['region_id']}:{idx_cfg['id']}")

    log.info("Baseline refresh launched for %d (region, index) pairs", len(launched))
    return launched


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_or_build_h3_fc(region_id: str, region_geom_json: dict) -> ee.FeatureCollection:
    """Try to load pre-uploaded GEE asset; fall back to building inline."""
    from sera.gee.compute import load_h3_asset, region_geojson_to_h3_fc
    try:
        fc = load_h3_asset(region_id, ENV)
        fc.size().getInfo()   # validates asset exists
        return fc
    except Exception:
        log.warning("H3 asset not found for region=%s — building inline", region_id)
        return region_geojson_to_h3_fc(region_geom_json)


def _estimate_cell_count(region_geom_json: dict) -> int:
    import h3
    try:
        cells = h3.polyfill_geojson(region_geom_json, 8)
        return len(cells)
    except Exception:
        return 0


def _build_baseline_chunked(
    region_id: str,
    region_geom: ee.Geometry,
    h3_fc: ee.FeatureCollection,
    index_id: str,
    sensor_id: str,
    start_year: int,
    end_year: int,
) -> str:
    """
    Split baseline build into 10-year decade chunks.
    Each chunk is a separate GEE task → separate CSV → loaded to BQ with WRITE_APPEND.
    """
    chunk_size = 10
    task_ids   = []

    for decade_start in range(start_year, end_year, chunk_size):
        decade_end = min(decade_start + chunk_size - 1, end_year)
        task_id = build_baseline_in_gee(
            region_id           = region_id,
            region_geom         = region_geom,
            index_id            = index_id,
            sensor_id           = sensor_id,
            h3_fc               = h3_fc,
            baseline_start_year = decade_start,
            baseline_end_year   = decade_end,
            gcs_bucket          = GCS_BUCKET,
            env                 = ENV,
        )
        task_ids.append(task_id)
        log.info(
            "Chunked baseline task submitted region=%s index=%s years=%d-%d task=%s",
            region_id, index_id, decade_start, decade_end, task_id,
        )

    return ",".join(task_ids)


def _load_active_region_configs(bq: bigquery.Client) -> list[dict]:
    import yaml, json
    query = f"""
    SELECT region_id, config_yaml,
           ST_AsGeoJSON(ST_GeomFromText('POINT(0 0)')) AS geom_placeholder
    FROM `{BQ_DATASET}.monitoring_regions_view`
    WHERE active = TRUE
    """
    # In practice, geom comes from PostGIS — this query is illustrative.
    rows = list(bq.query(query).result())
    return [
        {
            "region_id": r["region_id"],
            "config":    yaml.safe_load(r["config_yaml"]),
            "geom_json": {},   # fetched from PostGIS in real impl
        }
        for r in rows
    ]

