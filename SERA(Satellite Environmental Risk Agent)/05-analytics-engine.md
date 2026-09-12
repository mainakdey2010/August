# SERA — Analytics Engine
`v1.0 | 2026-09-07`

---

## 1. Baseline Materialisation

### Why a Materialised Baseline

Anomaly detection compares today's index values against historical norms.
Without materialisation, every scan would join against 40 years of raw pixel data —
an unbounded full-table scan at petabyte scale.

The baseline layer precomputes statistical summaries (mean, stddev, percentiles)
per `(region_id, h3_cell, index_id, sensor_id, week_of_year)` from the historical archive.
Anomaly detection then joins only against this summary — O(cells × indices) per scan,
not O(cells × indices × years × images).

### Initial Build

Run once per region on registration. This is a long-running Celery task (~hours for large regions).

```sql
-- Baseline build query (BigQuery)
-- Runs as a scheduled BQ job; result written to baseline_spectral_stats

INSERT INTO `{project}.sera_analytics.baseline_spectral_stats`
SELECT
  region_id,
  h3_cell,
  index_id,
  sensor_id,
  EXTRACT(WEEK FROM image_date)                       AS week_of_year,
  APPROX_QUANTILES(index_value, 100)[OFFSET(10)]      AS p10,
  APPROX_QUANTILES(index_value, 100)[OFFSET(25)]      AS p25,
  APPROX_QUANTILES(index_value, 100)[OFFSET(50)]      AS p50,
  APPROX_QUANTILES(index_value, 100)[OFFSET(75)]      AS p75,
  APPROX_QUANTILES(index_value, 100)[OFFSET(90)]      AS p90,
  AVG(index_value)                                    AS mean,
  STDDEV_POP(index_value)                             AS stddev,
  COUNT(*)                                            AS sample_count,
  ARRAY_AGG(DISTINCT EXTRACT(YEAR FROM image_date))   AS baseline_years,
  CURRENT_TIMESTAMP()                                 AS last_updated
FROM `{project}.sera_analytics.index_values`
WHERE
  region_id = @region_id
  AND image_date BETWEEN @baseline_start AND @baseline_end
  AND coverage_pct >= 0.5                             -- minimum pixel coverage
GROUP BY 1, 2, 3, 4, 5
```

Parameters: `baseline_start` = today - N years, `baseline_end` = yesterday.
N is set per region in config (default 10, max 40).

### Incremental Refresh (Weekly)

Each Sunday, add the most recent full week of observations without rebuilding from scratch.

```sql
-- Incremental baseline update — merge new week's stats into existing baseline

MERGE `{project}.sera_analytics.baseline_spectral_stats` AS target
USING (
  -- Compute stats for the just-completed week
  SELECT
    region_id, h3_cell, index_id, sensor_id,
    EXTRACT(WEEK FROM image_date) AS week_of_year,
    AVG(index_value) AS new_mean,
    STDDEV_POP(index_value) AS new_stddev,
    COUNT(*) AS new_count,
    ARRAY_AGG(DISTINCT EXTRACT(YEAR FROM image_date)) AS new_years
  FROM `{project}.sera_analytics.index_values`
  WHERE
    region_id = @region_id
    AND image_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 8 DAY)
                       AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
  GROUP BY 1, 2, 3, 4, 5
) AS source
ON (
  target.region_id   = source.region_id   AND
  target.h3_cell     = source.h3_cell     AND
  target.index_id    = source.index_id    AND
  target.sensor_id   = source.sensor_id  AND
  target.week_of_year = source.week_of_year
)
WHEN MATCHED THEN UPDATE SET
  -- Combine existing and new stats using online variance update formula
  mean        = (target.mean * target.sample_count + source.new_mean * source.new_count) 
                / (target.sample_count + source.new_count),
  stddev      = -- Welford's combined variance (computed in application layer, passed as param)
  sample_count = target.sample_count + source.new_count,
  baseline_years = ARRAY_CONCAT(target.baseline_years, source.new_years),
  last_updated = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (
  region_id, h3_cell, index_id, sensor_id, week_of_year,
  mean, stddev, sample_count, baseline_years, last_updated
) VALUES (
  source.region_id, source.h3_cell, source.index_id, source.sensor_id, source.week_of_year,
  source.new_mean, source.new_stddev, source.new_count, source.new_years, CURRENT_TIMESTAMP()
)
```

**Note on `stddev` update:** the Welford online algorithm for combining variance from two populations
is computed in Python before the MERGE (not in SQL) and passed as a parameter `@combined_stddev`.

---

## 2. Anomaly Detection

### z-score Computation (BigQuery)

