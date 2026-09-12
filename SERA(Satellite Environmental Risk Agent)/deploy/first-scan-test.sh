#!/usr/bin/env bash
# SERA — End-to-end pipeline smoke test
# Tests with a tiny polygon (London city centre, ~200 H3 cells)
# before committing to production regions.
#
# Usage: bash deploy/first-scan-test.sh <api-url>
# Example: bash deploy/first-scan-test.sh https://sera-api-dev-xxx.run.app

set -euo pipefail

API="${1:-http://localhost:8080}"
echo "==> SERA smoke test against: ${API}"

# ── 1. Health check ───────────────────────────────────────────────────────────
echo -n "Health: "
curl -sf "${API}/v1/health" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'])"

# ── 2. Register a tiny test region (London, ~200 H3 cells) ───────────────────
echo "==> Registering test region..."
curl -sf -X POST "${API}/v1/regions" \
  -H "Content-Type: application/json" \
  -d '{
    "region_id": "test-london-centre",
    "display_name": "Test: London City Centre",
    "asset_tier": "low",
    "geometry": {
      "type": "Polygon",
      "coordinates": [[
        [-0.12, 51.49],
        [-0.08, 51.49],
        [-0.08, 51.52],
        [-0.12, 51.52],
        [-0.12, 51.49]
      ]],
      "buffer_km": 1.0
    },
    "monitoring": {
      "interval_days": 7,
      "baseline_lookback_years": 5,
      "cloud_cover_threshold": 0.80
    },
    "indices": [
      {"id": "NDVI", "enabled": true, "weight": 0.6, "threshold_stddev": 2.0},
      {"id": "NDWI", "enabled": true, "weight": 0.4, "threshold_stddev": 2.0}
    ],
    "risk_scoring": {"fusion_method": "weighted_sum", "minimum_indices_required": 1},
    "notifications": [],
    "labels": {"region_id": "test-london-centre", "asset_tier": "low", "workflow_name": "sera-monitoring"}
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print('  Region:', d)"

# ── 3. Wait for H3 asset upload ───────────────────────────────────────────────
echo "==> Waiting 30s for H3 asset upload to start..."
sleep 30
echo "  (Check GEE Task Manager for 'sera-h3cells-test-london-centre')"

# ── 4. Trigger a scan ─────────────────────────────────────────────────────────
echo "==> Triggering scan..."
SCAN=$(curl -sf -X POST "${API}/v1/scans" \
  -H "Content-Type: application/json" \
  -d '{"region_ids": ["test-london-centre"]}')
echo "  ${SCAN}"
SCAN_ID=$(echo "${SCAN}" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['scan_id'])")

# ── 5. Poll until complete ────────────────────────────────────────────────────
echo "==> Polling scan ${SCAN_ID}..."
for i in $(seq 1 30); do
  STATUS=$(curl -sf "${API}/v1/scans/${SCAN_ID}" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "  [${i}/30] ${STATUS}"
  if [[ "${STATUS}" == "complete" || "${STATUS}" == "partial" || "${STATUS}" == "failed" ]]; then
    break
  fi
  sleep 60
done

# ── 6. Print results ──────────────────────────────────────────────────────────
echo "==> Scan results:"
curl -sf "${API}/v1/scans/${SCAN_ID}/results" | python3 -m json.tool

echo ""
echo "==> Smoke test complete. Check Cloud Logging for detailed task traces."
