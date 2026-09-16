"""
SERA FastAPI application — scan lifecycle, regions, webhooks.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import date, datetime, timezone
from typing import Any

import redis as redis_lib
from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from google.cloud import bigquery
from pydantic import BaseModel, Field

from sera.storage.bigquery import update_scan_log

log = logging.getLogger(__name__)

app = FastAPI(title="SERA API", version="1.0.0")

BQ_DATASET = os.environ.get("BQ_DATASET", "sera_analytics_dev")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "sera-rasters-dev")
ENV        = os.environ.get("ENV", "dev")


def _bq() -> bigquery.Client:
    return bigquery.Client()


def _redis() -> redis_lib.Redis:
    return redis_lib.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))


# ── Request / Response models ─────────────────────────────────────────────────

class ScanRequest(BaseModel):
    region_ids:      list[str]
    scan_date:       date | None = None
    force_recompute: bool        = False
    index_override:  list[str]   = Field(default_factory=list)


class ScanResponse(BaseModel):
    scan_id:   str
    region_id: str
    status:    str
    poll_url:  str


class WebhookRegisterRequest(BaseModel):
    url:        str
    secret:     str
    events:     list[str] = Field(default=["CRITICAL", "HIGH"])
    region_ids: list[str] = Field(default_factory=list)
    active:     bool       = True


# ── Scans ─────────────────────────────────────────────────────────────────────

@app.post("/v1/scans", response_model=list[ScanResponse], status_code=status.HTTP_202_ACCEPTED)
async def trigger_scans(req: ScanRequest) -> list[ScanResponse]:
    from sera.tasks.ingest import run_scan

    bq        = _bq()
    scan_date = req.scan_date or date.today()
    responses = []

    for region_id in req.region_ids:
        scan_id = f"scan_{scan_date.isoformat().replace('-', '')}_{region_id}"

        # Check duplicate
        if not req.force_recompute:
            existing = _get_scan_status(bq, scan_id)
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code":    "SCAN_ALREADY_EXISTS",
                        "message": f"Scan for {region_id} on {scan_date} already exists.",
                        "scan_id": scan_id,
                    },
                )

        region_cfg = _load_region_config(bq, region_id)
        if not region_cfg:
            raise HTTPException(status_code=404, detail=f"Region '{region_id}' not found.")

        # Create scan_log record
        _create_scan_record(bq, scan_id, region_id, scan_date)

        # Enqueue Celery task
        run_scan.delay(
            scan_id             = scan_id,
            region_id           = region_id,
            scan_date           = scan_date.isoformat(),
            region_geom_geojson = region_cfg["geometry"],
            region_bbox         = region_cfg.get("bbox", []),
            region_config       = region_cfg,
        )

        responses.append(ScanResponse(
            scan_id   = scan_id,
            region_id = region_id,
            status    = "pending",
            poll_url  = f"/v1/scans/{scan_id}",
        ))

    return responses


@app.get("/v1/scans/{scan_id}")
async def get_scan_status(scan_id: str) -> dict[str, Any]:
    bq  = _bq()
    row = _get_scan_status(bq, scan_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Scan '{scan_id}' not found.")
    return row


@app.get("/v1/scans/{scan_id}/results")
async def get_scan_results(
    scan_id: str,
    format: str = Query("json", regex="^(json|geojson)$"),
    min_risk_tier: str = Query("LOW", regex="^(CRITICAL|HIGH|MEDIUM|LOW)$"),
) -> dict[str, Any]:
    bq     = _bq()
    status = _get_scan_status(bq, scan_id)
    if not status:
        raise HTTPException(status_code=404)
    if status["status"] not in ("complete", "partial"):
        raise HTTPException(status_code=425, detail="Scan not yet complete.")

    events = _load_risk_events(bq, scan_id, min_risk_tier)
    return {
        "scan_id":   scan_id,
        "scan_date": status["scan_date"],
        "region_id": status["region_id"],
        "status":    status["status"],
        "summary":   {
            "indices_computed": status.get("indices_computed", []),
            "indices_gap":      status.get("indices_gap", []),
            "risk_events":      _count_by_tier(events),
        },
        "events": events,
    }


# ── Regions ───────────────────────────────────────────────────────────────────

@app.get("/v1/regions")
async def list_regions(
    asset_tier: str | None = None,
    active: bool = True,
) -> dict[str, Any]:
    bq    = _bq()
    query = f"""
    SELECT region_id, display_name, asset_tier, active,
           JSON_EXTRACT_SCALAR(config_yaml, '$.monitoring.interval_days') AS interval_days
    FROM `{BQ_DATASET}.monitoring_regions_view`
    WHERE active = @active
    {'AND asset_tier = @asset_tier' if asset_tier else ''}
    ORDER BY asset_tier, region_id
    """
    params = [bigquery.ScalarQueryParameter("active", "BOOL", active)]
    if asset_tier:
        params.append(bigquery.ScalarQueryParameter("asset_tier", "STRING", asset_tier))
    rows = list(bq.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())
    return {"regions": [dict(r) for r in rows], "total": len(rows)}


@app.get("/v1/health")
async def health() -> dict[str, Any]:
    rc    = _redis()
    bq    = _bq()
    gee_active = int(rc.get(f"sera:gee:active_tasks:{ENV}") or 0)

    components: dict[str, str] = {}

    try:
        rc.ping()
        components["redis"] = "healthy"
    except Exception:
        components["redis"] = "unhealthy"

    try:
        list(bq.query("SELECT 1").result())
        components["bigquery"] = "healthy"
    except Exception:
        components["bigquery"] = "unhealthy"

    from sera.gee.availability import SENSOR_CATALOG
    try:
        import ee
        ee.Initialize(project=os.environ.get("GCP_PROJECT") or bq.project)
        components["gee"] = "healthy"
    except Exception:
        log.exception("GEE initialization failed")
        components["gee"] = "unhealthy"

    overall = "healthy" if all(v == "healthy" for v in components.values()) else "degraded"

    return {
        "status":     overall,
        "components": components,
        "gee_quota":  {
            "concurrent_tasks_used":  gee_active,
            "concurrent_tasks_limit": int(os.environ.get("GEE_MAX_CONCURRENT", "8")),
        },
    }


# ── Webhooks ──────────────────────────────────────────────────────────────────

@app.post("/v1/regions", status_code=status.HTTP_201_CREATED)
async def register_region_endpoint(config: dict) -> dict[str, str]:
    """
    Register a new monitoring region.
    Triggers async H3 cell asset upload to GEE after registration.
    First scan will wait until H3 asset is ready (~minutes for large regions).
    """
    region_id = config.get("region_id")
    if not region_id:
        raise HTTPException(status_code=400, detail="region_id is required")

    geom_cfg  = config.get("geometry", {})
    geom_json = {"type": geom_cfg["type"], "coordinates": geom_cfg["coordinates"]}

    # Persist to Postgres
    from sera.storage.postgis import register_region
    register_region(
        region_id  = region_id,
        config     = config,
        geom_wkt   = _geojson_to_wkt(geom_json),
        buffer_wkt = None,
    )

    # Async: upload H3 cells as GEE asset (required before first scan can run)
    h3_status = "uploading"
    try:
        from sera.tasks.baseline_tasks import upload_h3_asset_for_region
        upload_h3_asset_for_region.delay(
            region_id        = region_id,
            region_geom_json = geom_json,
        )
    except Exception as exc:
        # Celery broker unavailable — region is registered; H3 upload needs a worker running
        log.warning("H3 asset upload dispatch failed for %s: %s", region_id, exc)
        h3_status = "pending_worker"

    return {"region_id": region_id, "status": "registered", "h3_asset": h3_status}


@app.post("/v1/webhooks", status_code=status.HTTP_201_CREATED)
async def register_webhook(req: WebhookRegisterRequest) -> dict[str, str]:
    from sera.storage.postgis import create_webhook
    webhook_id = create_webhook(
        url        = req.url,
        secret     = req.secret,
        events     = req.events,
        region_ids = req.region_ids,
        active     = req.active,
    )
    return {"webhook_id": webhook_id, "status": "active"}


@app.delete("/v1/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(webhook_id: str) -> None:
    from sera.storage.postgis import delete_webhook as pg_delete
    pg_delete(webhook_id)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_scan_status(bq: bigquery.Client, scan_id: str) -> dict | None:
    query = f"""
    SELECT * FROM `{BQ_DATASET}.scan_log`
    WHERE scan_id = @scan_id LIMIT 1
    """
    rows = list(bq.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("scan_id", "STRING", scan_id)]
        )
    ).result())
    return dict(rows[0]) if rows else None


def _load_region_config(bq: bigquery.Client, region_id: str) -> dict | None:
    import yaml
    query = f"""
    SELECT config_yaml FROM `{BQ_DATASET}.monitoring_regions_view`
    WHERE region_id = @region_id AND active = TRUE LIMIT 1
    """
    rows = list(bq.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("region_id", "STRING", region_id)]
        )
    ).result())
    if not rows:
        return None
    return yaml.safe_load(rows[0]["config_yaml"])


def _create_scan_record(bq: bigquery.Client, scan_id: str, region_id: str, scan_date: date) -> None:
    bq.insert_rows_json(f"{BQ_DATASET}.scan_log", [{
        "scan_id":           scan_id,
        "region_id":         region_id,
        "scan_date":         scan_date.isoformat(),
        "triggered_by":      "api",
        "status":            "pending",
        "indices_requested": [],
        "indices_computed":  [],
        "indices_gap":       [],
        "created_at":        datetime.now(timezone.utc).isoformat(),
        "updated_at":        datetime.now(timezone.utc).isoformat(),
    }])


def _load_risk_events(bq: bigquery.Client, scan_id: str, min_tier: str) -> list[dict]:
    tiers = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "NONE": 0}
    min_val = tiers.get(min_tier, 1)
    tier_filter = [t for t, v in tiers.items() if v >= min_val]

    query = f"""
    SELECT * FROM `{BQ_DATASET}.risk_events`
    WHERE scan_id = @scan_id
      AND risk_tier IN UNNEST(@tiers)
    ORDER BY composite_score DESC
    """
    rows = list(bq.query(
        query,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("scan_id", "STRING", scan_id),
            bigquery.ArrayQueryParameter("tiers",   "STRING", tier_filter),
        ])
    ).result())
    return [dict(r) for r in rows]


def _geojson_to_wkt(geom_json: dict) -> str:
    """Minimal GeoJSON Polygon → WKT without shapely dependency at import time."""
    coords = geom_json["coordinates"][0]
    ring   = ", ".join(f"{lon} {lat}" for lon, lat in coords)
    return f"POLYGON(({ring}))"


def _count_by_tier(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for e in events:
        tier = e.get("risk_tier", "LOW")
        counts[tier] = counts.get(tier, 0) + 1
    return counts

