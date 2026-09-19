"""
Ingestion tasks — updated to use server-side GEE H3 reduction.

Flow per scan:
  1. sensor_availability check (Redis-cached)
  2. index eligibility filter
  3. For each computable index: compute_index_and_export() → GEE task ID
  4. Record GEE task IDs in scan_log
  5. Poll via gee_poller (separate beat task)
  6. On all GEE tasks complete: load CSVs to BQ → trigger agent chain
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any

import ee
import redis as redis_lib
from celery import shared_task
from google.api_core.exceptions import GoogleAPIError
from google.cloud import bigquery

from sera.gee.availability import check_sensor_availability, compute_index_eligibility
from sera.gee.compute import compute_index_and_export, load_h3_csv_to_bq, load_h3_asset
from sera.storage.bigquery import get_last_processed_date, update_scan_log

log = logging.getLogger(__name__)

GCS_BUCKET = os.environ.get("GCS_BUCKET", "sera-rasters-dev")
ENV        = os.environ.get("ENV", "dev")
BQ_DATASET = os.environ.get("BQ_DATASET", "sera_analytics_dev")

# GEE slot semaphore (Redis INCR/DECR pattern)
GEE_MAX_CONCURRENT = int(os.environ.get("GEE_MAX_CONCURRENT", "8"))
GEE_SLOT_KEY       = f"sera:gee:active_tasks:{ENV}"
GEE_SLOT_EXPIRY_S  = 3600

# Atomic Lua script: check-and-increment in one round-trip.
# Returns 1 if slots acquired, 0 if over limit.
# Prevents TOCTOU race when two workers call GET then INCRBY concurrently.
_GEE_ACQUIRE_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local needed  = tonumber(ARGV[1])
local limit   = tonumber(ARGV[2])
if current + needed > limit then
    return 0
end
redis.call('INCRBY', KEYS[1], needed)
redis.call('EXPIRE',  KEYS[1], ARGV[3])
return 1
"""


def _bq() -> bigquery.Client:
    return bigquery.Client()


def _redis() -> redis_lib.Redis:
    return redis_lib.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))


@shared_task(
    name="sera.tasks.ingest.run_scan",
    bind=True,
    max_retries=3,
    autoretry_for=(ee.EEException, GoogleAPIError),   # retry on transient GCP/GEE errors only
    retry_backoff=True,
    retry_backoff_max=120,
)
def run_scan(
    self,
    scan_id: str,
    region_id: str,
    scan_date: str,           # ISO date string
    region_geom_geojson: dict,
    region_bbox: list[float],
    region_config: dict,
) -> dict[str, Any]:
    """
    Entry-point task for a single region scan.
    Dispatches one GEE export task per computable index.
    Returns summary dict written into scan_log.
    """
    ee.Initialize(project=os.environ.get("GCP_PROJECT", "august-505217"))
    rc    = _redis()
    bq    = _bq()
    scan_dt = date.fromisoformat(scan_date)

    update_scan_log(bq, BQ_DATASET, scan_id, region_id, {"status": "ingesting", "scan_date": scan_dt})

    region_geom = ee.Geometry(region_geom_geojson)

    # 1. Sensor availability (Redis-cached)
    availability = check_sensor_availability(
        region_geom=region_geom,
        region_bbox=region_bbox,
        scan_date=scan_dt,
        redis_client=rc,
        window_days=region_config.get("monitoring", {}).get("gee_window_days", 5),
    )

    cloud_threshold = region_config.get("monitoring", {}).get("cloud_cover_threshold", 0.70)
    computable, sar_fallback, gaps = compute_index_eligibility(
        availability, region_config["indices"], cloud_threshold
    )

    # Record data gaps immediately
    gap_index_ids = [idx_id for idx_id, _ in gaps]
    _record_gaps(bq, scan_id, region_id, scan_date, gaps, availability)

    if not computable and not sar_fallback:
        log.warning("scan=%s: no computable indices, marking partial", scan_id)
        update_scan_log(bq, BQ_DATASET, scan_id, region_id, {
            "status":         "partial",
            "indices_gap":    gap_index_ids,
            "failure_reason": "no_computable_indices",
        })
        return {"scan_id": scan_id, "status": "partial", "gaps": gap_index_ids}

    # Atomic GEE slot acquisition — Lua prevents TOCTOU race under concurrent scan triggers
    needed  = len(computable) + len(sar_fallback)
    acquire = rc.register_script(_GEE_ACQUIRE_LUA)
    acquired = acquire(
        keys=[GEE_SLOT_KEY],
        args=[needed, GEE_MAX_CONCURRENT, GEE_SLOT_EXPIRY_S],
    )
    if not acquired:
        active = int(rc.get(GEE_SLOT_KEY) or 0)
        log.warning(
            "GEE quota tight: active=%d needed=%d limit=%d — retrying in 60s",
            active, needed, GEE_MAX_CONCURRENT,
        )
        raise self.retry(countdown=60)

    # 2. Load H3 cells for this region from GEE Asset
    # Guard: if the H3 asset isn't ready yet (region registered but upload still running),
    # retry in 60s rather than failing permanently.
    h3_fc = _load_h3_with_readiness_check(self, region_id)

    # 3. Launch GEE export tasks per index
    gee_task_ids: dict[str, str] = {}   # {index_id: gee_task_id}

    for index_id, sensor_id in (computable + sar_fallback):
        avail = availability.get(sensor_id, {})
        image_date = avail.get("best_image_date", scan_date)

        # Incremental check — skip if image already processed
        last = get_last_processed_date(bq, BQ_DATASET, region_id, index_id, sensor_id)
        if last and str(last) >= image_date:
            log.info("scan=%s index=%s already processed for %s, skipping", scan_id, index_id, image_date)
            continue

        task_id = compute_index_and_export(
            sensor_id=sensor_id,
            image_date=image_date,
            index_id=index_id,
            scan_id=scan_id,
            region_id=region_id,
            region_geom=region_geom,
            h3_fc=h3_fc,
            gcs_bucket=GCS_BUCKET,
            env=ENV,
        )
        gee_task_ids[index_id] = task_id

    computed_ids = list(gee_task_ids.keys())

    # 4. Persist GEE task IDs — gee_poller will watch these and trigger agents when done
    update_scan_log(bq, BQ_DATASET, scan_id, region_id, {
        "status":             "ingesting",
        "indices_computed":   computed_ids,
        "indices_gap":        gap_index_ids,
        "cloud_cover_pct":    _avg_cloud_cover(availability, computable),
        "sar_fallback_used":  len(sar_fallback) > 0,
        "gee_task_ids":       list(gee_task_ids.values()),
    })

    return {
        "scan_id":         scan_id,
        "gee_task_ids":    gee_task_ids,
        "indices_computed": computed_ids,
        "indices_gap":     gap_index_ids,
    }


