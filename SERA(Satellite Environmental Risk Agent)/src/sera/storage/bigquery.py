"""
BigQuery client wrapper with mandatory label enforcement and incremental checkpoint.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

from google.cloud import bigquery

log = logging.getLogger(__name__)

REQUIRED_LABELS = frozenset({"env", "workflow_name", "region_id", "scan_id", "asset_tier"})


def build_job_config(labels: dict[str, str], **kwargs: Any) -> bigquery.QueryJobConfig:
    missing = REQUIRED_LABELS - labels.keys()
    if missing:
        raise ValueError(f"BQ job missing required labels: {missing}")
    return bigquery.QueryJobConfig(labels=labels, **kwargs)


def get_last_processed_date(
    bq_client: bigquery.Client,
    dataset: str,
    region_id: str,
    index_id: str,
    sensor_id: str,
) -> date | None:
    """
    Incremental checkpoint: returns the most recent image_date already loaded
    for this (region, index, sensor) combination.
    GEE query will then filterDate(last_date + 1 day, today).
    """
    query = f"""
    SELECT MAX(image_date) AS last_date
    FROM `{dataset}.index_values`
    WHERE region_id = @region_id
      AND index_id  = @index_id
      AND sensor_id = @sensor_id
    """
    env = os.environ.get("ENV", "dev")
    job_config = build_job_config(
        labels={
            "env":           env,
            "workflow_name": "sera-ingest",
            "region_id":     region_id[:63],
            "scan_id":       "checkpoint",
            "asset_tier":    "na",
        },
        query_parameters=[
            bigquery.ScalarQueryParameter("region_id", "STRING", region_id),
            bigquery.ScalarQueryParameter("index_id",  "STRING", index_id),
            bigquery.ScalarQueryParameter("sensor_id", "STRING", sensor_id),
        ],
    )
    rows = list(bq_client.query(query, job_config=job_config).result())
    if not rows or rows[0]["last_date"] is None:
        return None
    return rows[0]["last_date"]


def run_anomaly_detection_query(
    bq_client: bigquery.Client,
    dataset: str,
    scan_id: str,
    region_id: str,
    threshold_stddev: float = 2.0,
) -> list[dict[str, Any]]:
    """
    Join current scan index values against baseline stats, compute z-scores.
    Returns list of dicts — one per (h3_cell, index_id).
    """
    env = os.environ.get("ENV", "dev")
    query = f"""
    SELECT
      iv.h3_cell,
      iv.index_id,
      iv.sensor_id,
      iv.index_value,
      iv.coverage_pct,
      iv.image_date,
      bs.p50               AS baseline_p50,
      bs.stddev            AS baseline_stddev,
      bs.p10               AS baseline_p10,
      bs.p90               AS baseline_p90,
      SAFE_DIVIDE(iv.index_value - bs.p50, bs.stddev) AS z_score,
      ABS(SAFE_DIVIDE(iv.index_value - bs.p50, bs.stddev)) >= @threshold AS anomaly,
      CASE
        WHEN iv.index_value < bs.p50 THEN 'decline'
        WHEN iv.index_value > bs.p50 THEN 'spike'
        ELSE 'neutral'
      END AS direction
    FROM `{dataset}.index_values` iv
    JOIN `{dataset}.baseline_spectral_stats` bs
      ON  iv.region_id     = bs.region_id
      AND iv.h3_cell        = bs.h3_cell
      AND iv.index_id       = bs.index_id
      AND iv.sensor_id      = bs.sensor_id
      AND EXTRACT(WEEK FROM iv.image_date) = bs.week_of_year
    WHERE
      iv.scan_id       = @scan_id
      AND iv.coverage_pct >= 0.5
      AND bs.sample_count >= 10
    """
    job_config = build_job_config(
        labels={
            "env":           env,
            "workflow_name": "sera-agents",
            "region_id":     region_id[:63],
            "scan_id":       scan_id[:63],
            "asset_tier":    "na",
        },
        query_parameters=[
            bigquery.ScalarQueryParameter("scan_id",   "STRING", scan_id),
            bigquery.ScalarQueryParameter("threshold", "FLOAT64", threshold_stddev),
        ],
    )
    rows = list(bq_client.query(query, job_config=job_config).result())
    return [dict(r) for r in rows]


def write_risk_events(
    bq_client: bigquery.Client,
    dataset: str,
    region_id: str,
    scan_id: str,
    rows: list[dict[str, Any]],
) -> str:
    """Insert risk_events rows. Returns BQ insert status."""
    table_ref = f"{dataset}.risk_events"
    errors = bq_client.insert_rows_json(table_ref, rows)
    if errors:
        raise RuntimeError(f"risk_events insert failed: {errors}")
    log.info("Wrote %d risk events for scan=%s", len(rows), scan_id)
    return "ok"


def update_scan_log(
    bq_client: bigquery.Client,
    dataset: str,
    scan_id: str,
    region_id: str,
    updates: dict[str, Any],
) -> None:
    """
    Upsert scan_log for a given scan_id.
    WHEN MATCHED → UPDATE; WHEN NOT MATCHED → INSERT (creates the row on first call).
    Supports scalar and list/array field values.
    """
    env = os.environ.get("ENV", "dev")
    set_clauses = ", ".join(f"T.{k} = S.{k}" for k in updates)
    params_sql  = ", ".join(f"@{k} AS {k}" for k in updates)

    update_keys  = list(updates.keys())
    insert_cols  = ", ".join(["scan_id", "region_id"] + update_keys + ["created_at", "updated_at"])
    insert_vals  = ", ".join(
        ["@scan_id", "@region_id"] + [f"@{k}" for k in update_keys]
        + ["CURRENT_TIMESTAMP()", "CURRENT_TIMESTAMP()"]
    )

    query = f"""
    MERGE `{dataset}.scan_log` T
    USING (SELECT @scan_id AS scan_id, @region_id AS region_id, {params_sql}) S
    ON T.scan_id = S.scan_id
    WHEN MATCHED THEN UPDATE SET {set_clauses}, T.updated_at = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})
    """
    params = [
        bigquery.ScalarQueryParameter("scan_id",   "STRING", scan_id),
        bigquery.ScalarQueryParameter("region_id", "STRING", region_id),
    ]
    for k, v in updates.items():
        if isinstance(v, list):
            elem_type = "STRING"
            if v and isinstance(v[0], float): elem_type = "FLOAT64"
            elif v and isinstance(v[0], int):  elem_type = "INT64"
            params.append(bigquery.ArrayQueryParameter(k, elem_type, v))
        else:
            type_map = {str: "STRING", bool: "BOOL", int: "INT64", float: "FLOAT64"}
            params.append(bigquery.ScalarQueryParameter(k, type_map.get(type(v), "STRING"), v))

    job_config = build_job_config(
        labels={
            "env":           env,
            "workflow_name": "sera-scan",
            "region_id":     region_id[:63],
            "scan_id":       scan_id[:63],
            "asset_tier":    "na",
        },
        query_parameters=params,
    )
    bq_client.query(query, job_config=job_config).result()


