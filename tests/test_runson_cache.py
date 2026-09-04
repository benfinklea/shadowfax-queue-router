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


class ReviewFindingsTest(unittest.TestCase):
    """The two defects Elrond found reviewing PR #6, each pinned so they cannot return."""

    def setUp(self):
        self._real_fetch = qr._runson_fetch
        self.calls = []

    def tearDown(self):
        qr._runson_fetch = self._real_fetch
        qr.runson_last_read_at = 0.0
        if qr.runson_refresh_lock.locked():
            qr.runson_refresh_lock.release()
        with qr.runson_cache_lock:
            qr.runson_cache["data"] = None
            qr.runson_cache["ts"] = 0.0

    def seed(self, age_seconds):
        with qr.runson_cache_lock:
            qr.runson_cache["data"] = {"available": True, "marker": "cached"}
            qr.runson_cache["ts"] = time.time() - age_seconds

    def test_warm_min_age_leaves_room_before_the_snapshot_goes_stale(self):
        """The threshold has to be TTL minus one warm interval, or the cache expires
        between passes and readers start seeing stale=True on a watched dashboard."""
        self.assertEqual(qr.RUNSON_WARM_MIN_AGE, qr.RUNSON_CACHE_TTL - qr.RUNSON_WARM_INTERVAL)
        self.assertGreater(qr.RUNSON_WARM_MIN_AGE, 0)

    def test_a_fresh_snapshot_is_not_refetched_by_the_warm_loop(self):
        """~4,600 billed DynamoDB rows a minute for a snapshot that was already good."""
        self.seed(age_seconds=5)
        self.assertLess(qr._runson_snapshot_age(), qr.RUNSON_WARM_MIN_AGE)

    def test_an_aging_snapshot_is_refetched_by_the_warm_loop(self):
        self.seed(age_seconds=qr.RUNSON_WARM_MIN_AGE + 5)
        self.assertGreaterEqual(qr._runson_snapshot_age(), qr.RUNSON_WARM_MIN_AGE)

    def test_snapshot_age_of_an_empty_cache_is_infinite(self):
        with qr.runson_cache_lock:
            qr.runson_cache["data"] = None
        self.assertEqual(qr._runson_snapshot_age(), float("inf"))

    def test_a_failed_thread_start_releases_the_gate(self):
        """Otherwise the gate is held by a worker that will never run, and every
        later blocking reader waits forever on a lock nobody owns."""
        original = qr.threading.Thread

        class ExplodingThread(original):
            def start(self):
                raise RuntimeError("can't start new thread")

        qr.threading.Thread = ExplodingThread
        try:
            self.assertFalse(qr._runson_refresh_async(), "a failed start must report failure")
            self.assertFalse(qr.runson_refresh_lock.locked(), "the refresh gate was stranded")
        finally:
            qr.threading.Thread = original

    def test_a_reader_after_a_failed_thread_start_still_gets_served(self):
        """The end-to-end consequence: no deadlock on the next blocking read."""
        original = qr.threading.Thread

        class ExplodingThread(original):
            def start(self):
                raise RuntimeError("can't start new thread")

        self.seed(age_seconds=qr.RUNSON_CACHE_TTL + 30)

        def _fetch():
            self.calls.append(time.time())
            return {"available": True, "marker": "recovered"}

        qr._runson_fetch = _fetch
        qr.threading.Thread = ExplodingThread
        try:
            stale = qr.get_runson_status()
            self.assertEqual(stale["marker"], "cached")
        finally:
            qr.threading.Thread = original

        with qr.runson_cache_lock:
            qr.runson_cache["data"] = None
            qr.runson_cache["ts"] = 0.0
        recovered = qr.get_runson_status()
        self.assertEqual(recovered["marker"], "recovered", "the next blocking reader deadlocked")


class TruthSuiteTimeoutTest(unittest.TestCase):
    def test_runson_is_on_the_fifty_second_tier(self):
        """A 20s budget is what turned a slow-but-healthy endpoint into a hard FAIL."""
        import pathlib
        source = pathlib.Path(__file__).resolve().parent / "dashboard-truth" / "truth_suite.py"
        text = source.read_text()
        self.assertIn('"status", "fleet", "runson"', text)


if __name__ == "__main__":
    unittest.main()


class CreditsErrorVisibilityTest(unittest.TestCase):
    """A denied credits read must be visible in the payload, not only in the log.

    The handler swallows a denied Free Tier read so the panel never blanks
    (council 2026-09-02). That is right. But it left `credits_remaining: null`,
    which reads identically to "no credits data yet" - so a live IAM gap
    (fleet-runson-observer lacks freetier:GetAccountPlanState) was invisible to
    every instrument, and on 2026-09-04 council came within one message of
    retiring the card for it on the grounds that the payload showed no denial.
    It could not. These pin the contract that fixes that.
    """

    def test_the_payload_carries_credits_error_next_to_the_value(self):
        import pathlib
        source = (pathlib.Path(__file__).resolve().parent.parent / "queue_router.py").read_text()
        self.assertIn('"credits_error": credits_error or None,', source)
        # It has to sit with credits_remaining, or a reader can get the value
        # without the reason it is null.
        value_at = source.index('"credits_remaining": credit_amount,')
        error_at = source.index('"credits_error": credits_error or None,')
        self.assertLess(abs(error_at - value_at), 400,
                        "credits_error drifted away from credits_remaining")

    def test_the_truth_suite_fails_on_a_denied_credits_read(self):
        import pathlib
        suite = (pathlib.Path(__file__).resolve().parent / "dashboard-truth" / "truth_suite.py").read_text()
        self.assertIn('"runson.credits_read"', suite)
        self.assertIn('credits_error = r.get("credits_error")', suite)
        self.assertIn('"credits_error"', suite.split("credits_runway")[1][:200],
                      "credits_error is not in the render_state required-key list")
