import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import queue_router as qr


class Response:
    def __init__(self, payload):
        self.payload = payload
        self.ok = True
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class ShippingCounterTests(unittest.TestCase):
    def test_main_commit_activity_counts_the_commit_log_window(self):
        commits = [
            {"sha": "a", "commit": {"committer": {"date": "2026-09-13T00:02:11Z"}}},
            {"sha": "b", "commit": {"committer": {"date": "2026-09-12T23:59:47Z"}}},
        ]
        with mock.patch.object(qr.requests, "get", side_effect=[Response(commits), Response([])]):
            count, last_at = qr._get_main_commit_activity(
                {}, "2026-09-12T23:09:35Z", repo="armbrain-io/armbrain", branch="main"
            )
        self.assertEqual(count, 2, "both main-log commits inside 60m must be counted")
        self.assertEqual(last_at, "2026-09-13T00:02:11Z")

    def test_in_sync_fold_is_a_successful_run_with_zero_work(self):
        state = {
            "status": "in-sync", "ts": "2026-09-13T00:00:04Z",
            "pushed": True, "redirect_sync": "ok",
        }
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "last-run.json"
            state_path.write_text(json.dumps(state))
            with mock.patch.object(qr.subprocess, "run") as run:
                run.return_value = mock.Mock(stdout="0\n")
                ahead, last_at, status = qr._folded_stage(state_path=state_path)
        self.assertEqual((ahead, last_at, status), (0, state["ts"], "in-sync"))

    def test_draft_conflicts_are_not_actionable_conflicts(self):
        prs = [
            {"number": 1, "isDraft": True, "mergeable": "CONFLICTING"},
            {"number": 2, "isDraft": True, "mergeable": "CONFLICTING"},
            {"number": 3, "isDraft": False, "mergeable": "MERGEABLE"},
        ]
        self.assertEqual(qr._actionable_conflicts(prs), [],
                         "an all-draft conflict set is healthy input for CONFLICTED")


class ShippingRedPredicateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        page = qr.app.test_client().get("/").get_data(as_text=True)
        cls.page = page
        js = page[page.index("<script>") + len("<script>"):page.rindex("</script>")]
        names = ("shipOldestClass", "mergedStageClass", "foldedStageClass", "conflictedStageClass")
        thresholds_start = js.index("const SHIP_OLDEST_THRESHOLDS =")
        thresholds_end = js.index("\n};", thresholds_start) + len("\n};")
        functions = [js[thresholds_start:thresholds_end]]
        for name in names:
            match = re.search(r"function " + name + r"\([^)]*\)\s*\{", js)
            if not match:
                raise AssertionError(f"served page is missing {name}")
            start = match.start()
            brace = js.index("{", start)
            depth = 0
            for pos in range(brace, len(js)):
                if js[pos] == "{":
                    depth += 1
                elif js[pos] == "}":
                    depth -= 1
                    if depth == 0:
                        functions.append(js[start:pos + 1])
                        break
        harness = "\n".join(functions) + """
console.log(JSON.stringify({
  mergedHealthy: mergedStageClass(7),
  mergedFailed: mergedStageClass(120),
  foldedHealthy: foldedStageClass('in-sync', new Date(Date.now() - 2 * 60 * 1000).toISOString()),
  foldedStaleButHealthy: foldedStageClass('in-sync', new Date(Date.now() - 90 * 60 * 1000).toISOString()),
  foldedFailed: foldedStageClass('error'),
  conflictedHealthy: conflictedStageClass(0),
  conflictedFailed: conflictedStageClass(1),
}));
"""
        cp = subprocess.run(["node", "-e", harness], capture_output=True,
                            text=True, timeout=10, check=True)
        cls.classes = json.loads(cp.stdout)

    def assertNotRed(self, value, tile):
        self.assertNotRegex(value, r"(^|\s)(hot|merge-red)(\s|$)",
                            f"{tile} must be able to render without red on healthy input")

    def test_merged_red_is_absent_after_recent_main_commit(self):
        self.assertNotRed(self.classes["mergedHealthy"], "MERGED")
        self.assertIn("merge-red", self.classes["mergedFailed"])
        self.assertIn("mergedStageClass(d.merged_since_min)", self.page)

    def test_folded_red_is_absent_after_in_sync_run(self):
        self.assertNotRed(self.classes["foldedHealthy"], "FOLDED")
        self.assertEqual(self.classes["foldedHealthy"], "")
        self.assertEqual(self.classes["foldedStaleButHealthy"], "hot")
        self.assertEqual(self.classes["foldedFailed"], "hot")
        self.assertIn("foldedStageClass(d.fold_status, d.folded_last_at)", self.page)
        self.assertIn("d.folded_last_at, foldCls", self.page)

    def test_conflicted_red_is_absent_with_no_actionable_conflicts(self):
        self.assertNotRed(self.classes["conflictedHealthy"], "CONFLICTED")
        self.assertEqual(self.classes["conflictedFailed"], "hot")
        self.assertIn("conflictedStageClass(d.conflicted)", self.page)


if __name__ == "__main__":
    unittest.main()
