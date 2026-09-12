# SERA — Google Earth Engine Integration
`v1.0 | 2026-09-07`

---

## Sensor Catalog

| Sensor ID | Collection | Resolution | Revisit | Bands used | Cloud-sensitive |
|---|---|---|---|---|---|
| `S2_SR` | `COPERNICUS/S2_SR_HARMONIZED` | 10m | ~5 days | B2,B3,B4,B5,B8,B8A,B11,B12 | Yes |
| `L8_T1` | `LANDSAT/LC08/C02/T1_L2` | 30m | 16 days | B2,B3,B4,B5,B6,B10 | Yes |
| `L9_T1` | `LANDSAT/LC09/C02/T1_L2` | 30m | 16 days | B2,B3,B4,B5,B6,B10 | Yes |
| `S1_GRD` | `COPERNICUS/S1_GRD` | 10m | ~6-12 days | VV, VH | **No** |

Landsat 8 and 9 are treated as equivalent for index calculation and may be composited together.

---

## Sensor Availability Check

Before any computation, determine which sensors have imagery over the region bbox on or near `scan_date`.

```python
def check_sensor_availability(region_geom, scan_date, window_days=5):
    """
    Returns dict: {sensor_id: {available: bool, image_count: int, 
                                best_date: str, cloud_cover_pct: float}}
    window_days: search ±window_days from scan_date for best available image
    """
    results = {}
    date_range = (
        scan_date - timedelta(days=window_days),
        scan_date + timedelta(days=1)
    )
    
    for sensor_id, collection_id in SENSOR_CATALOG.items():
        collection = (ee.ImageCollection(collection_id)
                        .filterBounds(region_geom)
                        .filterDate(*date_range))
        
        count = collection.size().getInfo()
        if count == 0:
            results[sensor_id] = {"available": False}
            continue
        
        # Pick least-cloudy image within window
        best = collection.sort("CLOUDY_PIXEL_PERCENTAGE").first()
        cloud_cover = best.get("CLOUDY_PIXEL_PERCENTAGE").getInfo()
        
        results[sensor_id] = {
            "available": True,
            "image_count": count,
            "cloud_cover_pct": cloud_cover / 100,
            "best_image_date": best.date().format("YYYY-MM-dd").getInfo()
        }
    
    return results
```

---

## Index Eligibility Filter

Given sensor availability, determine which configured indices can be computed.

```python
def compute_index_eligibility(sensor_availability, region_config):
    """
    Returns:
      computable: list of (index_id, sensor_id) pairs
      sar_fallback: list of (index_id, sar_proxy_id) pairs  
      gap: list of (index_id, reason) pairs
    """
    computable, sar_fallback, gap = [], [], []
    cloud_threshold = region_config.get("cloud_cover_threshold", 0.70)
    
    for index_cfg in region_config["indices"]:
        if not index_cfg["enabled"]:
            continue
        
        index_id = index_cfg["id"]
        index_def = INDEX_REGISTRY[index_id]
        primary = index_def["sensors"]["primary"]
        fallback = index_def["sensors"].get("fallback")
        sar_proxy = index_def["sensors"].get("sar_proxy")
        
        primary_avail = sensor_availability.get(primary, {})
        
        # Try primary sensor
        if (primary_avail.get("available") and 
                primary_avail.get("cloud_cover_pct", 1.0) < cloud_threshold):
            computable.append((index_id, primary))
            continue
        
        # Try optical fallback (e.g. Landsat when S2 cloudy)
        if fallback:
            fallback_avail = sensor_availability.get(fallback, {})
            if (fallback_avail.get("available") and
                    fallback_avail.get("cloud_cover_pct", 1.0) < cloud_threshold):
                computable.append((index_id, fallback))
                continue
        
        # Try SAR proxy (all-weather)
        if sar_proxy and sensor_availability.get("S1_GRD", {}).get("available"):
            sar_fallback.append((index_id, sar_proxy))
            continue
        
        # Record gap
        reason = "cloud_cover" if primary_avail.get("available") else "sensor_unavailable"
        gap.append((index_id, reason))
    
    return computable, sar_fallback, gap
```

---

## Spectral Index Computation (GEE server-side)

All computation runs server-side in GEE. Formulas are parameterised from the index registry — no hardcoded band combinations in application code.

