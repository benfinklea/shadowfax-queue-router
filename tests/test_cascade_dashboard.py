import json
import os
import tempfile
import unittest

import cascade_dashboard


class TestCascadeDashboard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.report_path = os.path.join(self.tmp.name, "report.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write_report(self, **overrides):
        report = {
            "generated_at": 100.0,
            "latest_event_at": 99.0,
            "stale": False,
            "stale_after_seconds": 900,
            "definition": "No frontier calls.",
            "tasks": {
                "completed": 10,
                "dollar_zero": 8,
                "dollar_zero_pct": 80.0,
                "baseline_days": 3,
                "baseline_days_required": 14,
                "target_pct": None,
                "target_status": "collecting_baseline",
                "daily": [{"date": "2026-07-25", "dollar_zero_pct": 80.0}],
                "weekly": [{"week": "2026-W30", "dollar_zero_pct": 80.0}],
            },
            "tiers": [],
            "links": [],
        }
        report.update(overrides)
        with open(self.report_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle)

    def test_api_returns_rollup(self):
        self.write_report()
        report = cascade_dashboard.load_cascade_report(self.report_path)
        self.assertTrue(report["available"])
        self.assertEqual(report["tasks"]["dollar_zero_pct"], 80.0)

    def test_missing_snapshot_fails_loud(self):
        report = cascade_dashboard.load_cascade_report(self.report_path)
        self.assertFalse(report["available"])
        self.assertTrue(report["stale"])

    def test_invalid_snapshot_fails_loud(self):
        with open(self.report_path, "w", encoding="utf-8") as handle:
            handle.write('{"tasks":"not-an-object"}')
        report = cascade_dashboard.load_cascade_report(self.report_path)
        self.assertFalse(report["available"])

    def test_home_contains_cascade_waterfall(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "queue_router.py"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('id=cascade-card', html)
        self.assertIn("refreshCascade()", html)
        self.assertIn("/api/cascade", html)


if __name__ == "__main__":
    unittest.main()