@shared_task(name="sera.tasks.ingest.load_completed_gee_export")
def load_completed_gee_export(
    scan_id: str,
    region_id: str,
    index_id: str,
    gcs_prefix: str,
) -> str:
    """
    Called by gee_poller when a GEE export task completes.
    Loads CSV from GCS into BQ index_values table.
    """
    bq  = _bq()
    rc  = _redis()

    gcs_uri = f"gs://{GCS_BUCKET}/{gcs_prefix}*.csv"
    job_id  = load_h3_csv_to_bq(gcs_uri, bq, BQ_DATASET, scan_id, region_id)

    rc.decr(GEE_SLOT_KEY)
    # Mark BQ load complete (separate from "dispatched" key set by poller)
    rc.setex(f"sera:gee:bq_done:{scan_id}:{index_id}", 86400, "1")

    return job_id


@shared_task(name="sera.tasks.ingest.trigger_due_scans")
def trigger_due_scans() -> list[str]:
    """
    Beat task: check which regions are due for a scan and enqueue them.
    A region is due when: today >= last_scan_date + interval_days.
    """
    from sera.tasks.ingest import run_scan
    bq = _bq()
    # Query active regions due for scanning
    query = f"""
    SELECT
      r.region_id,
      r.config_yaml,
      COALESCE(last.last_scan, DATE('2000-01-01')) AS last_scan
    FROM `{BQ_DATASET}.monitoring_regions_view` r
    LEFT JOIN (
      SELECT region_id, MAX(scan_date) AS last_scan
      FROM `{BQ_DATASET}.scan_log`
      WHERE status IN ('complete', 'partial')
      GROUP BY region_id
    ) last USING (region_id)
    WHERE r.active = TRUE
      AND DATE_ADD(
        COALESCE(last.last_scan, DATE('2000-01-01')),
        INTERVAL r.interval_days DAY
      ) <= CURRENT_DATE()
    """
    rows = list(bq.query(query).result())
    launched = []
    for row in rows:
        import uuid, yaml
        cfg = yaml.safe_load(row["config_yaml"])
        scan_id = f"scan_{date.today().isoformat().replace('-','')}_{row['region_id']}"
        run_scan.delay(
            scan_id=scan_id,
            region_id=row["region_id"],
            scan_date=date.today().isoformat(),
            region_geom_geojson=cfg["geometry"],
            region_bbox=cfg.get("bbox", []),
            region_config=cfg,
        )
        launched.append(scan_id)
    return launched


def _load_h3_with_readiness_check(task, region_id: str) -> ee.FeatureCollection:
    """
    Load H3 asset, retrying if the GEE asset upload is still in progress.
    Raises task retry (60s backoff) if not ready within max_retries.
    """
    from sera.gee.compute import load_h3_asset, region_geojson_to_h3_fc

    try:
        fc = load_h3_asset(region_id, ENV)
        fc.size().getInfo()   # validates asset exists and is accessible
        return fc
    except ee.EEException as e:
        if "not found" in str(e).lower() or "not exist" in str(e).lower():
            log.warning(
                "H3 asset not ready for region=%s (upload still running) — retrying in 60s",
                region_id,
            )
            raise task.retry(countdown=60, exc=e)
        raise


def _record_gaps(bq, scan_id, region_id, scan_date, gaps, availability):
    rows = []
    for index_id, reason in gaps:
        primary_avail = availability.get("S2_SR", {})
        rows.append({
            "scan_id":               scan_id,
            "region_id":             region_id,
            "index_id":              index_id,
            "gap_date":              scan_date,
            "reason":                reason,
            "cloud_cover_pct":       primary_avail.get("cloud_cover_pct"),
            "sar_fallback_attempted": False,
            "sar_fallback_success":   False,
            "created_at":            datetime.now(timezone.utc).isoformat(),
        })
    if rows:
        bq.insert_rows_json(f"{BQ_DATASET}.data_gap_log", rows)


def _avg_cloud_cover(availability, computable):
    covers = [
        availability.get(sid, {}).get("cloud_cover_pct", 0.0)
        for _, sid in computable
    ]
    return sum(covers) / len(covers) if covers else 0.0



