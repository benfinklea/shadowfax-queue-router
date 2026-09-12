"""An arrow with no rate instrument must render 'n/a', never the alarming
word 'stalled' - only a MEASURED zero rate with a backlog behind it earns
that word (benfinklea/shadowfax-queue-router#39).

Tests go through the real entry point, _build_ship_arrows(result, rates),
not the private label helper directly, so a future internal refactor of
how the label is computed doesn't invalidate these."""
import unittest
import queue_router as qr


def arrow(arrows, key):
    return next(a for a in arrows if a["key"] == key)


class DrainLabelTest(unittest.TestCase):
    def test_no_instrument_renders_na_and_stays_bottleneck_ineligible(self):
        # Live evidence from #39: merged-deploy has no hourly rate instrument
        # (only daily counts) and a backlog of 8 - the healthiest stage in
        # the pipeline (3/3 deploys OK today) was the only one alarming.
        result = {"merged_today": 8}
        rates = {}
        a = arrow(qr._build_ship_arrows(result, rates), "merged-deploy")
        self.assertIsNone(a["rate_per_hour"],
                           "rate_per_hour must stay None (not coerced to 0) so the "
                           "client's bottleneck-eligibility check, which excludes "
                           "rate_per_hour === null arrows, correctly skips this one")
        self.assertEqual(a["backlog"], 8)
        self.assertEqual(a["drain_label"], "n/a",
                          "an absent instrument must never render the word 'stalled'")

    def test_measured_stall_still_renders_stalled_and_bottleneck_eligible(self):
        # The fix must not go too far: a REAL stall (measured rate of 0
        # with backlog behind it) must keep saying 'stalled' and must keep
        # rate_per_hour === 0 (not None), which is what makes the client
        # count it as bottleneck-eligible and colour the belt red.
        result = {"ci_queued": 5, "ci_running": 0}
        rates = {"runs_created_hour": 0}
        a = arrow(qr._build_ship_arrows(result, rates), "prs-ci")
        self.assertEqual(a["rate_per_hour"], 0)
        self.assertEqual(a["backlog"], 5)
        self.assertEqual(a["drain_label"], "stalled",
                          "a measured rate of 0 with a real backlog is a genuine stall")

    def test_positive_rate_renders_hours_to_drain(self):
        result = {"issues_open": 20}
        rates = {"pr_created_hour": 4}
        a = arrow(qr._build_ship_arrows(result, rates), "issues-prs")
        self.assertEqual(a["drain_label"], "5h to drain")

    def test_other_arrow_that_can_reach_rate_none_gets_the_same_fix(self):
        # ci-green's runs_success_hour is hardcoded None for any repo other
        # than GITHUB_CI_REPO (see _get_arrow_rates) - the same latent false
        # alarm the issue asks us to check for elsewhere.
        result = {"ci_queued": 3, "ci_running": 2}
        rates = {}
        a = arrow(qr._build_ship_arrows(result, rates), "ci-green")
        self.assertIsNone(a["rate_per_hour"])
        self.assertEqual(a["backlog"], 5)
        self.assertEqual(a["drain_label"], "n/a")

    def test_no_instrument_and_no_backlog_is_na_not_a_fake_zero_hours(self):
        # Per the issue's own table, an absent rate is unconditionally 'n/a' -
        # not just when there happens to be a backlog. Previously this
        # rendered '0h to drain', implying a measured rate that never existed.
        result = {"merged_today": 0}
        rates = {}
        a = arrow(qr._build_ship_arrows(result, rates), "merged-deploy")
        self.assertEqual(a["drain_label"], "n/a")


if __name__ == "__main__":
    unittest.main()
