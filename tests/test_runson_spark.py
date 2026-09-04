"""Runner history uses a temporary database and never calls AWS."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import queue_router as qr


class RunnerHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = patch.dict(qr.CONFIG, db_path=str(Path(self.tmp.name) / 'jobs.db'))
        self.config.start()
        qr.init_db()
        self.now = datetime(2026, 9, 4, 20, tzinfo=timezone.utc)

    def tearDown(self):
        self.config.stop()
        self.tmp.cleanup()

    def test_centered_average_shrinks_both_edges(self):
        raw = [{'ts': str(i), 'n': n} for i, n in enumerate([0, 3, 9, 6, 12, 0])]
        result = qr._smooth_runson_runners(raw)
        self.assertEqual([p['n'] for p in result], [4, 4.5, 6, 6, 6.75, 6])
        self.assertEqual([p['ts'] for p in result], [p['ts'] for p in raw])
        self.assertEqual(qr._smooth_runson_runners([]), [])
        self.assertEqual(qr._smooth_runson_runners(raw[:1]), raw[:1])

    def test_trim_boundary_and_persistence(self):
        qr._record_runson_runners(1, self.now - timedelta(seconds=3601))
        qr._record_runson_runners(2, self.now - timedelta(seconds=3600))
        qr.init_db()  # startup must preserve existing samples
        raw = qr._record_runson_runners(3, self.now)
        self.assertEqual([p['n'] for p in raw], [2, 3])
        raw = qr._record_runson_runners(4, self.now + timedelta(seconds=1))
        self.assertEqual([p['n'] for p in raw], [3, 4])

    def test_warm_timer_accounts_for_fetch_duration(self):
        # A 25-second fetch must leave 35 seconds until the next minute tick.
        with patch.object(qr.time, 'monotonic', side_effect=[0, 0, 60, 85]), \
             patch.object(qr.time, 'sleep', side_effect=[None, InterruptedError]) as sleep, \
             patch.object(qr, '_runson_should_warm', return_value=True), \
             patch.object(qr, '_runson_fetch', return_value={"available": False}) as fetch, \
             patch.object(qr, '_runson_store'):
            with self.assertRaises(InterruptedError):
                qr.runson_warm_loop()
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [60, 35])
        fetch.assert_called_once()

    def test_payload_and_independent_truth_check_detect_tampering(self):
        spec = importlib.util.spec_from_file_location('truth', Path(__file__).parent / 'dashboard-truth/truth_suite.py')
        truth = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(truth)
        qr._record_runson_runners(3, datetime.now(timezone.utc) - timedelta(minutes=1))
        with patch.dict(qr.runson_cache, data=None, ts=0):
            payload = qr._runson_store({'available': True, 'deployed': True, 'live_runners': 9})
            with patch.object(qr, '_runson_fetch', side_effect=AssertionError('must use cache')):
                self.assertEqual(qr.get_runson_status()['live_runners_series'][-1]['n'], 9)
        truth.api = {'runson': payload}
        with patch('builtins.print'):
            truth.check_runson_runner_history()
            self.assertEqual(truth.results[-1]['level'], 'PASS')
            payload['live_runners_smoothed'][-1]['n'] += 1
            truth.check_runson_runner_history()
            self.assertEqual(truth.results[-1]['level'], 'FAIL')
            payload['live_runners_smoothed'][-1]['n'] -= 1
            payload['live_runners'] = 10
            truth.check_runson_runner_history()
            self.assertEqual(truth.results[-1]['level'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
