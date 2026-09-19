"""Regression tests for populated agent checkpoints; no cloud credentials needed."""
import json
import math
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch

from sera.agents.state import AgentStateStore
from sera.tasks import agent_tasks


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.bq = Mock()
        self.bq.insert_rows_json.return_value = []
        self.store = AgentStateStore(self.bq, "test_dataset")

    def test_bigquery_dates_round_trip(self):
        payload = {"all_rows": [{
            "image_date": date(2026, 8, 27),
            "observed_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
            "index_value": 0.7,
        }]}
        self.store.write_anomaly_output("scan", "region", payload)
        row = self.bq.insert_rows_json.call_args.args[1][0]
        self.bq.query.return_value.result.return_value = [row]
        restored = self.store.read_anomaly_output("scan")
        self.assertEqual(restored["all_rows"][0]["image_date"], "2026-08-27")
        self.assertEqual(restored["all_rows"][0]["observed_at"], "2026-08-27T00:00:00+00:00")
        self.assertEqual(restored["all_rows"][0]["index_value"], 0.7)

    def test_checkpoint_stage_comes_from_table_not_payload(self):
        for payloads, expected in [
            ([None, None], set()),
            ([{}, None], {"anomaly_detection"}),
            ([{"all_rows": []}, {"asset_risks": []}],
             {"anomaly_detection", "risk_evaluation"}),
        ]:
            with self.subTest(expected=expected):
                self.store._read_latest = Mock(side_effect=payloads)
                self.assertEqual(self.store.completed_stages("scan"), expected)

    def test_unsupported_objects_and_nonfinite_values_fail_before_write(self):
        for value in [object(), math.nan, math.inf]:
            with self.subTest(value=value):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.write_anomaly_output("scan", "region", {"value": value})
        self.bq.insert_rows_json.assert_not_called()

    def test_insert_failure_is_not_reported_as_success(self):
        self.bq.insert_rows_json.return_value = [{"errors": [{"reason": "invalid"}]}]
        with self.assertRaises(RuntimeError):
            self.store.write_risk_output("scan", "region", {"asset_risks": []})

    def test_nonempty_risk_output_reports_before_and_after_checkpoint(self):
        # Synthetic plumbing evidence only; this is not a measured Gyirong detection.
        scan_id = "scan_20260827_gyirong-replay"
        rows = [{
            "h3_cell": "synthetic-cell", "index_id": "NDWI",
            "image_date": date(2026, 8, 27), "z_score": 5.0,
            "index_value": 0.7, "baseline_p50": 0.2,
            "anomaly": True, "direction": "spike", "coverage_pct": 0.95,
        }]
        config = {"indices": [{"id": "NDWI", "weight": 1.0,
                               "threshold_stddev": 2.0, "enabled": True}]}
        assets = {"gyirong-port": {"tier": "critical", "criticality": 1.0,
                                   "cells": ["synthetic-cell"]}}
        with patch("sera.storage.postgis.resolve_h3_to_assets", return_value=assets), \
             patch.object(agent_tasks, "_load_region_config", return_value=config), \
             patch.object(agent_tasks, "_get_gap_indices", return_value=[]):
            output = agent_tasks._run_risk_evaluation(
                self.bq, scan_id, "gyirong-replay",
                {"all_rows": rows, "anomalies": rows},
            )
        self.assertIsInstance(output["asset_risks"][0]["index_scores"][0], dict)
        self.assertIsInstance(output["asset_risks"][0]["data_quality"], dict)
        self.store.write_risk_output(scan_id, "gyirong-replay", output)
        row = self.bq.insert_rows_json.call_args.args[1][0]
        self.bq.query.return_value.result.return_value = [row]
        restored = self.store.read_risk_output(scan_id)
        fresh = agent_tasks._run_reporting(self.bq, scan_id, "gyirong-replay", output, {})
        resumed = agent_tasks._run_reporting(self.bq, scan_id, "gyirong-replay", restored, {})
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["risk_tier"], "CRITICAL")
        self.assertIn("NDWI anomaly", fresh[0]["executive_summary"])
        # Event identifiers remain random in the existing reporting implementation.
        for event in (fresh[0], resumed[0]):
            event.pop("event_id")
        self.assertEqual(fresh, resumed)
        json.dumps(resumed, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
