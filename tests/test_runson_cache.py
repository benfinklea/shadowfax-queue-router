"""Stale-while-revalidate for /api/runson (shadowfax-queue-router#5).

A cold RunsOn read is ~25-30s of serial AWS work: describe-stacks, thousands of
DynamoDB job rows through CLI subprocesses, then Cost Explorer. The hourly
dash-truth run gives the endpoint 20s, so it hit the refresh and reported
`api.runson.schema unreadable`, which cascaded into `runson.render_state`.

These tests replace the AWS read with a fake and assert the contract that makes
that impossible: a reader gets whatever is cached, immediately, and the refresh
happens behind it.
"""
import threading
import time
import unittest

import queue_router as qr


class RunsonCacheTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.release = threading.Event()
        self.entered = threading.Event()
        self._real_fetch = qr._runson_fetch
        with qr.runson_cache_lock:
            qr.runson_cache["data"] = None
            qr.runson_cache["ts"] = 0.0

    def tearDown(self):
        qr._runson_fetch = self._real_fetch
        self.release.set()
        with qr.runson_cache_lock:
            qr.runson_cache["data"] = None
            qr.runson_cache["ts"] = 0.0

    # --- fetch doubles ----------------------------------------------------
    def fast_fetch(self, tag="fresh"):
        def _fetch():
            self.calls.append(time.time())
            return {"available": True, "marker": tag, "jobs_today": len(self.calls)}
        return _fetch

    def slow_fetch(self):
        def _fetch():
            self.calls.append(time.time())
            self.entered.set()
            self.release.wait(timeout=10)
            return {"available": True, "marker": "slow", "jobs_today": len(self.calls)}
        return _fetch

    def seed(self, data, age_seconds):
        with qr.runson_cache_lock:
            qr.runson_cache["data"] = data
            qr.runson_cache["ts"] = time.time() - age_seconds

    # --- tests ------------------------------------------------------------
    def test_cold_cache_blocks_once_and_caches(self):
        """With nothing cached there is no honest alternative to waiting."""
        qr._runson_fetch = self.fast_fetch()
        result = qr.get_runson_status()
        self.assertEqual(result["marker"], "fresh")
        self.assertFalse(result["stale"])
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(qr.runson_cache["data"])

    def test_fresh_cache_never_refetches(self):
        self.seed({"available": True, "marker": "cached"}, age_seconds=5)
        qr._runson_fetch = self.fast_fetch("should-not-run")
        result = qr.get_runson_status()
        self.assertEqual(result["marker"], "cached")
        self.assertEqual(self.calls, [])
        self.assertFalse(result["stale"])
        self.assertAlmostEqual(result["cache_age_seconds"], 5, delta=1.5)

    def test_stale_cache_is_served_immediately_and_refreshed_behind(self):
        """The actual bug: the reader must not wait for the slow AWS read."""
        self.seed({"available": True, "marker": "cached"}, age_seconds=qr.RUNSON_CACHE_TTL + 30)
        qr._runson_fetch = self.slow_fetch()

        started = time.time()
        result = qr.get_runson_status()
        elapsed = time.time() - started

        self.assertEqual(result["marker"], "cached", "a stale reader must get the old snapshot, not None")
        self.assertTrue(result["stale"], "and must be told the snapshot is stale")
        self.assertLess(elapsed, 1.0, f"reader blocked for {elapsed:.1f}s behind the refresh")
        self.assertTrue(self.entered.wait(timeout=5), "no background refresh was started")

        self.release.set()
        for _ in range(100):
            if qr.runson_cache["data"].get("marker") == "slow":
                break
            time.sleep(0.05)
        self.assertEqual(qr.runson_cache["data"]["marker"], "slow", "the background refresh never landed")

    def test_only_one_refresh_runs_at_a_time(self):
        """Two readers arriving on an expired cache must not start two AWS storms."""
        self.seed({"available": True, "marker": "cached"}, age_seconds=qr.RUNSON_CACHE_TTL + 30)
        qr._runson_fetch = self.slow_fetch()

        for _ in range(5):
            qr.get_runson_status()
        self.assertTrue(self.entered.wait(timeout=5))
        time.sleep(0.2)
        self.assertEqual(len(self.calls), 1, f"expected one refresh in flight, got {len(self.calls)}")
        self.release.set()

    def test_beyond_max_stale_the_reader_waits_rather_than_lie(self):
        """A snapshot this old is not evidence any more, so serving it would be a lie."""
        self.seed({"available": True, "marker": "ancient"}, age_seconds=qr.RUNSON_MAX_STALE + 60)
        qr._runson_fetch = self.fast_fetch("refetched")
        result = qr.get_runson_status()
        self.assertEqual(result["marker"], "refetched")
        self.assertEqual(len(self.calls), 1)

    def test_force_refresh_always_fetches(self):
        self.seed({"available": True, "marker": "cached"}, age_seconds=1)
        qr._runson_fetch = self.fast_fetch("forced")
        result = qr.get_runson_status(force_refresh=True)
        self.assertEqual(result["marker"], "forced")
        self.assertEqual(len(self.calls), 1)

    def test_the_cached_dict_is_never_mutated_by_a_reader(self):
        """Annotations go on a copy; two readers must not scribble on shared state."""
        snapshot = {"available": True, "marker": "cached"}
        self.seed(snapshot, age_seconds=5)
        qr._runson_fetch = self.fast_fetch()
        qr.get_runson_status()
        self.assertNotIn("cache_age_seconds", snapshot)
        self.assertNotIn("stale", snapshot)

    def test_a_failed_background_refresh_keeps_the_old_snapshot(self):
        """An AWS blip must not blank the card; the last good read stands."""
        self.seed({"available": True, "marker": "cached"}, age_seconds=qr.RUNSON_CACHE_TTL + 30)

        def _boom():
            self.calls.append(time.time())
            self.entered.set()
            raise RuntimeError("aws exploded")

        qr._runson_fetch = _boom
        result = qr.get_runson_status()
        self.assertEqual(result["marker"], "cached")
        self.assertTrue(self.entered.wait(timeout=5))
        time.sleep(0.3)
        self.assertEqual(qr.runson_cache["data"]["marker"], "cached")
        self.assertFalse(qr.runson_refresh_lock.locked(), "the single-flight gate was left held after a failure")

    def test_refresh_gate_is_released_after_a_normal_background_pass(self):
        self.seed({"available": True, "marker": "cached"}, age_seconds=qr.RUNSON_CACHE_TTL + 30)
        qr._runson_fetch = self.fast_fetch("bg")
        qr.get_runson_status()
        for _ in range(100):
            if not qr.runson_refresh_lock.locked():
                break
            time.sleep(0.05)
        self.assertFalse(qr.runson_refresh_lock.locked())
        self.assertEqual(qr.runson_cache["data"]["marker"], "bg")


