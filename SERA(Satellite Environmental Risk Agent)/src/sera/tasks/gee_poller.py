"""
GEE task poller — the missing link in the pipeline.

Runs every 60 seconds via Celery beat.
For each scan with status='ingesting':
  - Check GEE task status for all tracked task IDs
  - COMPLETED: trigger BQ load → on all done, trigger agent chain
  - FAILED: log data gap, decrement slot
  - If all tasks settled (complete or failed): evaluate whether enough
    indices were computed to proceed with agents or mark as partial/failed
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import ee
import redis as redis_lib
from celery import shared_task
from google.cloud import bigquery

from sera.storage.bigquery import update_scan_log

log = logging.getLogger(__name__)

BQ_DATASET = os.environ.get("BQ_DATASET", "sera_analytics_dev")
GCS_BUCKET = os.environ.get("GCS_BUCKET", "sera-rasters-dev")
ENV        = os.environ.get("ENV", "dev")

GEE_SLOT_KEY      = f"sera:gee:active_tasks:{ENV}"
GEE_SLOT_EXPIRY_S = 3600   # 1 hour — slots auto-expire to recover from worker crashes

# GEE task terminal states
GEE_DONE   = {"COMPLETED"}
GEE_FAILED = {"FAILED", "CANCELLED", "CANCEL_REQUESTED"}
GEE_ACTIVE = {"READY", "RUNNING"}


def _bq() -> bigquery.Client:
    return bigquery.Client()


def _redis() -> redis_lib.Redis:
    return redis_lib.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))


@shared_task(name="sera.tasks.gee_poller.poll_active_tasks")
def poll_active_tasks() -> dict[str, Any]:
    """
    Beat task: check all in-flight GEE tasks, trigger downstream on completion.
    Returns summary for monitoring.
    """
    ee.Initialize(project=os.environ.get("GCP_PROJECT", "august-505217"))
    bq = _bq()
    rc = _redis()

    active_scans = _load_active_scans(bq)
    if not active_scans:
        return {"checked": 0, "completed": 0, "failed": 0}

    # Batch-fetch all GEE task statuses in one API call
    all_task_ids = [tid for scan in active_scans for tid in scan["gee_task_ids"]]
    if not all_task_ids:
        return {"checked": 0, "completed": 0, "failed": 0}

    gee_statuses = _batch_task_status(all_task_ids)

    stats = {"checked": len(active_scans), "completed": 0, "failed": 0, "still_running": 0}

    for scan in active_scans:
        scan_id   = scan["scan_id"]
        region_id = scan["region_id"]

        task_states: dict[str, str] = {}    # {task_id: state}
        for task_id in scan["gee_task_ids"]:
            task_states[task_id] = gee_statuses.get(task_id, "UNKNOWN")

        completed_tasks = [t for t, s in task_states.items() if s in GEE_DONE]
        failed_tasks    = [t for t, s in task_states.items() if s in GEE_FAILED]
        running_tasks   = [t for t, s in task_states.items() if s in GEE_ACTIVE]

        # Trigger BQ load for newly completed tasks
        for task_id in completed_tasks:
            if not _already_loaded(rc, scan_id, task_id):
                index_id = _resolve_index_from_task(bq, scan_id, task_id)
                if index_id:
                    _trigger_bq_load(scan_id, region_id, index_id, task_id, rc)
                    stats["completed"] += 1

        # Log failures as data gaps
        for task_id in failed_tasks:
            if not _already_logged_failure(rc, scan_id, task_id):
                index_id = _resolve_index_from_task(bq, scan_id, task_id)
                _handle_gee_failure(bq, rc, scan_id, region_id, index_id, task_id)
                stats["failed"] += 1

        stats["still_running"] += len(running_tasks)

        # All GEE tasks settled — wait for async BQ loads to complete before evaluating
        if not running_tasks:
            dispatched = [
                tid for tid in scan["gee_task_ids"]
                if rc.exists(f"sera:gee:loaded:{scan_id}:{tid}")
            ]
            bq_done_indices = [
                k.decode().split(":")[-1]
                for k in rc.keys(f"sera:gee:bq_done:{scan_id}:*")
            ]
            if len(bq_done_indices) < len(dispatched):
                log.info(
                    "scan=%s waiting for BQ loads: %d/%d done",
                    scan_id, len(bq_done_indices), len(dispatched),
                )
            else:
                _evaluate_scan_completion(bq, rc, scan_id, region_id, scan)

    return stats


def _load_active_scans(bq: bigquery.Client) -> list[dict]:
    query = f"""
    SELECT scan_id, region_id, gee_task_ids, indices_computed, indices_gap,
           indices_requested, created_at
    FROM `{BQ_DATASET}.scan_log`
    WHERE status = 'ingesting'
      AND ARRAY_LENGTH(gee_task_ids) > 0
      AND DATE(created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY)
    """
    rows = list(bq.query(query).result())
    return [dict(r) for r in rows]


def _batch_task_status(task_ids: list[str]) -> dict[str, str]:
    """
    Fetch GEE task statuses. GEE doesn't have a true batch API,
    but ee.data.getTaskList() returns all tasks — filter to our IDs.
    For production with many tasks, use ee.data.getOperation() per task in parallel threads.
    """
    try:
        all_tasks = ee.data.getTaskList()
        return {
            t["id"]: t["state"]
            for t in all_tasks
            if t["id"] in set(task_ids)
        }
    except Exception:
        log.exception("Failed to fetch GEE task list")
        return {}


def _already_loaded(rc: redis_lib.Redis, scan_id: str, task_id: str) -> bool:
    key = f"sera:gee:loaded:{scan_id}:{task_id}"
    return bool(rc.exists(key))


def _already_logged_failure(rc: redis_lib.Redis, scan_id: str, task_id: str) -> bool:
    key = f"sera:gee:failed:{scan_id}:{task_id}"
    return bool(rc.exists(key))


def _resolve_index_from_task(bq: bigquery.Client, scan_id: str, task_id: str) -> str | None:
    """
    Map a GEE task_id back to its index_id.
    We encode this in the task description: "sera-{env}-{scan_id}-{index_id}"
    """
    try:
        task_info = ee.data.getTaskStatus([task_id])
        if task_info:
            desc = task_info[0].get("description", "")
            # Format: sera-{env}-{scan_id}-{index_id}
            parts = desc.split("-")
            if len(parts) >= 4 and desc.startswith(f"sera-{ENV}-"):
                return parts[-1]   # last segment is index_id
    except Exception:
        log.warning("Could not resolve index_id for task %s", task_id)
    return None


def _trigger_bq_load(
    scan_id: str,
    region_id: str,
    index_id: str,
    task_id: str,
    rc: redis_lib.Redis,
) -> None:
    from sera.tasks.ingest import load_completed_gee_export

    gcs_prefix = f"sera/h3/{region_id}/{scan_id}/{index_id}"
    load_completed_gee_export.delay(
        scan_id   = scan_id,
        region_id = region_id,
        index_id  = index_id,
        gcs_prefix= gcs_prefix,
    )
    # Mark as dispatched (idempotency guard — 24h TTL)
    rc.setex(f"sera:gee:loaded:{scan_id}:{task_id}", 86400, "1")
    log.info("BQ load triggered scan=%s index=%s task=%s", scan_id, index_id, task_id)


def _handle_gee_failure(
    bq: bigquery.Client,
    rc: redis_lib.Redis,
    scan_id: str,
    region_id: str,
    index_id: str | None,
    task_id: str,
) -> None:
    rc.setex(f"sera:gee:failed:{scan_id}:{task_id}", 86400, "1")

    # Release the GEE slot
    _release_slot(rc)

    if not index_id:
        log.error("GEE task %s failed but could not resolve index_id for scan=%s", task_id, scan_id)
        return

    # Log as data gap
    bq.insert_rows_json(f"{BQ_DATASET}.data_gap_log", [{
        "scan_id":               scan_id,
        "region_id":             region_id,
        "index_id":              index_id,
        "gap_date":              datetime.now(timezone.utc).date().isoformat(),
        "reason":                "gee_export_failed",
        "sar_fallback_attempted": False,
        "sar_fallback_success":   False,
        "created_at":            datetime.now(timezone.utc).isoformat(),
    }])
    log.error("GEE task %s failed → gap logged scan=%s index=%s", task_id, scan_id, index_id)


def _evaluate_scan_completion(
    bq: bigquery.Client,
    rc: redis_lib.Redis,
    scan_id: str,
    region_id: str,
    scan: dict,
) -> None:
    """
    All GEE tasks for this scan have settled.
    Check how many indices were successfully loaded, then trigger agents or mark as partial/failed.
    """
    loaded_indices = _count_loaded_indices(bq, scan_id)
    total_requested = len(scan.get("indices_requested") or [])
    min_required = int(rc.get(f"sera:region:min_indices:{region_id}") or 2)

    if loaded_indices == 0:
        update_scan_log(bq, BQ_DATASET, scan_id, region_id, {
            "status":         "failed",
            "failure_reason": "no_indices_loaded",
        })
        log.error("Scan %s failed: no indices loaded", scan_id)
        return

    status = "complete" if loaded_indices >= (total_requested or 1) else "partial"
    update_scan_log(bq, BQ_DATASET, scan_id, region_id, {"status": "reasoning"})

    if loaded_indices < min_required:
        update_scan_log(bq, BQ_DATASET, scan_id, region_id, {
            "status":         "partial",
            "failure_reason": f"only {loaded_indices} indices loaded, minimum {min_required}",
        })
        log.warning(
            "Scan %s: only %d indices loaded (min=%d), skipping agents",
            scan_id, loaded_indices, min_required,
        )
        return

    # Trigger agent chain
    _trigger_agent_chain(scan_id, region_id)
    log.info("Agent chain triggered for scan=%s (%d indices loaded)", scan_id, loaded_indices)


def _count_loaded_indices(bq: bigquery.Client, scan_id: str) -> int:
    query = f"""
    SELECT COUNT(DISTINCT index_id) AS cnt
    FROM `{BQ_DATASET}.index_values`
    WHERE scan_id = @scan_id
    """
    rows = list(bq.query(
        query,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("scan_id", "STRING", scan_id)
        ])
    ).result())
    return rows[0]["cnt"] if rows else 0


def _trigger_agent_chain(scan_id: str, region_id: str) -> None:
    from sera.tasks.agent_tasks import run_agent_chain
    run_agent_chain.delay(scan_id=scan_id, region_id=region_id)


def _release_slot(rc: redis_lib.Redis) -> None:
    current = int(rc.get(GEE_SLOT_KEY) or 0)
    if current > 0:
        rc.decr(GEE_SLOT_KEY)
    # Reset expiry to prevent permanent lock on unexpected states
    rc.expire(GEE_SLOT_KEY, GEE_SLOT_EXPIRY_S)


