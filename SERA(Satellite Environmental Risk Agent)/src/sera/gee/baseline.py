"""
GEE-based baseline computation.

Critical fix: the original spec computed baselines by running APPROX_QUANTILES
in BigQuery over 40 years of raw pixel data — would scan >1 billion rows and
either timeout or cost thousands of dollars.

New approach: compute per-H3-cell statistics entirely in GEE (server-side),
export a pre-reduced CSV (~weeks × cells × stats), load to BigQuery.
BigQuery only stores the result, never aggregates the raw pixels.

One GEE export task per (region, index). For 10k cells × 52 weeks this produces
a CSV of ~500k rows, exported as ~5-20 MB. No BigQuery APPROX_QUANTILES needed.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import ee

from sera.gee.availability import SENSOR_CATALOG
from sera.gee.compute import (
    _apply_cloud_mask,
    _apply_scale_factors,
    _compute_index_expression,
)

log = logging.getLogger(__name__)

BASELINE_SCALE_METERS = 100   # coarser scale for baseline aggregation (speed vs. precision tradeoff)


def build_baseline_in_gee(
    region_id: str,
    region_geom: ee.Geometry,
    index_id: str,
    sensor_id: str,
    h3_fc: ee.FeatureCollection,
    baseline_start_year: int,
    baseline_end_year: int,
    gcs_bucket: str,
    env: str,
) -> str:
    """
    Compute weekly per-H3-cell baseline statistics for one (region, index) pair
    entirely in GEE. Returns GEE task ID.

    Output CSV columns:
      h3_cell, week_of_year, mean, stddev, p10, p25, p50, p75, p90,
      sample_count, region_id, index_id, sensor_id
    """
    collection_id = SENSOR_CATALOG[sensor_id]

    full_collection = (
        ee.ImageCollection(collection_id)
        .filterBounds(region_geom)
        .filter(
            ee.Filter.calendarRange(baseline_start_year, baseline_end_year, "year")
        )
    )

    def add_index_and_week(img: ee.Image) -> ee.Image:
        img = _apply_scale_factors(img, sensor_id)
        img = _apply_cloud_mask(img, sensor_id)
        index_img = _compute_index_expression(img, index_id, sensor_id)
        week = img.date().getRelative("week", "year").add(1)   # 1-52
        return index_img.set("week_of_year", week)

    with_index = full_collection.map(add_index_and_week)

    # Compute stats per week across all years, then reduce to H3 cells
    weeks = ee.List.sequence(1, 52)

    def weekly_baseline_fc(week: ee.Number) -> ee.FeatureCollection:
        week_imgs = with_index.filter(ee.Filter.eq("week_of_year", week))

        # Per-pixel stats across all images in this week across all baseline years
        stats_image = week_imgs.reduce(
            ee.Reducer.mean()
            .combine(ee.Reducer.stdDev(), sharedInputs=True)
            .combine(ee.Reducer.percentile([10, 25, 50, 75, 90]), sharedInputs=True)
            .combine(ee.Reducer.count(), sharedInputs=True)
        )

        # Aggregate pixel stats to H3 cells
        reduced = stats_image.reduceRegions(
            collection=h3_fc,
            reducer=ee.Reducer.mean(),   # mean of per-pixel stats within cell
            scale=BASELINE_SCALE_METERS,
            crs="EPSG:4326",
        )

        return reduced.map(
            lambda f: f.set({
                "week_of_year": week,
                "region_id":    region_id,
                "index_id":     index_id,
                "sensor_id":    sensor_id,
            })
        )

    all_weeks_fc = ee.FeatureCollection(weeks.map(weekly_baseline_fc)).flatten()

    description = f"sera-baseline-{env}-{region_id}-{index_id}"
    export_prefix = f"sera/baselines/{region_id}/{index_id}/{baseline_start_year}-{baseline_end_year}"

    task = ee.batch.Export.table.toCloudStorage(
        collection=all_weeks_fc,
        description=description,
        bucket=gcs_bucket,
        fileNamePrefix=export_prefix,
        fileFormat="CSV",
        selectors=[
            "h3_cell", "week_of_year",
            # GEE reducer output band names for combined reducer
            f"{index_id}_mean",
            f"{index_id}_stdDev",
            f"{index_id}_p10", f"{index_id}_p25", f"{index_id}_p50",
            f"{index_id}_p75", f"{index_id}_p90",
            f"{index_id}_count",
            "region_id", "index_id", "sensor_id",
        ],
    )
    task.start()
    log.info(
        "Baseline GEE export started region=%s index=%s years=%s-%s task=%s",
        region_id, index_id, baseline_start_year, baseline_end_year, task.id,
    )
    return task.id


def load_baseline_csv_to_bq(
    gcs_uri: str,
    index_id: str,
    bq_client: Any,
    dataset: str,
    region_id: str,
) -> str:
    """
    Load GEE-exported baseline CSV to BigQuery baseline_spectral_stats.
    Uses WRITE_TRUNCATE per (region_id, index_id) partition — safe to re-run.
    Returns BQ job ID.
    """
    from google.cloud import bigquery

    schema = [
        bigquery.SchemaField("region_id",    "STRING",  mode="REQUIRED"),
        bigquery.SchemaField("h3_cell",      "STRING",  mode="REQUIRED"),
        bigquery.SchemaField("index_id",     "STRING",  mode="REQUIRED"),
        bigquery.SchemaField("sensor_id",    "STRING",  mode="REQUIRED"),
        bigquery.SchemaField("week_of_year", "INT64",   mode="REQUIRED"),
        bigquery.SchemaField("mean",         "FLOAT64"),
        bigquery.SchemaField("stddev",       "FLOAT64"),
        bigquery.SchemaField("p10",          "FLOAT64"),
        bigquery.SchemaField("p25",          "FLOAT64"),
        bigquery.SchemaField("p50",          "FLOAT64"),
        bigquery.SchemaField("p75",          "FLOAT64"),
        bigquery.SchemaField("p90",          "FLOAT64"),
        bigquery.SchemaField("sample_count", "INT64"),
    ]

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV,
        skip_leading_rows=1,
        autodetect=False,
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        labels={
            "workflow_name": "sera-baseline",
            "region_id":      region_id[:63],
            "scan_id":        "baseline-build",
            "env":            os.environ.get("ENV", "dev"),
            "asset_tier":     "n/a",
        },
    )

    table_ref = f"{dataset}.baseline_spectral_stats"
    job = bq_client.load_table_from_uri(gcs_uri, table_ref, job_config=job_config)
    job.result()
    log.info("Baseline BQ load complete region=%s index=%s job=%s", region_id, index_id, job.job_id)
    return job.job_id

