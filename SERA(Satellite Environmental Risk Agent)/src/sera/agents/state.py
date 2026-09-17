"""
BQ-based agent state store.

Critical fix: original spec stored agent outputs in PostgreSQL JSONB (agent_runs table).
Problem: if an ADK worker crashes mid-chain, the partial JSONB blob is either incomplete
or not committed — the retry has no clean stage to resume from.

Fix: each agent stage writes its output to a dedicated BQ staging table.
Recovery is: check which stage tables have a row for this scan_id, skip completed stages.
BQ is append-only and atomic per row — a crash before commit leaves no partial row.
Output is also inherently auditable (permanent record per scan).

BQ staging tables:
  sera_analytics.agent_anomaly_results   — output of Anomaly Detection Agent
  sera_analytics.agent_risk_results      — output of Risk Evaluation Agent
  (Reporting Agent writes directly to risk_events — no separate staging table needed)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any

from google.cloud import bigquery

log = logging.getLogger(__name__)


def _json_default(value: Any) -> str:
    """Encode BigQuery DATE/TIMESTAMP values without hiding unsupported types."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class AgentStateStore:
    def __init__(self, bq_client: bigquery.Client, dataset: str) -> None:
        self._bq   = bq_client
        self._ds   = dataset
        self._env  = os.environ.get("ENV", "dev")

    # ── Anomaly Detection Stage ───────────────────────────────────────────────

    def write_anomaly_output(self, scan_id: str, region_id: str, payload: dict[str, Any]) -> None:
        self._insert(
            table="agent_anomaly_results",
            row={
                "scan_id":    scan_id,
                "region_id":  region_id,
                "stage":      "anomaly_detection",
                "payload":    json.dumps(payload, default=_json_default, allow_nan=False),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            labels={"workflow_name": "sera-agents", "scan_id": scan_id[:63], "region_id": region_id[:63]},
        )

    def read_anomaly_output(self, scan_id: str) -> dict[str, Any] | None:
        return self._read_latest("agent_anomaly_results", scan_id)

    # ── Risk Evaluation Stage ─────────────────────────────────────────────────

    def write_risk_output(self, scan_id: str, region_id: str, payload: dict[str, Any]) -> None:
        self._insert(
            table="agent_risk_results",
            row={
                "scan_id":    scan_id,
                "region_id":  region_id,
                "stage":      "risk_evaluation",
                "payload":    json.dumps(payload, default=_json_default, allow_nan=False),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            labels={"workflow_name": "sera-agents", "scan_id": scan_id[:63], "region_id": region_id[:63]},
        )

    def read_risk_output(self, scan_id: str) -> dict[str, Any] | None:
        return self._read_latest("agent_risk_results", scan_id)

    # ── Stage Completion Check ────────────────────────────────────────────────

    def completed_stages(self, scan_id: str) -> set[str]:
        """Return set of stage names already completed for this scan_id."""
        done: set[str] = set()
        for table, stage in (
            ("agent_anomaly_results", "anomaly_detection"),
            ("agent_risk_results", "risk_evaluation"),
        ):
            # _read_latest returns only the payload, not the BQ stage column.
            if self._read_latest(table, scan_id) is not None:
                done.add(stage)
        return done

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _insert(self, table: str, row: dict, labels: dict[str, str]) -> None:
        full_table = f"{self._ds}.{table}"
        job_config = bigquery.QueryJobConfig(labels={
            **labels,
            "env": self._env,
            "asset_tier": "na",
        })
        errors = self._bq.insert_rows_json(full_table, [row])
        if errors:
            raise RuntimeError(f"BQ insert failed for {full_table}: {errors}")
        log.debug("agent state written to %s scan=%s", full_table, row.get("scan_id"))

    def _read_latest(self, table: str, scan_id: str) -> dict[str, Any] | None:
        query = f"""
        SELECT payload
        FROM `{self._ds}.{table}`
        WHERE scan_id = @scan_id
        ORDER BY created_at DESC
        LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("scan_id", "STRING", scan_id)],
            labels={
                "workflow_name": "sera-agents",
                "scan_id":       scan_id[:63],
                "region_id":     "na",
                "env":           self._env,
                "asset_tier":    "na",
            },
        )
        rows = list(self._bq.query(query, job_config=job_config).result())
        if not rows:
            return None
        return json.loads(rows[0]["payload"])

