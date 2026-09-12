"""MONITOR-REVIEW-OWNERS: per-seat review breakdowns + stale-verdict detector.

Fixtures in tests/fixtures/ were captured live from armbrain-io/armbrain at
3:2x PM CDT 2026-09-12 (see the dispatch this PR implements). #7277 is the
known-positive: gimli-seat's latest review is CHANGES_REQUESTED against a
commit two heads behind the current one, while galadriel-seat re-approved at
the current head - reviewDecision still reads CHANGES_REQUESTED because
GitHub's own algorithm counts gimli's un-dismissed verdict. #7571 is a
plain approved-at-head PR with nothing stale.
"""
import json
import os
import unittest

import queue_router as qr

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def pr_from_fixture(name, labels=(), updated_at="2026-09-12T20:00:00Z"):
    fx = load_fixture(name)
    return dict(fx, labels=[{"name": l} for l in labels], updatedAt=updated_at)


class ReviewRoutedBreakdownTest(unittest.TestCase):
    def test_waiting_seat_excludes_reviewer_who_already_cleared_the_head(self):
        # galadriel-seat's latest review on #7277 IS at the current head
        # (APPROVED @6b984d069...) - galadriel must NOT appear in the
        # 'waiting' rows even though the galadriel-review label is still on
        # the PR (labels aren't auto-removed on review).
        pr = pr_from_fixture("pr7277-stale-gimli.json", labels=["gimli-review", "galadriel-review"])
        bd = qr._review_routed_breakdown([pr])
        seats = {b["label"]: b["count"] for b in bd["buckets"]}
        self.assertEqual(seats, {"gimli": 1})
        self.assertNotIn("galadriel", seats)

    def test_two_labels_fan_out_to_two_rows_not_a_partition(self):
        pr1 = pr_from_fixture("pr7571-fresh-approved.json", labels=["gimli-review"], updated_at="2026-09-10T00:00:00Z")
        pr2 = dict(pr1, number=999, labels=[{"name": "cirdan-review"}])
        bd = qr._review_routed_breakdown([pr1, pr2])
        seats = {b["label"]: b["count"] for b in bd["buckets"]}
        self.assertEqual(seats, {"gimli": 1, "cirdan": 1})

    def test_no_review_labels_produces_no_rows(self):
        pr = pr_from_fixture("pr7571-fresh-approved.json", labels=[])
        self.assertEqual(qr._review_routed_breakdown([pr])["buckets"], [])

    def test_oldest_is_a_min_over_waits_not_a_max_over_activity(self):
        stale_pr = pr_from_fixture("pr7571-fresh-approved.json", labels=["gimli-review"], updated_at="2026-09-01T00:00:00Z")
        fresh_pr = dict(stale_pr, number=1000, updatedAt="2026-09-12T19:00:00Z")
        bd = qr._review_routed_breakdown([stale_pr, fresh_pr])
        row = bd["buckets"][0]
        self.assertEqual(row["count"], 2)
        # Oldest of the two PRs' updatedAt -> the OLD one (2026-09-01), not
        # the newest activity - this is the exact defect claim #2 describes.
        self.assertGreater(row["oldest_h"], 200)  # ~11 days old, comfortably past any 1h slip


