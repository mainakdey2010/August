# SERA — Observability
`v1.0 | 2026-09-07`

---

## Structured Logging

All logs are JSON-structured (Cloud Logging compatible). Every log entry carries:

```json
{
  "severity": "INFO",
  "timestamp": "2026-09-07T08:11:23Z",
  "scan_id": "scan_20260907_emea-energy-north",
  "region_id": "emea-energy-grid-north",
  "component": "celery.ingest",
  "env": "prod",
  "message": "GEE export task started",
  "gee_task_id": "JQHX7K2AB3...",
  "index_id": "NDVI",
  "sensor_id": "S2_SR"
}
```

Standard fields on every entry: `scan_id`, `region_id`, `component`, `env`.

---

## Key Metrics (Cloud Monitoring)

### Scan throughput & latency

| Metric | Description | Alert if |
|---|---|---|
| `sera/scan/duration_seconds` | End-to-end scan duration per region | P95 > 3600s |
| `sera/scan/status` | Count by status (complete/partial/failed) | failed > 0 in 1h |
| `sera/scan/partial_rate` | % of scans completing as `partial` | > 20% in 24h |

### Data quality

| Metric | Description | Alert if |
|---|---|---|
| `sera/gap/rate` | % of index-region-date combinations with data gaps | > 40% in 7-day rolling |
| `sera/gap/sar_fallback_success` | % of gaps where SAR fallback succeeded | < 50% when SAR attempted |
| `sera/baseline/staleness_days` | Days since last baseline refresh per region | > 14 |

### GEE quota

| Metric | Description | Alert if |
|---|---|---|
| `sera/gee/concurrent_tasks` | Active GEE tasks vs. limit | > 80% of limit |
| `sera/gee/export_failures` | GEE export task failure count | > 3 in 1h |
| `sera/gee/quota_wait_seconds` | Time spent waiting for GEE slot | P95 > 300s |

### Risk events

| Metric | Description | Alert if |
|---|---|---|
| `sera/risk/critical_count` | CRITICAL events per day | any (page on-call) |
| `sera/risk/high_count` | HIGH events per day | > 10 in 24h |
| `sera/webhook/delivery_failures` | Webhook POST failures | > 0 (retry + alert) |

### Agent performance

| Metric | Description | Alert if |
|---|---|---|
| `sera/agent/duration_seconds` | ADK agent chain duration | P95 > 300s |
| `sera/agent/retry_count` | Agent retries per scan | > 2 in 1h |
| `sera/agent/failure_rate` | Agent failures / total runs | > 5% in 24h |

---

## Cost Attribution Dashboard

Primary view: BigQuery cost by `workflow_name` label, grouped by `region_id` and `env`.

```sql
-- Monthly cost by workflow and region (run against INFORMATION_SCHEMA)
SELECT
  labels.value AS workflow_name,
  r.labels.region_id,
  SUM(total_bytes_billed) / POW(1024, 4) AS tb_billed,
  SUM(total_bytes_billed) / POW(1024, 4) * 5.0 AS estimated_eur  -- $5/TB on-demand
FROM `region-asia-south1`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
CROSS JOIN UNNEST(labels) AS labels
WHERE
  labels.key = 'workflow_name'
  AND creation_time BETWEEN TIMESTAMP_TRUNC(CURRENT_TIMESTAMP(), MONTH)
                        AND CURRENT_TIMESTAMP()
GROUP BY 1, 2
ORDER BY tb_billed DESC
```

Run weekly; output to a BigQuery table consumed by the internal cost dashboard.

---

## Alerting Runbook

### CRITICAL risk event
- Fires: immediately on `risk_tier = CRITICAL` written to `risk_events`
- Channel: PagerDuty (on-call) + Slack `#sera-alerts`
- Action: do not silence until asset owner acknowledges

### Scan failed
- Fires: when `scan_log.status = failed` and `retry_count >= 3`
- Channel: Slack `#sera-ops`
- Action: check `failure_reason`, check GEE quota, check BQ job logs with `scan_id`

### GEE quota exhausted
- Fires: when `sera/gee/concurrent_tasks > 80%` for > 10 minutes
- Channel: Slack `#sera-ops`
- Action: check whether a large baseline rebuild is running and throttle it

### Baseline staleness
- Fires: when `baseline_staleness_days > 14` for any active region
- Channel: Slack `#sera-ops`
- Action: trigger manual `POST /v1/baselines/{region_id}/refresh?scope=incremental`

### High gap rate
- Fires: when rolling 7-day gap rate > 40% for any region
- Channel: Slack `#sera-data-quality`
- Action: check cloud cover patterns; consider extending `window_days` in sensor availability check
