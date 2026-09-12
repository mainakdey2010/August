"""
Agent chain task — triggered by gee_poller once all GEE exports are loaded.

Runs the three ADK agents sequentially (Anomaly → Risk → Reporting),
using BQ-backed state for crash recovery.
Each stage checks if already completed before running.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any

from celery import shared_task
from google.cloud import bigquery

from sera.agents.state import AgentStateStore
from sera.analytics.risk_score import score_asset, to_bq_row
from sera.storage.bigquery import (
    run_anomaly_detection_query,
    update_scan_log,
    write_risk_events,
)

log = logging.getLogger(__name__)

BQ_DATASET = os.environ.get("BQ_DATASET", "sera_analytics_dev")
ENV        = os.environ.get("ENV", "dev")


def _bq() -> bigquery.Client:
    return bigquery.Client()


@shared_task(
    name="sera.tasks.agent_tasks.run_agent_chain",
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),          # broad here — each stage has its own guard
    retry_backoff=True,
    retry_backoff_max=120,
    queue="agents",
)
def run_agent_chain(self, scan_id: str, region_id: str) -> dict[str, Any]:
    """
    Orchestrates the three-stage agent chain for one scan.
    Uses BQ-backed state so any stage can resume after a crash without re-running completed stages.
    """
    bq    = _bq()
    store = AgentStateStore(bq, BQ_DATASET)

    done  = store.completed_stages(scan_id)
    log.info("Agent chain starting scan=%s region=%s completed_stages=%s", scan_id, region_id, done)

    # ── Stage 1: Anomaly Detection ─────────────────────────────────────────────
    if "anomaly_detection" not in done:
        anomaly_output = _run_anomaly_detection(bq, scan_id, region_id)
        store.write_anomaly_output(scan_id, region_id, anomaly_output)
        log.info("Stage anomaly_detection complete scan=%s anomalies=%d",
                 scan_id, len(anomaly_output.get("anomalies", [])))
    else:
        anomaly_output = store.read_anomaly_output(scan_id)
        log.info("Stage anomaly_detection skipped (cached) scan=%s", scan_id)

    # ── Stage 2: Risk Evaluation ──────────────────────────────────────────────
    if "risk_evaluation" not in done:
        risk_output = _run_risk_evaluation(bq, scan_id, region_id, anomaly_output)
        store.write_risk_output(scan_id, region_id, risk_output)
        log.info("Stage risk_evaluation complete scan=%s events=%d",
                 scan_id, len(risk_output.get("asset_risks", [])))
    else:
        risk_output = store.read_risk_output(scan_id)
        log.info("Stage risk_evaluation skipped (cached) scan=%s", scan_id)

    # ── Stage 3: Reporting ────────────────────────────────────────────────────
    # Reporting always re-runs (idempotent write to risk_events; prose is cheap to regenerate)
    event_rows = _run_reporting(bq, scan_id, region_id, risk_output, anomaly_output)

    # ── Persist & notify ──────────────────────────────────────────────────────
    if event_rows:
        write_risk_events(bq, BQ_DATASET, region_id, scan_id, event_rows)
        _fire_webhooks(scan_id, region_id, event_rows)

    update_scan_log(bq, BQ_DATASET, scan_id, region_id, {"status": "complete"})
    return {"scan_id": scan_id, "events_written": len(event_rows)}


# ── Stage implementations ─────────────────────────────────────────────────────

def _run_anomaly_detection(
    bq: bigquery.Client,
    scan_id: str,
    region_id: str,
) -> dict[str, Any]:
    """
    Query BQ for z-scores; group results by H3 cell for agent input.
    No ADK call needed here — anomaly detection is deterministic SQL.
    ADK agents consume the structured output of this stage.
    """
    rows = run_anomaly_detection_query(bq, BQ_DATASET, scan_id, region_id)

    anomalies = [r for r in rows if r.get("anomaly")]
    gap_cells: dict[str, list[str]] = {}   # index_id → [h3_cells with low coverage]

    low_confidence = [
        r for r in rows
        if not r.get("anomaly") and r.get("coverage_pct", 1.0) < 0.5
    ]
    for r in low_confidence:
        gap_cells.setdefault(r["index_id"], []).append(r["h3_cell"])

    return {
        "scan_id":        scan_id,
        "region_id":      region_id,
        "anomalies":      anomalies,
        "all_rows":       rows,
        "low_confidence": low_confidence,
    }


def _run_risk_evaluation(
    bq: bigquery.Client,
    scan_id: str,
    region_id: str,
    anomaly_output: dict[str, Any],
) -> dict[str, Any]:
    """
    Resolve H3 cells → assets via PostGIS, then score each asset.
    """
    from sera.storage.postgis import resolve_h3_to_assets

    all_rows  = anomaly_output.get("all_rows", [])
    anomalies = anomaly_output.get("anomalies", [])

    # Get unique anomalous H3 cells
    anomalous_cells = list({r["h3_cell"] for r in anomalies})
    if not anomalous_cells:
        return {"scan_id": scan_id, "asset_risks": []}

    # PostGIS: H3 cells → affected assets (as-of scan date)
    scan_date_str = scan_id.split("_")[1]   # scan_20260907_... → "20260907"
    scan_date = date(int(scan_date_str[:4]), int(scan_date_str[4:6]), int(scan_date_str[6:8]))

    asset_cells = resolve_h3_to_assets(region_id, anomalous_cells, scan_date)
    # {asset_id: {"tier": str, "criticality": float, "cells": [str]}}

    region_config = _load_region_config(bq, region_id)
    index_configs = region_config.get("indices", [])
    gap_indices   = _get_gap_indices(bq, scan_id)

    asset_risks = []
    for asset_id, asset_info in asset_cells.items():
        # Filter anomaly rows to cells touching this asset
        asset_rows = [
            r for r in all_rows
            if r["h3_cell"] in set(asset_info["cells"])
        ]
        result = score_asset(
            asset_id         = asset_id,
            asset_tier       = asset_info["tier"],
            criticality_score= asset_info["criticality"],
            anomaly_rows     = asset_rows,
            index_configs    = index_configs,
            gap_indices      = gap_indices,
            affected_h3_cells= asset_info["cells"],
        )
        asset_risks.append(result)

    return {"scan_id": scan_id, "asset_risks": [r.__dict__ for r in asset_risks]}


def _run_reporting(
    bq: bigquery.Client,
    scan_id: str,
    region_id: str,
    risk_output: dict[str, Any],
    anomaly_output: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Build risk_events rows from scored assets.
    Generates executive_summary prose for non-NONE events.
    """
    from sera.analytics.risk_score import RiskTier

    event_date = _scan_date_from_id(scan_id)
    event_rows = []

    for asset_risk in risk_output.get("asset_risks", []):
        # Skip NONE tier — no event to record
        if asset_risk.get("risk_tier") == RiskTier.NONE.value:
            continue

        row = to_bq_row(
            result     = _dict_to_result(asset_risk),
            scan_id    = scan_id,
            region_id  = region_id,
            event_date = event_date,
        )
        row["executive_summary"] = _generate_executive_summary(asset_risk)
        event_rows.append(row)

    return event_rows


