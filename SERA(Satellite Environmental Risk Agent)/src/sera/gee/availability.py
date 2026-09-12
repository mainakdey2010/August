"""
Sensor availability check with Redis caching.

getInfo() calls to GEE are synchronous and slow (~1-3s per sensor).
For 50 regions × 4 sensors this would add 10+ minutes of idle time.
Cache per (region_bbox, date_window_key) with 6h TTL.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, timedelta
from typing import Any

import ee
import redis

log = logging.getLogger(__name__)

SENSOR_CATALOG: dict[str, str] = {
    "S2_SR":  "COPERNICUS/S2_SR_HARMONIZED",
    "L8_T1":  "LANDSAT/LC08/C02/T1_L2",
    "L9_T1":  "LANDSAT/LC09/C02/T1_L2",
    "S1_GRD": "COPERNICUS/S1_GRD",
}

CACHE_TTL_SECONDS = 6 * 3600  # 6 hours


def _cache_key(region_bbox: list[float], scan_date: date, window_days: int) -> str:
    payload = json.dumps(
        {"bbox": region_bbox, "date": str(scan_date), "window": window_days},
        sort_keys=True,
    )
    return "sera:sensor_avail:" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def check_sensor_availability(
    region_geom: ee.Geometry,
    region_bbox: list[float],        # [west, south, east, north] for cache key
    scan_date: date,
    redis_client: redis.Redis,
    window_days: int = 5,
) -> dict[str, dict[str, Any]]:
    """
    Returns availability info per sensor_id, using Redis cache.

    {
      "S2_SR": {"available": True, "cloud_cover_pct": 0.12, "best_image_date": "2026-09-06"},
      "S1_GRD": {"available": True, "cloud_cover_pct": 0.0,  "best_image_date": "2026-09-05"},
      "L8_T1":  {"available": False},
      ...
    }
    """
    cache_key = _cache_key(region_bbox, scan_date, window_days)
    cached = redis_client.get(cache_key)
    if cached:
        log.debug("sensor_availability cache hit for %s", cache_key)
        return json.loads(cached)

    results = _query_gee(region_geom, scan_date, window_days)
    redis_client.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(results))
    return results


def _query_gee(
    region_geom: ee.Geometry,
    scan_date: date,
    window_days: int,
) -> dict[str, dict[str, Any]]:
    start = (scan_date - timedelta(days=window_days)).isoformat()
    end   = (scan_date + timedelta(days=1)).isoformat()

    # Batch: use aggregate_array to get cloud cover stats in one round-trip per sensor
    results: dict[str, dict[str, Any]] = {}

    for sensor_id, collection_id in SENSOR_CATALOG.items():
        try:
            col = (
                ee.ImageCollection(collection_id)
                .filterBounds(region_geom)
                .filterDate(start, end)
            )

            count = col.size().getInfo()
            if count == 0:
                results[sensor_id] = {"available": False}
                continue

            if sensor_id == "S1_GRD":
                # SAR has no cloud cover metadata
                best = col.sort("system:time_start", False).first()
                image_date = (
                    ee.Date(best.get("system:time_start")).format("YYYY-MM-dd").getInfo()
                )
                results[sensor_id] = {
                    "available": True,
                    "cloud_cover_pct": 0.0,
                    "best_image_date": image_date,
                }
                continue

            cloud_prop = (
                "CLOUDY_PIXEL_PERCENTAGE"
                if sensor_id == "S2_SR"
                else "CLOUD_COVER"
            )

            # Get cloud cover for all images in one getInfo call
            cloud_covers = (
                col.aggregate_array(cloud_prop).getInfo()
            )
            image_dates = (
                col.aggregate_array("system:time_start")
                   .map(lambda t: ee.Date(t).format("YYYY-MM-dd"))
                   .getInfo()
            )

            min_idx = cloud_covers.index(min(cloud_covers))
            results[sensor_id] = {
                "available": True,
                "cloud_cover_pct": cloud_covers[min_idx] / 100.0,
                "best_image_date": image_dates[min_idx],
                "image_count": count,
            }

        except Exception:
            log.exception("GEE availability check failed for %s", sensor_id)
            results[sensor_id] = {"available": False, "error": True}

    return results


def compute_index_eligibility(
    sensor_availability: dict[str, dict[str, Any]],
    region_indices: list[dict[str, Any]],       # from region config
    cloud_threshold: float = 0.70,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    """
    Returns:
      computable:   [(index_id, sensor_id), ...]  — ready to compute
      sar_fallback: [(index_id, sar_proxy_id), ...] — optical unavailable, using SAR
      gap:          [(index_id, reason), ...]      — cannot compute
    """
    from sera.config.index_registry import get_index

    computable, sar_fallback, gap = [], [], []

    for idx_cfg in region_indices:
        if not idx_cfg.get("enabled", True):
            continue

        index_id = idx_cfg["id"]
        defn = get_index(index_id)
        sensors = defn.get("sensors", {})
        primary   = sensors.get("primary")
        fallback  = sensors.get("fallback")
        sar_proxy = sensors.get("sar_proxy")

        def _usable(sid: str | None) -> bool:
            if not sid:
                return False
            av = sensor_availability.get(sid, {})
            return av.get("available", False) and av.get("cloud_cover_pct", 1.0) < cloud_threshold

        if _usable(primary):
            computable.append((index_id, primary))
        elif _usable(fallback):
            computable.append((index_id, fallback))
        elif sar_proxy and sensor_availability.get("S1_GRD", {}).get("available"):
            sar_fallback.append((index_id, sar_proxy))
        else:
            av = sensor_availability.get(primary or "", {})
            reason = (
                "cloud_cover" if av.get("available") else "sensor_unavailable"
            )
            gap.append((index_id, reason))

    return computable, sar_fallback, gap