class WarmLoopIdleTest(unittest.TestCase):
    """A full warm pass reads ~4,600 DynamoDB rows. Nobody should pay for that
    around the clock on a dashboard no one has open."""

    def tearDown(self):
        qr.runson_last_read_at = 0.0

    def test_no_recent_reader_means_no_warm_pass(self):
        qr.runson_last_read_at = time.time() - (qr.RUNSON_WARM_IDLE_AFTER + 60)
        self.assertFalse(qr._runson_should_warm())

    def test_a_recent_reader_keeps_the_loop_warming(self):
        qr.runson_last_read_at = time.time() - 30
        self.assertTrue(qr._runson_should_warm())

    def test_reading_the_endpoint_re_arms_the_loop(self):
        qr.runson_last_read_at = 0.0
        with qr.runson_cache_lock:
            qr.runson_cache["data"] = {"available": True, "marker": "cached"}
            qr.runson_cache["ts"] = time.time()
        try:
            qr.get_runson_status()
            self.assertTrue(qr._runson_should_warm(), "a live reader did not re-arm the warm loop")
        finally:
            with qr.runson_cache_lock:
                qr.runson_cache["data"] = None
                qr.runson_cache["ts"] = 0.0


class TruthSuiteTimeoutTest(unittest.TestCase):
    def test_runson_is_on_the_fifty_second_tier(self):
        """A 20s budget is what turned a slow-but-healthy endpoint into a hard FAIL."""
        import pathlib
        source = pathlib.Path(__file__).resolve().parent / "dashboard-truth" / "truth_suite.py"
        text = source.read_text()
        self.assertIn('"status", "fleet", "runson"', text)


if __name__ == "__main__":
    unittest.main()