def _fire_webhooks(scan_id: str, region_id: str, event_rows: list[dict]) -> None:
    """Enqueue webhook delivery tasks for all events above threshold."""
    from sera.tasks.webhook_tasks import deliver_event
    for row in event_rows:
        deliver_event.delay(
            region_id  = region_id,
            risk_tier  = row["risk_tier"],
            event      = row,
        )


def _generate_executive_summary(asset_risk: dict) -> str:
    """
    Build concrete, non-hedging executive summary.
    Rules: lead with primary signal, state asset, state co-signals, end with action + timeframe.
    """
    scores = asset_risk.get("index_scores", [])
    if isinstance(scores, dict):
        scores = list(scores.values())

    anomalous = sorted(
        [s for s in scores if s.get("anomaly")],
        key=lambda x: abs(x.get("z_score", 0)), reverse=True,
    )
    if not anomalous:
        return "No anomalies detected. Routine monitoring continues."

    primary = anomalous[0]
    asset_id = asset_risk.get("asset_id", "unknown")
    tier     = asset_risk.get("risk_tier", "MEDIUM")
    gaps     = asset_risk.get("data_quality", {}).get("gap_indices", [])

    lines = [
        f"{primary['index_id']} anomaly detected "
        f"(z={primary['z_score']:+.1f}, value={primary['current_value']:.3f} "
        f"vs. baseline {primary['baseline_p50']:.3f}) adjacent to asset {asset_id}."
    ]

    co_signals = anomalous[1:]
    if co_signals:
        co_str = ", ".join(
            f"{s['index_id']} z={s['z_score']:+.1f}" for s in co_signals
        )
        lines.append(f"Co-occurring signals: {co_str}.")

    if gaps:
        lines.append(f"Indices not computed (cloud cover / sensor gap): {', '.join(gaps)}.")

    action_map = {
        "CRITICAL": "Immediate ground inspection required within 24 hours.",
        "HIGH":     "Field inspection recommended within 72 hours.",
        "MEDIUM":   "Schedule inspection within 14 days. Monitor next scan.",
        "LOW":      "Flag for next scheduled inspection cycle.",
    }
    lines.append(action_map.get(tier, "Review at next inspection cycle."))

    return " ".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_region_config(bq: bigquery.Client, region_id: str) -> dict:
    import yaml
    query = f"""
    SELECT config_yaml FROM `{BQ_DATASET}.monitoring_regions_view`
    WHERE region_id = @region_id LIMIT 1
    """
    rows = list(bq.query(
        query,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("region_id", "STRING", region_id)
        ])
    ).result())
    return yaml.safe_load(rows[0]["config_yaml"]) if rows else {}