class StaleVerdictTest(unittest.TestCase):
    def test_known_positive_7277_fires_with_a_named_assertion(self):
        pr = pr_from_fixture("pr7277-stale-gimli.json", labels=["gimli-review"])
        _in_review_bd, stale = qr._in_review_breakdown_and_stale([pr])
        self.assertEqual(stale["count"], 1, "the #7277 known-positive must be counted as exactly one stale PR")
        seats = {b["label"] for b in stale["breakdown"]["buckets"]}
        self.assertEqual(seats, {"gimli"}, "the stale verdict must be attributed to gimli, not galadriel")

    def test_known_negative_approved_at_head_does_not_fire(self):
        pr = pr_from_fixture("pr7571-fresh-approved.json", labels=["gimli-review"])
        self.assertEqual(pr["reviewDecision"], "APPROVED")
        _in_review_bd, stale = qr._in_review_breakdown_and_stale([pr])
        self.assertEqual(stale["count"], 0)

    def test_changes_requested_at_current_head_is_not_stale(self):
        # Synthetic known-negative distinct from #7571: a CHANGES_REQUESTED
        # verdict left AGAINST the current head must not be flagged - only a
        # verdict against an OLD head is stale.
        pr = {
            "number": 42, "headRefOid": "HEAD1", "reviewDecision": "CHANGES_REQUESTED",
            "labels": [{"name": "gimli-review"}], "updatedAt": "2026-09-12T20:00:00Z",
            "reviews": [{"author": {"login": "gimli-seat"}, "state": "CHANGES_REQUESTED",
                         "submittedAt": "2026-09-12T19:00:00Z", "commit": {"oid": "HEAD1"}}],
        }
        in_review_bd, stale = qr._in_review_breakdown_and_stale([pr])
        self.assertEqual(stale["count"], 0)
        seats = {b["label"] for b in in_review_bd["buckets"]}
        self.assertIn("author", seats, "still in-review, attributed to the author, not the reviewer")

    def test_mutation_inverting_the_head_comparison_is_caught(self):
        """Prove the detector is not decorative: if the `== head` comparison
        were flipped to `!=` (the mutation this brief calls out), #7277 must
        stop being counted and the fresh-at-head negative must start being
        miscounted - i.e. the two tests above must disagree with each other
        under the mutant. This test locks in the correct polarity directly,
        so a future edit that flips the comparison fails THIS assertion with
        a named message, not a traceback."""
        stale_pr = pr_from_fixture("pr7277-stale-gimli.json", labels=["gimli-review"])
        fresh_pr = {
            "number": 42, "headRefOid": "HEAD1", "reviewDecision": "CHANGES_REQUESTED",
            "labels": [{"name": "gimli-review"}], "updatedAt": "2026-09-12T20:00:00Z",
            "reviews": [{"author": {"login": "gimli-seat"}, "state": "CHANGES_REQUESTED",
                         "submittedAt": "2026-09-12T19:00:00Z", "commit": {"oid": "HEAD1"}}],
        }
        _, stale_positive = qr._in_review_breakdown_and_stale([stale_pr])
        _, stale_negative = qr._in_review_breakdown_and_stale([fresh_pr])
        self.assertEqual(stale_positive["count"], 1)
        self.assertEqual(stale_negative["count"], 0)

    def test_missing_head_never_flags_stale(self):
        # An unread headRefOid is an unknown, not a defect - must not read as stale.
        pr = pr_from_fixture("pr7277-stale-gimli.json", labels=["gimli-review"])
        pr = dict(pr, headRefOid=None)
        _in_review_bd, stale = qr._in_review_breakdown_and_stale([pr])
        self.assertEqual(stale["count"], 0)

    def test_zero_rows_reports_none_never_zero_hours(self):
        bd, stale = qr._in_review_breakdown_and_stale([])
        self.assertEqual(bd["buckets"], [])
        self.assertEqual(stale["count"], 0)
        self.assertIsNone(stale["last_at"])

    def test_review_required_buckets_by_routing_label_not_reviewer(self):
        pr = {
            "number": 7, "headRefOid": "H", "reviewDecision": "REVIEW_REQUIRED",
            "labels": [{"name": "cirdan-review"}], "updatedAt": "2026-09-12T18:00:00Z", "reviews": [],
        }
        bd, stale = qr._in_review_breakdown_and_stale([pr])
        seats = {b["label"] for b in bd["buckets"]}
        self.assertEqual(seats, {"cirdan"})
        self.assertEqual(stale["count"], 0)

    def test_review_required_with_no_label_buckets_unassigned(self):
        pr = {"number": 8, "headRefOid": "H", "reviewDecision": "REVIEW_REQUIRED",
              "labels": [], "updatedAt": "2026-09-12T18:00:00Z", "reviews": []}
        bd, _stale = qr._in_review_breakdown_and_stale([pr])
        self.assertEqual({b["label"] for b in bd["buckets"]}, {"unassigned"})


class OldestHoursTest(unittest.TestCase):
    def test_zero_timestamps_is_none_not_zero(self):
        self.assertIsNone(qr._oldest_hours([]))


if __name__ == "__main__":
    unittest.main()
