# SERA — Agent Subsystem Design
`v1.0 | 2026-09-07`

Framework: **Google Agent Development Kit (ADK) only.**
LangChain is not used. ADK provides orchestration, tool routing, and state management.

---

## Design Constraints

- Agents are reasoning engines, not I/O handlers. All BigQuery reads/writes and GEE calls are executed by Celery workers before agents are invoked.
- Agents receive structured inputs from BQ tables. They never query raw rasters.
- Agent state is persisted in PostgreSQL (`agent_runs` table) so partial failures can resume without re-running completed stages.
- The full agent chain runs per scan, not per region — one ADK session per `scan_id`.

---

## Trigger Mechanism

Celery ingestion worker signals completion by posting to an internal FastAPI webhook:

```
POST /internal/agents/trigger
{
  "scan_id": "scan_20260907_emea-energy-north",
  "region_id": "emea-energy-grid-north",
  "indices_computed": ["NDVI", "LST", "NDRE"],
  "indices_gap": ["BSI"]
}
```

FastAPI enqueues an ADK session via Celery (separate agent worker pool — not the same Celery pool as ingestion, to avoid resource contention).

---

## Agent 1 — Anomaly Detection Agent

**Purpose:** Flag index-level anomalies per H3 cell by comparing current values against precomputed baselines.

**Inputs (read from BQ by Celery before invocation):**
```json
{
  "scan_id": "...",
  "region_id": "...",
  "week_of_year": 36,
  "current_values": [
    {
      "h3_cell": "88283082b5fffff",
      "index_id": "NDVI",
      "sensor_id": "S2_SR",
      "index_value": 0.21,
      "coverage_pct": 0.94
    }
  ],
  "baselines": [
    {
      "h3_cell": "88283082b5fffff",
      "index_id": "NDVI",
      "p50": 0.61, "stddev": 0.12, "p10": 0.21, "p90": 0.78
    }
  ],
  "data_gaps": [
    { "index_id": "BSI", "reason": "cloud_cover" }
  ]
}
```

**Reasoning steps:**
1. For each `(h3_cell, index_id)`: compute z-score = `(current - p50) / stddev`
2. Flag anomaly if `|z_score| > threshold` (from region config)
3. Assess direction: negative z (decline) vs. positive z (spike) — direction matters for index interpretation
4. For data-gap indices: mark as `"status": "gap"` — do not infer a z-score
5. Apply `coverage_pct` filter: cells with `< 0.5` valid pixel coverage are flagged as low-confidence

**Output (structured JSON written to agent state):**
```json
{
  "anomalies": [
    {
      "h3_cell": "88283082b5fffff",
      "index_id": "NDVI",
      "z_score": -2.3,
      "current_value": 0.21,
      "baseline_p50": 0.61,
      "anomaly": true,
      "direction": "decline",
      "confidence": "high",       // based on coverage_pct
      "interpretation": "Significant vegetation decline — below 10th percentile baseline."
    }
  ],
  "gap_flags": [
    { "index_id": "BSI", "affected_cells": ["88283082b5fffff"], "reason": "cloud_cover" }
  ]
}
```

---

## Agent 2 — Risk Evaluation Agent

**Purpose:** Aggregate cell-level anomalies into asset-level composite risk scores, incorporating data-gap penalties and asset criticality.

**Inputs:**
- Anomaly Detection Agent output
- Asset overlay: which assets intersect which H3 cells (precomputed by Celery via PostGIS query)
- Region config: index weights, fusion method, data-gap penalty, criticality scores per asset

**Reasoning steps:**
1. For each asset, collect all H3 cells that intersect its boundary
2. For each intersecting cell, compute weighted anomaly signal:
   ```
   index_signal(i) = weight(i) * clamp(|z_score(i)| / threshold(i), 0, 1)
                     if anomaly else 0
   ```
3. Apply fusion method (default: `weighted_sum`):
   ```
   raw_score = Σ index_signal(i) for i in computed_indices
   ```
4. Apply data-gap penalty:
   ```
   gap_count = count(gap_indices)
   penalty = gap_count * data_gap_penalty_per_index
   adjusted_score = min(raw_score + penalty, 1.0)
   ```
