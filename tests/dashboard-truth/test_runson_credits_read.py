"""Offline checks of credit-read reporting; no AWS or dashboard requests."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

import truth_suite as suite


class CreditsReadTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("results", []), ("CRON", False)):
            mocked = patch.object(suite, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        for name in ("run", "http", "url_json", "finish"):
            mocked = patch.object(suite, name, side_effect=AssertionError("offline check only"))
            mocked.start()
            self.addCleanup(mocked.stop)

    def check(self, payload):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            suite.check_runson_credits_read(payload)
        row = suite.results[-1]
        self.assertEqual(row["signal"], "runson.credits_read")
        self.assertIn("kind", row["instrument"], "contract expectations must be explicitly labeled")
        self.assertEqual(row["instrument"]["kind"], "expected_contract")
        self.assertEqual(row["instrument"]["expected"], {"credits_error": None})
        self.assertIs(row["instrument"]["independent_aws_measurement"], False)
        self.assertNotIn("credits_error", row["instrument"], "expected value is not a measured value")
        self.assertIn("no independent AWS measurement", row["detail"])
        # Both the stored JSON and console output must keep the evidence label.
        self.assertEqual(json.loads(json.dumps(row)), row)
        self.assertIn('"kind":"expected_contract"', output.getvalue())
        self.assertIn('"independent_aws_measurement":false', output.getvalue())
        self.assertIn("no independent AWS measurement", output.getvalue())
        return row

    def test_denial_remains_failure(self):
        row = self.check({"available": True, "credits_error": "access_denied", "credits_remaining": None})
        self.assertEqual(row["level"], "FAIL")
        self.assertEqual(row["dashboard"], {"credits_error": "access_denied", "credits_remaining": None})
        self.assertIn("dashboard reports credits read failing (access_denied)", row["detail"])

    def test_unavailable_response_does_not_hide_reported_denial(self):
        row = self.check({"available": False, "credits_error": "access_denied"})
        self.assertEqual(row["level"], "FAIL")

    def test_clean_status_passes_only_as_dashboard_contract(self):
        for remaining in (0, 100.25, None):
            with self.subTest(credits_remaining=remaining):
                row = self.check({"available": True, "credits_error": None, "credits_remaining": remaining})
                self.assertEqual(row["level"], "PASS")
                self.assertEqual(row["dashboard"]["credits_remaining"], remaining)
                self.assertIn("dashboard reports credits read clean", row["detail"])

    def test_absent_or_unavailable_status_never_passes_as_clean(self):
        for payload in (
            {},
            {"available": False, "error": "credentials", "message": "creds expired; aws login"},
            {"available": False, "credits_error": None},
            {"available": True, "credits_remaining": 100.25},
            {"credits_error": None, "credits_remaining": 100.25},
            {"available": True, "credits_error": ""},
        ):
            with self.subTest(payload=payload):
                row = self.check(payload)
                self.assertEqual(row["level"], "WARN")
                self.assertIn("clean read cannot be established", row["detail"])

    def test_failure_does_not_invent_a_reason_for_numeric_balance(self):
        row = self.check({"available": True, "credits_error": "timeout", "credits_remaining": 42})
        self.assertEqual(row["level"], "FAIL")
        self.assertEqual(row["dashboard"]["credits_remaining"], 42)
        self.assertNotIn("credits_remaining is null", row["detail"])


if __name__ == "__main__":
    unittest.main()