```python
def compute_index_on_image(image, index_id, sensor_id):
    """
    Returns single-band image named after index_id.
    Band names resolved from index_registry at runtime.
    """
    index_def = INDEX_REGISTRY[index_id]
    bands = index_def["bands"][sensor_id]
    formula = index_def["formula_gee"]  # GEE expression string
    
    # Apply scale factors (Sentinel-2 SR: divide by 10000)
    image = apply_scale_factors(image, sensor_id)
    
    # Apply cloud mask
    image = apply_cloud_mask(image, sensor_id)
    
    # Compute index
    return image.expression(formula, {b: image.select(b) for b in bands}) \
                .rename(index_id) \
                .clip(region_geom)


def apply_cloud_mask(image, sensor_id):
    if sensor_id in ("S2_SR",):
        qa = image.select("QA60")
        cloud_bit = 1 << 10
        cirrus_bit = 1 << 11
        mask = qa.bitwiseAnd(cloud_bit).eq(0).And(qa.bitwiseAnd(cirrus_bit).eq(0))
        return image.updateMask(mask)
    
    if sensor_id in ("L8_T1", "L9_T1"):
        qa = image.select("QA_PIXEL")
        mask = qa.bitwiseAnd(1 << 3).eq(0).And(qa.bitwiseAnd(1 << 4).eq(0))
        return image.updateMask(mask)
    
    return image  # S1_GRD: no cloud mask needed
```

---

## GEE Export Pipeline

```python
def export_index_to_gcs(image, index_id, scan_id, region_id, region_geom):
    """
    Server-side export to Cloud Storage as GeoTIFF.
    Returns GEE task ID for tracking.
    """
    export_path = f"sera/rasters/{region_id}/{scan_id}/{index_id}"
    
    task = ee.batch.Export.image.toCloudStorage(
        image=image.select(index_id),
        description=f"sera-{scan_id}-{index_id}",
        bucket=GCS_BUCKET,
        fileNamePrefix=export_path,
        region=region_geom,
        scale=10,                      # 10m for S2, 30m for Landsat — match to sensor
        crs="EPSG:4326",
        fileFormat="GeoTIFF",
        maxPixels=1e13
    )
    task.start()
    return task.id
```

**GEE task polling** is handled by a dedicated Celery beat task (`poll_gee_tasks`) that runs every 60 seconds and updates `scan_log.gee_task_ids` with final status.

---

## H3 Tessellation (post-export)

After GeoTIFF lands in Cloud Storage, a Celery worker reads it and aggregates pixel values to H3 cells:

```python
import h3
import rasterio
import numpy as np

def tessellate_raster_to_h3(gcs_path, index_id, scan_id, region_id, h3_resolution=8):
    """
    Reads GeoTIFF from GCS, aggregates valid pixels to H3 cells.
    Returns list of dicts for BQ load.
    """
    with rasterio.open(f"gs://{GCS_BUCKET}/{gcs_path}") as src:
        data = src.read(1)
        transform = src.transform
        nodata = src.nodata
    
    rows = []
    for row_idx in range(data.shape[0]):
        for col_idx in range(data.shape[1]):
            value = data[row_idx, col_idx]
            if value == nodata or np.isnan(value):
                continue
            
            lon, lat = rasterio.transform.xy(transform, row_idx, col_idx)
            cell = h3.latlng_to_cell(lat, lon, h3_resolution)
            rows.append({"cell": cell, "value": float(value)})
    
    # Aggregate by H3 cell
    from collections import defaultdict
    cell_values = defaultdict(list)
    for r in rows:
        cell_values[r["cell"]].append(r["value"])
    
    return [
        {
            "scan_id": scan_id,
            "region_id": region_id,
            "h3_cell": cell,
            "index_id": index_id,
            "index_value": float(np.mean(vals)),
            "pixel_count": len(vals),
            "coverage_pct": len(vals) / expected_pixel_count(cell, h3_resolution)
        }
        for cell, vals in cell_values.items()
    ]
```

---

## GEE Quota Management

GEE imposes concurrent task limits per project. SERA tracks quota in Redis:

```python
GEE_CONCURRENT_TASK_LIMIT = 10   # configurable per GCP project

def acquire_gee_slot(scan_id, timeout_seconds=300):
    """Blocking acquire with timeout. Returns True if slot acquired."""
    return redis_client.set(
        f"gee:slot:{scan_id}",
        1,
        nx=True,
        ex=timeout_seconds
    )

def release_gee_slot(scan_id):
    redis_client.delete(f"gee:slot:{scan_id}")
```

If no slot is available within timeout:
- Log `gee_quota_exceeded` to `data_gap_log` for all pending indices
- Mark scan `partial`
- Schedule retry in next available window

All GEE exports carry a `sera-scan-id` property for cross-referencing task IDs in GEE console.
