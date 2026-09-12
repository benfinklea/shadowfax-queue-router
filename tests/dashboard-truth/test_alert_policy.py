import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import truth_suite as suite
from alert_policy import DAY, fingerprint

FAILURE = {"level": "FAIL", "signal": "runson.credits_read",
           "dashboard": {"credits_error": "access_denied", "credits_remaining": None},
           "instrument": {"credits_error": None}}


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.calls = []
        self.real_run = suite.run
        self.now = 100000
        self.rc = 0
        for name, value in (("STATE", self.state), ("CRON", True), ("run", self.send)):
            p = patch.object(suite, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(suite.time, "time", lambda: self.now)
        p.start()
        self.addCleanup(p.stop)
        self.failure = copy.deepcopy(FAILURE)
        self.failure["signal"] = "test.new_failure"

    def send(self, args, **kwargs):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, self.rc)

    def tick(self, failures=None):
        suite.results = [self.failure] if failures is None else failures
        self.assertEqual(suite.finish(), int(any(r["level"] == "FAIL" for r in suite.results)))
        self.now += 60

    def pages(self):
        return [args for args in self.calls if "--priority" in args]

    def test_same_failure_twice_does_not_repage_urgent(self):
        self.tick()
        self.tick()
        self.assertEqual(len(self.pages()), 1, "identical second failure must not page")
        self.assertEqual(self.pages()[0][4], "urgent")

    def test_changed_payload_pages_immediately(self):
        self.tick()
        self.tick()
        self.failure["dashboard"]["credits_error"] = "new_outage"
        self.tick()
        self.assertEqual(len(self.pages()), 2, "new payload on same check must immediately page")
        self.assertEqual(self.pages()[-1][4], "urgent")

    def test_new_signal_not_hidden_by_known_failure(self):
        self.tick([FAILURE])
        self.tick([FAILURE, self.failure])
        self.assertEqual(len(self.pages()), 2)
        self.assertEqual(self.pages()[0][4], "routine")
        self.assertEqual(self.pages()[1][4], "urgent")

    def test_owned_reminder_carries_ack_and_issue(self):
        self.tick()
        (self.state / "dispositions.json").write_text(json.dumps({fingerprint(self.failure): {
            "owner": "Cirdan", "issue": "https://github.com/armbrain-io/fleet-planning/issues/611",
            "note": "Scoped credential denial; repair owned."}}))
        for _ in range(5):
            self.tick()
        self.assertEqual(len(self.pages()), 2)
        self.assertEqual(self.pages()[-1][4], "routine")
        self.assertIn("Scoped credential denial", self.pages()[-1][-1])
        self.assertIn("/issues/611", self.pages()[-1][-1])
        self.failure["instrument"] = {"credits_error": "different"}
        self.tick()
        self.assertEqual(self.pages()[-1][4], "urgent", "ownership must not cover novel payload")

    def test_backoff_then_daily(self):
        for _ in range(12):
            self.tick()
        self.assertEqual(len(self.pages()), 3, "pages at observations 1, 6, 12")
        self.now = 100000 + DAY
        self.tick()
        self.assertEqual(len(self.pages()), 3)
        self.now = 100000 + DAY + 11 * 60
        self.tick()
        self.assertEqual(len(self.pages()), 4, "daily reminder due since last delivery")
        for _ in range(12):
            self.tick()
        self.assertEqual(len(self.pages()), 4, "sixth-run backoff ends after first day")

    def test_recovery_and_manual_run(self):
        self.tick()
        with patch.object(suite, "CRON", False), patch("builtins.print"):
            self.tick([])
        self.tick()
        self.assertEqual(len(self.pages()), 1, "manual run must not reset cron state")
        self.tick([dict(self.failure, level="PASS")])
        self.tick()
        self.assertEqual(len(self.pages()), 2, "recovered incident must rearm")

    def test_failed_transport_retries_first_and_reminder(self):
        self.rc = 1
        self.tick()
        self.rc = 0
        self.tick()
        self.assertEqual(len(self.pages()), 2)
        for _ in range(3):
            self.tick()
        self.rc = 1
        self.tick()
        self.rc = 0
        self.tick()
        self.assertEqual(len(self.pages()), 4, "failed reminder retries next run")

    def test_corrupt_state_and_disposition_do_not_hide_new_failure(self):
        (self.state / "alerts.json").write_text(json.dumps({fingerprint(self.failure): {"count": "broken"}}))
        (self.state / "dispositions.json").write_text("{truncated")
        with patch("builtins.print"):
            self.tick()
        self.assertEqual(len(self.pages()), 1, "corrupt state must not hide novel failure")
        self.assertEqual(self.pages()[0][4], "urgent")

    def test_unknown_or_missing_verdict_does_not_rearm(self):
        self.tick()
        self.tick([dict(self.failure, level="WARN"), dict(FAILURE, level="PASS")])
        self.tick([])
        self.tick()
        self.assertEqual(len(self.pages()), 1)

    def test_widespread_outage_batches_without_hiding_new_signals(self):
        failures = [dict(self.failure, signal=f"outage.{i}") for i in range(50)]
        self.tick(failures)
        self.assertEqual(len(self.pages()), 1)
        self.assertEqual(len(self.calls), 2, "one inbox batch and one council notification")
        for failure in failures:
            self.assertIn(failure["signal"], self.pages()[0][-1])
        self.tick(failures)
        self.assertEqual(len(self.pages()), 1)

    def test_recorded_credits_denial_matches_owned_disposition(self):
        recorded = json.loads(Path(__file__).with_name("credits-failure-fixture.json").read_text())
        self.tick([recorded])
        self.assertEqual(self.pages()[0][4], "routine")
        self.assertIn("Cirdan", self.pages()[0][-1])

    def labeled_credits_failure(self):
        with patch.object(suite, "results", []):
            suite.check_runson_credits_read({"available": True, **FAILURE["dashboard"]})
            return copy.deepcopy(suite.results[0])

    def test_credits_contract_label_preserves_recorded_key_without_mutating_report(self):
        recorded = json.loads(Path(__file__).with_name("credits-failure-fixture.json").read_text())
        labeled = self.labeled_credits_failure()
        before = copy.deepcopy(labeled)
        self.assertEqual(fingerprint(labeled), fingerprint(recorded))
        self.assertEqual(labeled, before, "canonicalization must not strip labels from the report")
        self.tick([labeled])
        self.assertEqual(self.pages()[0][4], "routine")
        self.assertIn("Cirdan", self.pages()[0][-1])
        self.assertIn("expected_contract", self.pages()[0][-1])

    def test_credits_contract_label_retains_existing_backoff_and_owner(self):
        self.tick([FAILURE])
        key = fingerprint(FAILURE)
        original = json.loads((self.state / "alerts.json").read_text())[key]
        labeled = self.labeled_credits_failure()
        self.tick([labeled])
        self.assertEqual(len(self.pages()), 1, "relabeling the same denial must not page anew")
        current = json.loads((self.state / "alerts.json").read_text())
        self.assertEqual(set(current), {key})
        self.assertEqual(current[key], dict(original, count=2))
        for _ in range(4):
            self.tick([labeled])
        self.assertEqual(len(self.pages()), 2, "existing sixth-observation backoff remains due")
        self.assertEqual(self.pages()[-1][4], "routine")
        self.assertIn("Cirdan", self.pages()[-1][-1])
        self.assertIn("/issues/611", self.pages()[-1][-1])

    def test_changed_credit_claim_or_expectation_gets_new_key_and_immediate_page(self):
        labeled = self.labeled_credits_failure()
        self.tick([labeled])
        seen = {fingerprint(labeled)}
        for field, key, value in (
            ("dashboard", "credits_error", "timeout"),
            ("dashboard", "credits_remaining", 42),
            ("expected", "credits_error", "different_expectation"),
        ):
            with self.subTest(field=field, key=key):
                changed = copy.deepcopy(labeled)
                target = changed["instrument"]["expected"] if field == "expected" else changed[field]
                target[key] = value
                self.assertNotIn(fingerprint(changed), seen)
                seen.add(fingerprint(changed))
                self.tick([changed])
                self.assertEqual(len(self.pages()), len(seen))
                self.assertEqual(self.pages()[-1][4], "urgent")

    def test_credits_normalization_keeps_other_evidence_and_signals_distinct(self):
        labeled = self.labeled_credits_failure()
        key = fingerprint(labeled)
        instruments = []
        for value in (True, 0, None):
            changed = copy.deepcopy(labeled["instrument"])
            changed["independent_aws_measurement"] = value
            instruments.append(changed)
        for extra in ({"source": "independent AWS probe"}, {"kind": "measurement"},
                      {"expected": {"credits_error": None, "additional_claim": True}},
                      {"expected": None}):
            instruments.append({**copy.deepcopy(labeled["instrument"]), **extra})
        instruments.append({"kind": "expected_contract", "expected": {"credits_error": None}})
        for instrument in instruments:
            with self.subTest(instrument=instrument):
                self.assertNotEqual(fingerprint(dict(labeled, instrument=instrument)), key)
        self.assertNotEqual(fingerprint(dict(labeled, signal="unrelated.signal")),
                            fingerprint(dict(FAILURE, signal="unrelated.signal")))

    def test_real_transport_contract_returns_nonzero_without_raising(self):
        # The production helper uses subprocess.run without check=True.
        with patch.object(suite, "run", self.real_run):
            sent = suite.run(["python3", "-c", "raise SystemExit(7)"])
        self.assertEqual(sent.returncode, 7)

    def test_canonical_order_and_incomplete_ownership(self):
        (self.state / "dispositions.json").write_text(json.dumps({fingerprint(self.failure): {"owner": "Cirdan"}}))
        self.tick()
        self.failure["dashboard"] = dict(reversed(list(self.failure["dashboard"].items())))
        self.tick()
        self.assertEqual(len(self.pages()), 1)
        self.assertEqual(self.pages()[0][4], "urgent")


if __name__ == "__main__":
    unittest.main()
