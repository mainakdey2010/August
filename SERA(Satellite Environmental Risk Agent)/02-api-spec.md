# SERA — REST API Specification
`v1.0 | 2026-09-07`

Base URL: `https://api.sera.internal/v1`
Auth: OAuth2 Bearer token (scope: `sera:read`, `sera:write`, `sera:admin`)

---

## Scan Lifecycle

### `POST /v1/scans`
Trigger a scan for one or more regions.

**Request**
```json
{
  "region_ids": ["emea-energy-grid-north"],
  "scan_date": "2026-09-07",           // optional — defaults to today
  "force_recompute": false,            // re-run even if scan_date already processed
  "index_override": ["NDVI", "LST"]   // optional — override region config
}
```

**Response `202 Accepted`**
```json
{
  "scan_id": "scan_20260907_emea-energy-north",
  "region_id": "emea-energy-grid-north",
  "status": "pending",
  "poll_url": "/v1/scans/scan_20260907_emea-energy-north"
}
```

**Errors**
- `409 Conflict` — scan already exists for this region/date (use `force_recompute: true`)
- `404 Not Found` — region_id not registered
- `400 Bad Request` — invalid index_override value

---

### `GET /v1/scans/{scan_id}`
Poll scan status.

**Response `200 OK`**
```json
{
  "scan_id": "scan_20260907_emea-energy-north",
  "region_id": "emea-energy-grid-north",
  "status": "reasoning",               // pending|ingesting|reasoning|complete|partial|failed
  "scan_date": "2026-09-07",
  "progress": {
    "stage": "agent:risk-evaluation",
    "indices_computed": ["NDVI", "LST", "NDRE"],
    "indices_remaining": ["NBR"],
    "indices_gap": []
  },
  "created_at": "2026-09-07T08:00:00Z",
  "updated_at": "2026-09-07T08:11:23Z"
}
```

---

### `GET /v1/scans/{scan_id}/results`
Retrieve full results. Only available when `status` is `complete` or `partial`.

**Query params**
- `format` — `json` (default) | `geojson`
- `min_risk_tier` — `CRITICAL` | `HIGH` | `MEDIUM` | `LOW` (filter results)

**Response `200 OK`**
```json
{
  "scan_id": "...",
  "scan_date": "2026-09-07",
  "region_id": "emea-energy-grid-north",
  "status": "complete",
  "summary": {
    "indices_computed": ["NDVI", "LST", "NDRE", "NBR"],
    "indices_gap": ["BSI"],
    "gap_reasons": { "BSI": "cloud_cover (87%); no SAR proxy" },
    "risk_events": { "CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3 }
  },
  "events": [ /* array of risk event objects — see data-models.md */ ]
}
```

---

## Region Management

### `GET /v1/regions`
List all registered monitoring regions.

**Query params:** `asset_tier`, `active` (bool), `page`, `page_size`

**Response `200 OK`**
```json
{
  "regions": [
    {
      "region_id": "emea-energy-grid-north",
      "display_name": "EMEA Northern Energy Grid",
      "asset_tier": "critical",
      "active": true,
      "monitoring_interval_days": 7,
      "index_count": 5,
      "last_scan_date": "2026-09-07",
      "last_scan_status": "complete"
    }
  ],
  "total": 12,
  "page": 1
}
```

---

### `POST /v1/regions`
Register a new monitoring region.

**Request:** full monitoring-region config (see `config/monitoring-region.yaml`)

**Response `201 Created`**
```json
{ "region_id": "emea-energy-grid-north", "status": "registered" }
```

---

### `GET /v1/regions/{region_id}`
Retrieve region config and current status.

### `PATCH /v1/regions/{region_id}`
Update region config (partial update — only provided fields are changed).
Changes take effect on next scheduled scan.

### `DELETE /v1/regions/{region_id}`
Deactivate region (soft delete — sets `active: false`). Existing scan history retained.

---

### `GET /v1/regions/{region_id}/history`
Risk history for a region.