```sql
-- Anomaly detection join — run per scan after index_values loaded

SELECT
  iv.scan_id,
  iv.region_id,
  iv.h3_cell,
  iv.index_id,
  iv.index_value,
  iv.coverage_pct,
  bs.p50               AS baseline_p50,
  bs.stddev            AS baseline_stddev,
  bs.p10               AS baseline_p10,
  bs.p90               AS baseline_p90,
  SAFE_DIVIDE(iv.index_value - bs.p50, bs.stddev) AS z_score,
  CASE
    WHEN ABS(SAFE_DIVIDE(iv.index_value - bs.p50, bs.stddev)) >= @threshold_stddev
    THEN TRUE ELSE FALSE
  END AS anomaly,
  CASE
    WHEN iv.index_value < bs.p50 THEN 'decline'
    WHEN iv.index_value > bs.p50 THEN 'spike'
    ELSE 'neutral'
  END AS direction
FROM `{project}.sera_analytics.index_values` iv
JOIN `{project}.sera_analytics.baseline_spectral_stats` bs
  ON  iv.region_id    = bs.region_id
  AND iv.h3_cell      = bs.h3_cell
  AND iv.index_id     = bs.index_id
  AND iv.sensor_id    = bs.sensor_id
  AND EXTRACT(WEEK FROM iv.image_date) = bs.week_of_year
WHERE
  iv.scan_id = @scan_id
  AND iv.coverage_pct >= 0.5          -- exclude low-coverage cells
  AND bs.sample_count >= 10           -- exclude cells with insufficient baseline history
```

Results are written to a staging table and passed to the Anomaly Detection Agent.

### Seasonal Adjustment

The `week_of_year` join handles seasonal normalisation automatically — NDVI in week 36
(Northern Hemisphere late summer) is compared only to other week-36 observations,
never to winter values. No additional deseasonalisation step is required.

For indices with high inter-annual variance (e.g., NBR in fire-prone regions), the region
config can set `anomaly_detection: percentile_rank` instead of `z-score`, using the p10/p90
bands directly rather than stddev.

---

## 3. Raster-Vector Overlay (PostGIS)

After H3 tessellation, resolve which assets are affected:

```sql
-- Resolve H3 cells → asset IDs (run in PostGIS)
-- h3_cells_input is a VALUES list of the computed H3 cell IDs for this scan

SELECT DISTINCT
  ab.asset_id,
  ab.asset_tier,
  ab.criticality_score,
  hcv.h3_cell
FROM (VALUES {h3_cells_placeholder}) AS hcv(h3_cell)
JOIN asset_boundaries ab
  ON ST_Intersects(
    ab.geom,
    ST_H3CellToBoundary(hcv.h3_cell)  -- requires h3-pg extension
  )
WHERE ab.region_id = $1
```

If `h3-pg` extension is unavailable: convert H3 cells to WKT polygons in Python using the `h3` library,
then pass as a PostGIS `ST_GeomFromText` input. Slightly slower but same result.

---

## 4. Data Gap Tracking

Data gaps are recorded per scan, per index — they are outputs, not silent failures.

```python
def record_data_gap(scan_id, region_id, index_id, gap_date, reason,
                    cloud_cover_pct=None, sar_attempted=False, sar_success=False):
    bq_client.insert_rows_json(
        "sera_analytics.data_gap_log",
        [{
            "scan_id": scan_id,
            "region_id": region_id,
            "index_id": index_id,
            "gap_date": gap_date.isoformat(),
            "reason": reason,
            "cloud_cover_pct": cloud_cover_pct,
            "sar_fallback_attempted": sar_attempted,
            "sar_fallback_success": sar_success,
            "created_at": datetime.utcnow().isoformat()
        }]
    )
```

Gap counts feed directly into composite risk scoring via the data-gap penalty parameter.

---

## 5. Incremental Ingestion Checkpoint

Before loading any image, check what's already been processed:

```python
def get_last_processed_date(region_id, index_id, sensor_id):
    query = f"""
    SELECT MAX(image_date) AS last_date
    FROM `{PROJECT}.sera_analytics.index_values`
    WHERE region_id = @region_id
      AND index_id = @index_id
      AND sensor_id = @sensor_id
    """
    result = bq_client.query(query, job_config=QueryJobConfig(
        query_parameters=[
            ScalarQueryParameter("region_id", "STRING", region_id),
            ScalarQueryParameter("index_id",  "STRING", index_id),
            ScalarQueryParameter("sensor_id", "STRING", sensor_id),
        ]
    )).result()
    
    row = next(iter(result))
    return row.last_date  # None if no data yet
```

GEE image collection is then filtered: `filterDate(last_date + 1 day, today)`.
Re-running a scan never re-loads already-processed images.