def _get_gap_indices(bq: bigquery.Client, scan_id: str) -> list[str]:
    query = f"""
    SELECT ARRAY_AGG(DISTINCT index_id) AS gaps
    FROM `{BQ_DATASET}.data_gap_log`
    WHERE scan_id = @scan_id
    """
    rows = list(bq.query(
        query,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("scan_id", "STRING", scan_id)
        ])
    ).result())
    return rows[0]["gaps"] or [] if rows else []


def _scan_date_from_id(scan_id: str) -> str:
    """Extract YYYY-MM-DD from scan_20260907_region → 2026-09-07"""
    try:
        raw = scan_id.split("_")[1]
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    except (IndexError, ValueError):
        return datetime.now(timezone.utc).date().isoformat()


def _dict_to_result(d: dict) -> Any:
    """Reconstruct AssetRiskResult from a dict (from BQ state store)."""
    from sera.analytics.risk_score import (
        AssetRiskResult, DataQuality, IndexScore, QualityTier, RiskTier
    )
    scores = d.get("index_scores", [])
    if isinstance(scores, dict):
        scores = list(scores.values())

    dq = d.get("data_quality", {})
    return AssetRiskResult(
        asset_id          = d["asset_id"],
        asset_tier        = d.get("asset_tier", ""),
        criticality_score = d.get("criticality_score", 0.5),
        risk_score        = d.get("risk_score", 0.0),
        risk_tier         = RiskTier(d.get("risk_tier", "NONE")),
        index_scores      = [IndexScore(**s) for s in scores] if scores else [],
        data_quality      = DataQuality(
            computed_count = dq.get("computed_count", 0),
            total_count    = dq.get("total_count", 0),
            gap_indices    = dq.get("gap_indices", []),
            quality_score  = dq.get("quality_score", 1.0),
            quality_tier   = QualityTier(dq.get("quality_tier", "GOOD")),
        ),
        affected_h3_cells = d.get("affected_h3_cells", []),
        reasoning         = d.get("reasoning", ""),
    )