**Query params:** `from_date`, `to_date`, `min_risk_tier`, `page`, `page_size`

**Response `200 OK`**
```json
{
  "region_id": "emea-energy-grid-north",
  "events": [
    {
      "event_date": "2026-09-07",
      "risk_tier": "HIGH",
      "composite_score": 0.74,
      "event_id": "evt_20260907_..."
    }
  ]
}
```

---

## Asset Queries

### `GET /v1/assets/{asset_id}/risk`
Current risk assessment for a specific asset.

**Response `200 OK`**
```json
{
  "asset_id": "ast_001",
  "region_id": "emea-energy-grid-north",
  "as_of_date": "2026-09-07",
  "risk_tier": "HIGH",
  "composite_score": 0.74,
  "index_scores": { ... },
  "data_gap_indices": [],
  "event_id": "evt_20260907_..."
}
```

---

### `GET /v1/assets/{asset_id}/history`
**Query params:** `from_date`, `to_date`, `page`, `page_size`

---

## Baseline Management

### `GET /v1/baselines/{region_id}`
Retrieve baseline statistics summary for a region.

**Query params:** `index_id`, `week_of_year`

**Response `200 OK`**
```json
{
  "region_id": "emea-energy-grid-north",
  "index_id": "NDVI",
  "week_of_year": 36,
  "baseline_years": [2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
  "cell_count": 9842,
  "stats": {
    "p10": 0.21, "p50": 0.61, "p90": 0.78,
    "mean": 0.59, "stddev": 0.12
  },
  "last_updated": "2026-09-01T02:00:00Z"
}
```

### `POST /v1/baselines/{region_id}/refresh`
Trigger an out-of-schedule baseline recompute.
`scope`: `"full"` (full historical rebuild) | `"incremental"` (add latest season only)

---

## Index Registry

### `GET /v1/indices`
List all configured spectral indices.

**Response `200 OK`**
```json
{
  "indices": [
    {
      "index_id": "NDVI",
      "name": "Normalized Difference Vegetation Index",
      "primary_sensor": "S2_SR",
      "fallback_sensor": "L8_T1",
      "sar_proxy": "SAR_RVI",
      "cloud_sensitive": true,
      "seasonal_adjustment": true
    }
  ]
}
```

---

## Webhooks

### `POST /v1/webhooks`
Register a webhook receiver.

**Request**
```json
{
  "url": "https://ops.example.com/sera-alerts",
  "secret": "sha256-hmac-secret",
  "events": ["CRITICAL", "HIGH"],
  "region_ids": ["emea-energy-grid-north"],  // empty = all regions
  "active": true
}
```

**Response `201 Created`**
```json
{ "webhook_id": "wh_abc123", "status": "active" }
```

**Delivery payload** (POST to registered URL, signed with HMAC-SHA256)
```json
{
  "webhook_id": "wh_abc123",
  "event_type": "risk_event.HIGH",
  "timestamp": "2026-09-07T08:47:00Z",
  "event": { /* full risk event object */ }
}
```

### `GET /v1/webhooks` — list registered webhooks
### `DELETE /v1/webhooks/{webhook_id}` — deregister

---

## Health & Diagnostics

### `GET /v1/health`
```json
{
  "status": "healthy",
  "components": {
    "api": "healthy",
    "celery": "healthy",
    "redis": "healthy",
    "postgres": "healthy",
    "bigquery": "healthy",
    "gee": "healthy"
  },
  "gee_quota": {
    "concurrent_tasks_used": 4,
    "concurrent_tasks_limit": 10,
    "export_quota_pct_used": 12
  }
}
```

---

## Error Format (all endpoints)

```json
{
  "error": {
    "code": "SCAN_ALREADY_EXISTS",
    "message": "A scan for region emea-energy-grid-north on 2026-09-07 already exists.",
    "detail": { "scan_id": "scan_20260907_emea-energy-north" },
    "request_id": "req_xyz789"
  }
}
```

Standard HTTP status codes. `request_id` present on all responses for tracing.