5. Apply asset criticality multiplier:
   ```
   final_score = min(adjusted_score * (0.5 + 0.5 * criticality_score), 1.0)
   ```
   Assets with `criticality_score = 1.0` receive full weight; lower-criticality assets are dampened.
6. Map to risk tier:
   - `>= 0.75` → CRITICAL
   - `>= 0.50` → HIGH
   - `>= 0.25` → MEDIUM
   - `>  0.00` → LOW
   - `0.00` → NONE

**Output:**
```json
{
  "asset_risks": [
    {
      "asset_id": "ast_001",
      "composite_score": 0.74,
      "risk_tier": "HIGH",
      "raw_score": 0.71,
      "gap_penalty": 0.0,
      "criticality_multiplier": 1.0,
      "index_contributions": {
        "NDVI": { "signal": 0.415, "weight": 0.35, "z_score": -2.3 },
        "LST":  { "signal": 0.325, "weight": 0.25, "z_score": 3.1 }
      },
      "affected_h3_cells": ["88283082b5fffff"],
      "reasoning": "Drought-pattern co-signal: NDVI decline + LST spike in same cells."
    }
  ]
}
```

**Score transparency requirement:** every `composite_score` must be fully reconstructible from `index_contributions` + `gap_penalty` + `criticality_multiplier`. The audit trail is non-negotiable for regulatory output.

---

## Agent 3 — Reporting Agent

**Purpose:** Synthesise risk evaluation output into structured GeoJSON payloads and human-readable executive summaries.

**Inputs:**
- Risk Evaluation Agent output
- Asset metadata from PostGIS
- Scan metadata from `scan_log`

**Responsibilities:**
1. Build GeoJSON Feature per risk event (geometry from asset_boundaries + affected H3 cells)
2. Write structured `executive_summary` — one paragraph, factual, no hedging language
3. Populate all fields in `risk_events` schema
4. Return structured output for Celery to persist to BQ and trigger webhooks

**Executive summary template guidance (enforced by agent prompt):**
- Lead with the primary signal: index + z-score + direction
- State affected asset(s) and proximity
- State co-occurring signals if present
- End with a concrete recommended action and timeframe
- No probability language ("may", "could", "might") — state what the data shows

**Example output:**
```
Vegetation decline (NDVI z=-2.3, value 0.21 vs. baseline 0.61) and elevated 
surface temperature (LST z=+3.1, 42.1°C vs. baseline 31.4°C) detected in 3 
H3 cells directly adjacent to pipeline segment ast_001 (criticality: critical). 
Pattern is consistent with acute heat-stress drought onset. BSI not computed 
(cloud cover 87%). Recommend ground inspection of ast_001 perimeter within 72 hours.
```

---

## State Persistence

```sql
CREATE TABLE agent_runs (
  agent_run_id      TEXT        PRIMARY KEY,
  scan_id           TEXT        NOT NULL,
  region_id         TEXT        NOT NULL,
  status            TEXT        NOT NULL,   -- 'pending'|'anomaly_detection'|'risk_evaluation'|'reporting'|'complete'|'failed'
  anomaly_output    JSONB,
  risk_output       JSONB,
  report_output     JSONB,
  failure_stage     TEXT,
  failure_reason    TEXT,
  retry_count       INT         DEFAULT 0,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

On failure at any stage: retry from the failed stage only, using stored output from completed stages. Max 3 retries before marking `failed` and alerting.

---

## Failure Handling

| Failure | Behaviour |
|---|---|
| GEE export timeout (pre-agent) | Celery retries GEE task up to 3× with backoff; logs to data_gap_log if all fail |
| Anomaly Detection Agent failure | Retry up to 3×; mark scan as `partial` if exhausted |
| Risk Evaluation Agent failure | Retry up to 3×; emit `UNKNOWN` risk tier with failure note |
| Reporting Agent failure | Retry up to 3×; structured output still written; prose summary fallback to template |
| Insufficient indices (< minimum_indices_required) | Skip agent chain; write `partial` scan with explicit gap log; alert ops |

All failures emit structured logs with `scan_id`, `agent_run_id`, `stage`, `reason`.
