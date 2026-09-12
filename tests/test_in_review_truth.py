"""in_review must count only reviewer-blocked PRs - not drafts, holds, or
rework. monitor-in-review-truth-20260911: one square (in_review) was silently
mixing four different states behind a single number."""
import unittest

import queue_router as qr


def make_pr(number, is_draft=False, decision=None, labels=None, updated_at=None, reviews=None):
    return {
        "number": number, "title": f"PR {number}", "isDraft": is_draft,
        "reviewDecision": decision, "labels": [{"name": l} for l in (labels or [])],
        "updatedAt": updated_at, "reviews": reviews or [],
    }


class ClassifyReviewPrsTest(unittest.TestCase):
    def test_four_cases_land_in_the_right_bucket(self):
        draft = make_pr(1, is_draft=True, decision="REVIEW_REQUIRED")
        held = make_pr(2, decision="REVIEW_REQUIRED", labels=["hold"])
        rework = make_pr(3, decision="CHANGES_REQUESTED")
        reviewable = make_pr(4, decision="REVIEW_REQUIRED")
        in_review, changes_requested, drafts, held_prs = qr._classify_review_prs(
            [draft, held, rework, reviewable])
        self.assertEqual([p["number"] for p in in_review], [4])
        self.assertEqual([p["number"] for p in changes_requested], [3])
        self.assertEqual([p["number"] for p in drafts], [1])
        self.assertEqual([p["number"] for p in held_prs], [2])

    def test_reviewer_routed_label_is_not_a_hold_for_in_review(self):
        # gate-review/galadriel-review ROUTE a PR to a reviewer; unlike
        # green_waiting, in_review must NOT treat that as a hold.
        routed = make_pr(5, decision="REVIEW_REQUIRED", labels=["gate-review"])
        in_review, changes_requested, drafts, held_prs = qr._classify_review_prs([routed])
        self.assertEqual([p["number"] for p in in_review], [5])
        self.assertEqual(held_prs, [])

    def test_draft_takes_priority_over_held_and_decision(self):
        both = make_pr(6, is_draft=True, decision="CHANGES_REQUESTED", labels=["hold"])
        in_review, changes_requested, drafts, held_prs = qr._classify_review_prs([both])
        self.assertEqual([p["number"] for p in drafts], [6])
        self.assertEqual(changes_requested, [])
        self.assertEqual(held_prs, [])

    def test_approved_prs_are_excluded_from_every_bucket(self):
        approved = make_pr(7, decision="APPROVED")
        result = qr._classify_review_prs([approved])
        self.assertEqual(result, ([], [], [], []))


class PipelineCompositionTest(unittest.TestCase):
    """Pin the composition the brief measured live on armbrain-io/armbrain at
    10:10 PM CDT 2026-09-11: 41 mixed = 21 rework + 11 draft + 4 held + 5
    genuinely reviewable. A test asserting only the total (41, or the fixed 5)
    would not have caught the original bug - assert every bucket."""

    def test_matches_measured_composition(self):
        prs = ([make_pr(n, decision="CHANGES_REQUESTED") for n in range(1, 22)]
               + [make_pr(100 + n, is_draft=True, decision="REVIEW_REQUIRED") for n in range(11)]
               + [make_pr(200 + n, decision="REVIEW_REQUIRED", labels=["hold"]) for n in range(4)]
               + [make_pr(300 + n, decision="REVIEW_REQUIRED") for n in range(5)])
        in_review, changes_requested, drafts, held_prs = qr._classify_review_prs(prs)
        self.assertEqual(len(changes_requested), 21)
        self.assertEqual(len(drafts), 11)
        self.assertEqual(len(held_prs), 4)
        self.assertEqual(len(in_review), 5)
        self.assertEqual(len(in_review) + len(changes_requested) + len(drafts) + len(held_prs), 41)


if __name__ == '__main__':
    unittest.main()
