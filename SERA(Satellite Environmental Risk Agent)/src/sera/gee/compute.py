"""
GEE server-side H3 reduction.

Critical fix: replaces the GeoTIFF export → rasterio → H3 tessellation pipeline.

Old approach (wasteful):
  GEE → GeoTIFF (GBs) → Cloud Storage → rasterio read → H3 aggregation → BQ

New approach:
  GEE → reduceRegions(H3 cells FC) → CSV (MBs) → Cloud Storage → BQ load

Eliminates: full raster download, rasterio OOM risk, raster-to-vector post-processing.
GEE task quota impact: same 1 task per index, but export size drops from GBs to ~1-5 MB.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import ee
import h3

log = logging.getLogger(__name__)

SCALE_METERS: dict[str, int] = {
    "S2_SR":  10,
    "L8_T1":  30,
    "L9_T1":  30,
    "S1_GRD": 10,
}

H3_RESOLUTION = 8   # ~0.74 km² per cell


def region_geojson_to_h3_fc(region_geojson: dict, resolution: int = H3_RESOLUTION) -> ee.FeatureCollection:
    """
    Convert a GeoJSON Polygon to a GEE FeatureCollection of H3 hex cells.
    Each feature carries the h3_cell property.

    For large regions (>50k cells), pre-upload as a GEE Asset once on registration
    and load via load_h3_asset() — don't call this per scan.
    """
    cells = h3.polyfill_geojson(region_geojson, resolution)
    features = []
    for cell in cells:
        # h3.cell_to_boundary returns (lat, lon) tuples; GeoJSON wants [lon, lat]
        boundary = [(lon, lat) for lat, lon in h3.cell_to_boundary(cell)]
        boundary.append(boundary[0])  # close ring
        features.append(
            ee.Feature(ee.Geometry.Polygon([boundary]), {"h3_cell": cell})
        )
    return ee.FeatureCollection(features)


def upload_h3_cells_as_asset(
    h3_fc: ee.FeatureCollection,
    region_id: str,
    env: str,
) -> str:
    """
    Persist H3 cells as a GEE Asset (once per region registration).
    Returns asset_id.
    """
    asset_id = f"projects/sera-gee-{env}/assets/regions/{region_id}/h3_cells"
    task = ee.batch.Export.table.toAsset(
        collection=h3_fc,
        description=f"sera-h3cells-{region_id}",
        assetId=asset_id,
    )
    task.start()
    log.info("H3 asset upload started for region %s: task %s", region_id, task.id)
    return asset_id


def load_h3_asset(region_id: str, env: str) -> ee.FeatureCollection:
    asset_id = f"projects/sera-gee-{env}/assets/regions/{region_id}/h3_cells"
    return ee.FeatureCollection(asset_id)


def _apply_scale_factors(image: ee.Image, sensor_id: str) -> ee.Image:
    if sensor_id in ("S2_SR",):
        optical = image.select("B.*").multiply(0.0001)
        return image.addBands(optical, overwrite=True)
    if sensor_id in ("L8_T1", "L9_T1"):
        optical = image.select("SR_B.").multiply(0.0000275).add(-0.2)
        return image.addBands(optical, overwrite=True)
    return image


def _apply_cloud_mask(image: ee.Image, sensor_id: str) -> ee.Image:
    if sensor_id == "S2_SR":
        qa = image.select("QA60")
        cloud   = 1 << 10
        cirrus  = 1 << 11
        mask = qa.bitwiseAnd(cloud).eq(0).And(qa.bitwiseAnd(cirrus).eq(0))
        return image.updateMask(mask)
    if sensor_id in ("L8_T1", "L9_T1"):
        qa = image.select("QA_PIXEL")
        mask = qa.bitwiseAnd(1 << 3).eq(0).And(qa.bitwiseAnd(1 << 4).eq(0))
        return image.updateMask(mask)
    return image  # S1_GRD: no cloud mask


def _compute_index_expression(image: ee.Image, index_id: str, sensor_id: str) -> ee.Image:
    from sera.config.index_registry import get_index

    defn  = get_index(index_id)
    bands = defn["bands"].get(sensor_id)
    if not bands:
        raise ValueError(f"No band mapping for index={index_id} sensor={sensor_id}")

    formula_gee = defn["formula_gee"]
    band_map = {alias: image.select(band) for alias, band in bands.items()}
    return image.expression(formula_gee, band_map).rename(index_id)


def compute_index_and_export(
    sensor_id: str,
    image_date: str,              # "YYYY-MM-DD" — best image date from availability check
    index_id: str,
    scan_id: str,
    region_id: str,
    region_geom: ee.Geometry,
    h3_fc: ee.FeatureCollection,  # from load_h3_asset() or region_geojson_to_h3_fc()
    gcs_bucket: str,
    env: str,
) -> str:
    """
    Server-side: compute spectral index → reduceRegions to H3 cells → export CSV.
    Returns GEE task ID.

    Export output columns:
      h3_cell, mean, count, stdDev, scan_id, region_id, index_id, sensor_id, image_date
    """
    from sera.gee.availability import SENSOR_CATALOG

    collection_id = SENSOR_CATALOG[sensor_id]
    end_date = _next_day(image_date)

    image = (
        ee.ImageCollection(collection_id)
        .filterBounds(region_geom)
        .filterDate(image_date, end_date)
        .sort("system:time_start", False)
        .first()
    )
    image = _apply_scale_factors(image, sensor_id)
    image = _apply_cloud_mask(image, sensor_id)

    index_image = _compute_index_expression(image, index_id, sensor_id)

    reduced = index_image.reduceRegions(
        collection=h3_fc,
        reducer=(
            ee.Reducer.mean()
            .combine(ee.Reducer.count(), sharedInputs=True)
            .combine(ee.Reducer.stdDev(), sharedInputs=True)
        ),
        scale=SCALE_METERS[sensor_id],
        crs="EPSG:4326",
    )

    # Tag each feature with scan metadata
    reduced = reduced.map(
        lambda f: f.set({
            "scan_id":    scan_id,
            "region_id":  region_id,
            "index_id":   index_id,
            "sensor_id":  sensor_id,
            "image_date": image_date,
        })
    )

    # Encode scan metadata in description (GEE tasks don't support custom labels)
    description = f"sera-{env}-{scan_id}-{index_id}"   # max 100 chars

    export_prefix = f"sera/h3/{region_id}/{scan_id}/{index_id}"

    task = ee.batch.Export.table.toCloudStorage(
        collection=reduced,
        description=description,
        bucket=gcs_bucket,
        fileNamePrefix=export_prefix,
        fileFormat="CSV",
        selectors=[
            "h3_cell", "mean", "count", "stdDev",
            "scan_id", "region_id", "index_id", "sensor_id", "image_date",
        ],
    )
    task.start()
    log.info(
        "GEE export started scan=%s index=%s sensor=%s task=%s",
        scan_id, index_id, sensor_id, task.id,
    )
    return task.id


def _next_day(date_str: str) -> str:
    from datetime import date, timedelta
    d = date.fromisoformat(date_str)
    return (d + timedelta(days=1)).isoformat()


def load_h3_csv_to_bq(
    gcs_uri: str,
    bq_client: Any,
    dataset: str,
    scan_id: str,
    region_id: str,
) -> str:
    """
    Load the GEE-exported CSV from GCS into BigQuery index_values table.
    Returns BQ job ID.
    """
    from google.cloud import bigquery

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV,
        skip_leading_rows=1,
        autodetect=False,
        schema=[
            bigquery.SchemaField("h3_cell",     "STRING",  mode="REQUIRED"),
            bigquery.SchemaField("index_value",  "FLOAT64"),   # renamed from 'mean'
            bigquery.SchemaField("pixel_count",  "INT64"),     # from 'count'
            bigquery.SchemaField("stddev",       "FLOAT64"),   # from 'stdDev'
            bigquery.SchemaField("scan_id",      "STRING",  mode="REQUIRED"),
            bigquery.SchemaField("region_id",    "STRING",  mode="REQUIRED"),
            bigquery.SchemaField("index_id",     "STRING",  mode="REQUIRED"),
            bigquery.SchemaField("sensor_id",    "STRING",  mode="REQUIRED"),
            bigquery.SchemaField("image_date",   "DATE",    mode="REQUIRED"),
        ],
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        labels={
            "workflow_name": "sera-ingest",
            "region_id":      region_id[:63],
            "scan_id":        scan_id[:63],
            "env":            os.environ.get("ENV", "dev"),
            "asset_tier":     "n/a",
        },
    )

    table_ref = f"{dataset}.index_values"
    job = bq_client.load_table_from_uri(gcs_uri, table_ref, job_config=job_config)
    job.result()   # block until done
    log.info("BQ load complete for %s → %s (job=%s)", gcs_uri, table_ref, job.job_id)
    return job.job_id
